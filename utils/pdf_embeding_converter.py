#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Заполнение ChromaDB эмбеддингами:
1. Markdown с вопросами-ответами (структурированный)
2. PDF-пособия (обычные чанки)

Модель: qwen/qwen3-embedding-8b через OpenRouter
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ====================== НАСТРОЙКИ ======================

HERE = Path(__file__).resolve().parent.parent  # данные лежат в корне репозитория

DEFAULT_QA_FILE = HERE / "Ответы_Транслирующие_системы.md"
DEFAULT_PDF_DIR = HERE / "pdfs"
DEFAULT_DB_DIR = HERE / "chroma_db"
DEFAULT_COLLECTION = "compilers_kb"

OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_MODEL = "qwen/qwen3-embedding-8b"

# Для Q&A
QA_CHUNK_SIZE = 1100
QA_OVERLAP = 180

# Для PDF-пособий
PDF_CHUNK_SIZE = 850
PDF_OVERLAP = 150

EMBED_BATCH = 24
UPSERT_BATCH = 80

# =======================================================


@dataclass
class Section:
    number: int
    question: str
    answer: str
    source_note: str


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict = field(default_factory=dict)


# -------------------- Разбор Markdown Q&A --------------------

HEADING_RE = re.compile(r"^## +(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
ANSWER_MARK = "**Ответ.**"
SOURCE_MARK = "**Пояснение:**"


def parse_qa_document(markdown: str) -> list[Section]:
    sections: list[Section] = []
    matches = list(HEADING_RE.finditer(markdown))

    # Вступление (если есть)
    preamble = markdown[: matches[0].start()] if matches else markdown
    preamble = re.sub(r"^# .*$", "", preamble, count=1, flags=re.MULTILINE).strip()
    if preamble:
        sections.append(
            Section(
                number=0,
                question="Общие сведения о документе",
                answer=preamble,
                source_note="",
            )
        )

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[m.end() : end].strip()

        if SOURCE_MARK in body:
            answer_part, source_note = body.split(SOURCE_MARK, 1)
        else:
            answer_part, source_note = body, ""

        answer = answer_part.replace(ANSWER_MARK, "", 1).strip()
        sections.append(
            Section(
                number=int(m.group(1)),
                question=m.group(2).strip(),
                answer=answer,
                source_note=source_note.strip(),
            )
        )
    return sections


def _split_into_blocks(text: str) -> list[str]:
    """Делит текст на абзацы, сохраняя блоки кода."""
    blocks, buf, in_code = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            buf.append(line)
            continue
        if not line.strip() and not in_code:
            if buf:
                blocks.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return blocks


def _split_long_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]

    pieces = []
    for line in block.split("\n"):
        if len(line) <= limit:
            pieces.append(line)
            continue

        # делим по предложениям
        for sent in re.split(r"(?<=[.!?;])\s+", line):
            while len(sent) > limit:
                cut = sent.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                pieces.append(sent[:cut])
                sent = sent[cut:].lstrip()
            if sent:
                pieces.append(sent)

    # склеиваем мелкие куски
    merged = []
    for p in pieces:
        if merged and len(merged[-1]) + 1 + len(p) <= limit:
            merged[-1] += "\n" + p
        else:
            merged.append(p)
    return merged


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    if overlap >= chunk_size:
        raise ValueError("overlap должен быть меньше chunk_size")

    body_limit = chunk_size - overlap - 10
    units = []
    for block in _split_into_blocks(text):
        units.extend(_split_long_block(block, body_limit))

    chunks = []
    current = ""
    for unit in units:
        if current and len(current) + 2 + len(unit) > chunk_size:
            chunks.append(current)
            # overlap
            tail = current[-overlap:] if len(current) > overlap else current
            # стараемся начать с начала предложения/строки
            m = re.search(r"\n|(?<=[.!?;])\s+", tail)
            if m:
                tail = tail[m.end() :]
            current = (tail + "\n\n" + unit).strip() if tail else unit
        else:
            current = (current + "\n\n" + unit).strip() if current else unit
    if current.strip():
        chunks.append(current)
    return chunks


def build_qa_chunks(
    sections: list[Section], source_file: str, model: str
) -> list[Chunk]:
    chunks = []
    for sec in sections:
        header = f"Вопрос {sec.number}. {sec.question}" if sec.number else sec.question
        parts = split_text(sec.answer, QA_CHUNK_SIZE, QA_OVERLAP)

        for idx, part in enumerate(parts):
            # Важно: в каждый чанк кладём заголовок вопроса
            full_text = f"{header}\n\n{part}"

            chunks.append(
                Chunk(
                    chunk_id=f"qa_q{sec.number:02d}_c{idx:02d}",
                    text=full_text,
                    metadata={
                        "type": "qa",
                        "question_number": sec.number,
                        "question": sec.question,
                        "chunk_index": idx,
                        "chunks_total": len(parts),
                        "source_note": sec.source_note[:500] if sec.source_note else "",
                        "source_file": source_file,
                        "embedding_model": model,
                    },
                )
            )
    return chunks


# -------------------- PDF --------------------


def extract_text_from_pdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("Установи pypdf: uv pip install pypdf")

    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"[Страница {i + 1}]\n{text}")
    return "\n\n".join(pages)


def build_pdf_chunks(pdf_path: Path, model: str) -> list[Chunk]:
    text = extract_text_from_pdf(pdf_path)
    if not text.strip():
        print(f"  [!] Пустой PDF: {pdf_path.name}")
        return []

    parts = split_text(text, PDF_CHUNK_SIZE, PDF_OVERLAP)
    chunks = []
    for idx, part in enumerate(parts):
        chunks.append(
            Chunk(
                chunk_id=f"pdf_{pdf_path.stem}_c{idx:04d}",
                text=part,
                metadata={
                    "type": "textbook",
                    "source_file": pdf_path.name,
                    "chunk_index": idx,
                    "chunks_total": len(parts),
                    "embedding_model": model,
                },
            )
        )
    return chunks


# -------------------- OpenRouter Embeddings --------------------


def embed_texts(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost",
    }

    vectors = []
    session = requests.Session()

    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        for attempt in range(1, 6):
            try:
                resp = session.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json={"model": model, "input": batch},
                    timeout=90,
                )
                data = resp.json()
                if resp.status_code == 200 and "data" in data:
                    items = sorted(data["data"], key=lambda x: x["index"])
                    vectors.extend(item["embedding"] for item in items)
                    break
                else:
                    error = data.get("error", resp.text[:200])
                    if resp.status_code in (400, 401, 402, 403):
                        raise RuntimeError(
                            f"OpenRouter error {resp.status_code}: {error}"
                        )
                    print(f"  Попытка {attempt}/5: {error}")
            except requests.RequestException as e:
                print(f"  Сетевая ошибка (попытка {attempt}): {e}")

            if attempt == 5:
                raise RuntimeError("Не удалось получить эмбеддинги после 5 попыток")
            time.sleep(min(2**attempt, 20))

        print(f"  эмбеддинги: {min(start + EMBED_BATCH, len(texts))}/{len(texts)}")

    return vectors


# -------------------- ChromaDB --------------------


def get_collection(db_dir: Path, name: str, model: str, reset: bool = False):
    import chromadb

    client = chromadb.PersistentClient(path=str(db_dir))

    if reset:
        try:
            client.delete_collection(name)
            print(f"Коллекция «{name}» удалена")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=name,
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": model,
        },
    )

    # Проверка совместимости модели
    stored = (collection.metadata or {}).get("embedding_model")
    if stored and stored != model:
        raise SystemExit(
            f"Коллекция создана под модель {stored}, а сейчас {model}.\n"
            "Запусти с --reset"
        )
    return collection


def upsert_chunks(collection, chunks: list[Chunk], vectors: list[list[float]]):
    for start in range(0, len(chunks), UPSERT_BATCH):
        batch_chunks = chunks[start : start + UPSERT_BATCH]
        batch_vectors = vectors[start : start + UPSERT_BATCH]

        collection.upsert(
            ids=[c.chunk_id for c in batch_chunks],
            documents=[c.text for c in batch_chunks],
            embeddings=batch_vectors,
            metadatas=[c.metadata for c in batch_chunks],
        )


# -------------------- Поиск --------------------


def search(collection, query: str, model: str, api_key: str, top_k: int = 5):
    print(f"\nЗапрос: {query}\n")
    vector = embed_texts([query], model, api_key)[0]

    results = collection.query(
        query_embeddings=[vector],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    for i, (doc, meta, dist) in enumerate(
        zip(results["documents"][0], results["metadatas"][0], results["distances"][0]),
        1,
    ):
        score = 1 - dist  # cosine similarity
        q_num = meta.get("question_number", "—")
        src = meta.get("source_file", "")
        typ = meta.get("type", "")

        print(f"{'=' * 70}")
        print(f"{i}. type={typ} | вопрос {q_num} | score={score:.3f} | {src}")
        print(f"{'=' * 70}")
        print(doc[:1200] + ("..." if len(doc) > 1200 else ""))
        if meta.get("source_note"):
            print(f"\n[Пояснение] {meta['source_note'][:300]}")
        print()


# -------------------- Main --------------------


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Заполнение ChromaDB (Q&A + PDF)")
    parser.add_argument(
        "--qa", type=Path, default=DEFAULT_QA_FILE, help="Markdown с вопросами-ответами"
    )
    parser.add_argument(
        "--pdf-dir", type=Path, default=DEFAULT_PDF_DIR, help="Папка с PDF-пособиями"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_DIR)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument(
        "--model", default=os.getenv("OPENROUTER_EMBEDDING_MODEL", DEFAULT_MODEL)
    )
    parser.add_argument(
        "--reset", action="store_true", help="Удалить коллекцию и создать заново"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Только показать статистику, без API"
    )
    parser.add_argument("--only-qa", action="store_true", help="Только вопросы-ответы")
    parser.add_argument("--only-pdf", action="store_true", help="Только PDF")
    parser.add_argument(
        "--query", type=str, help="Выполнить поисковый запрос по уже заполненной базе"
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Сколько результатов вернуть при --query"
    )
    args = parser.parse_args()

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    # ========== Режим поиска ==========
    if args.query:
        if not api_key:
            sys.exit("Задай OPENROUTER_API_KEY")
        collection = get_collection(args.db, args.collection, args.model, reset=False)
        search(collection, args.query, args.model, api_key, args.top_k)
        return

    # ========== Режим заполнения ==========
    if not args.dry_run and not api_key:
        sys.exit("Задай переменную окружения OPENROUTER_API_KEY")

    all_chunks: list[Chunk] = []

    # 1. Вопросы-ответы
    if not args.only_pdf and args.qa.exists():
        print(f"\n=== Обработка Q&A: {args.qa.name} ===")
        markdown = args.qa.read_text(encoding="utf-8")
        sections = parse_qa_document(markdown)
        qa_chunks = build_qa_chunks(sections, args.qa.name, args.model)
        all_chunks.extend(qa_chunks)
        print(f"  Вопросов: {len(sections)}, чанков: {len(qa_chunks)}")
    elif not args.only_pdf:
        print(f"[!] Файл Q&A не найден: {args.qa}")

    # 2. PDF
    if not args.only_qa and args.pdf_dir.exists():
        pdf_files = sorted(args.pdf_dir.glob("*.pdf"))
        if not pdf_files:
            print(f"[!] В папке {args.pdf_dir} нет PDF-файлов")
        else:
            print(f"\n=== Обработка PDF ({len(pdf_files)} файлов) ===")
            for pdf in pdf_files:
                print(f"  → {pdf.name}")
                pdf_chunks = build_pdf_chunks(pdf, args.model)
                all_chunks.extend(pdf_chunks)
                print(f"     чанков: {len(pdf_chunks)}")
    elif not args.only_qa:
        print(f"[!] Папка с PDF не найдена: {args.pdf_dir}")

    if not all_chunks:
        sys.exit("Нет данных для загрузки")

    print(f"\nВсего чанков: {len(all_chunks)}")
    print(f"Модель: {args.model}")

    if args.dry_run:
        print("\n--- Пример первых 2 чанков ---")
        for c in all_chunks[:2]:
            print(f"\nID: {c.chunk_id}")
            print(f"Type: {c.metadata.get('type')}")
            print(c.text[:400] + "..." if len(c.text) > 400 else c.text)
        return

    print("\nПолучение эмбеддингов...")
    vectors = embed_texts([c.text for c in all_chunks], args.model, api_key)

    print("Запись в ChromaDB...")
    collection = get_collection(args.db, args.collection, args.model, reset=args.reset)

    # Удаляем старые записи этих файлов
    source_files = list({c.metadata["source_file"] for c in all_chunks})
    for sf in source_files:
        collection.delete(where={"source_file": sf})

    upsert_chunks(collection, all_chunks, vectors)

    print("\nГотово!")
    print(f"Коллекция: {args.collection}")
    print(f"Всего записей: {collection.count()}")
    print(f"Размерность: {len(vectors[0])}")
    print(f"База: {args.db}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"Ошибка: {e}")

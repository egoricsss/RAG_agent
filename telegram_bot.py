"""Private Telegram interface for the course RAG agent."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import DEFAULT_CHAT_MODEL, ExamAgent

logger = logging.getLogger(__name__)
ALBUM_DELAY = 2.0
MAX_PHOTOS = 10  # Telegram albums contain at most 10 items.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 4096


@dataclass(frozen=True)
class BotConfig:
    token: str
    allowed_ids: frozenset[int]
    vision_model: str

    @classmethod
    def from_env(cls) -> BotConfig:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN.")
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        parts = raw_ids.split(",")
        if not all(part.strip().isascii() and part.strip().isdigit() for part in parts):
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS должен содержать ID через запятую."
            )
        ids = frozenset(int(part.strip()) for part in parts)
        if not ids or 0 in ids:
            raise ValueError("Укажите положительные ID в TELEGRAM_ALLOWED_USER_IDS.")
        model = os.getenv("OPENROUTER_VISION_MODEL", DEFAULT_CHAT_MODEL).strip()
        if not model:
            raise ValueError("Укажите OPENROUTER_VISION_MODEL.")
        return cls(token, ids, model)


class ExtractedQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(
        description="Отдельные задания с вариантами ответа, в исходном порядке."
    )


async def extract_questions(
    service: ExamAgent, model: str, images: list[bytes]
) -> list[str]:
    """Transcribe photos without attempting to solve the questions."""
    content = [
        {
            "type": "text",
            "text": (
                "Перепиши задания с изображений по порядку. Верни JSON с полем questions: "
                "массив строк; одна строка на задание вместе со всеми его вариантами ответа. "
                "Сохраняй номера и обозначения вариантов, не решай задания и не выдумывай "
                "неразборчивый текст — обозначай его [неразборчиво]. Если заданий нет, "
                "верни пустой массив. Изображения могут продолжать одно задание."
            ),
        }
    ]
    for image in images:
        encoded = base64.b64encode(image).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    response = await service.chat_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "extracted_questions",
                "strict": True,
                "schema": ExtractedQuestions.model_json_schema(),
            },
        },
        timeout=90,
    )
    text = response.choices[0].message.content
    if not text:
        raise ValueError("Модель не вернула текст с фотографий.")
    result = ExtractedQuestions.model_validate_json(text)
    return [question.strip() for question in result.questions if question.strip()]


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split plain text conservatively, counting non-BMP characters as two units."""
    chunks = []
    start = 0
    while start < len(text):
        end, units, last_newline = start, 0, -1
        while end < len(text):
            size = 2 if ord(text[end]) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
            if text[end - 1] == "\n":
                last_newline = end
        if end < len(text) and last_newline > start:
            end = last_newline
        if end == start:
            raise ValueError("Message limit is too small for a character")
        chunks.append(text[start:end])
        start = end
    return chunks


@dataclass
class Album:
    messages: list = field(default_factory=list)
    task: asyncio.Task | None = None


class PrivateBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.service: ExamAgent | None = None
        self.albums: dict[tuple[int, int, str], Album] = {}
        self.tasks: set[asyncio.Task] = set()

    def authorized(self, update: Update) -> bool:
        return bool(
            update.effective_user
            and update.effective_user.id in self.config.allowed_ids
            and update.effective_chat
            and update.effective_chat.type == "private"
            and update.effective_message
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        await update.effective_message.reply_text(
            "Отправьте вопрос текстом или фото теста. Несколько фото можно отправить одним альбомом."
        )

    async def send_answer(self, message, question: str) -> None:
        assert self.service is not None
        answer = await self.service.answer(question)
        for part in split_message(answer):
            await message.reply_text(part)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        try:
            await self.send_answer(message, message.text)
        except Exception as exc:
            logger.error("Text request failed: %s", type(exc).__name__)
            await message.reply_text(
                "Не удалось обработать вопрос. Попробуйте ещё раз."
            )

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        if not message.media_group_id:
            await self.process_photos([message], context)
            return
        key = (
            update.effective_chat.id,
            update.effective_user.id,
            message.media_group_id,
        )
        album = self.albums.setdefault(key, Album())
        album.messages.append(message)
        if album.task:
            album.task.cancel()
        album.task = asyncio.create_task(self.finish_album(key, context))
        self.tasks.add(album.task)
        album.task.add_done_callback(self.tasks.discard)

    async def finish_album(self, key, context) -> None:
        try:
            await asyncio.sleep(ALBUM_DELAY)
        except asyncio.CancelledError:
            return
        album = self.albums.pop(key, None)
        if album:
            await self.process_photos(album.messages, context)

    async def process_photos(self, messages, context) -> None:
        message = messages[0]
        try:
            if len(messages) > MAX_PHOTOS:
                raise ValueError("Слишком много фотографий в альбоме.")
            images = []
            for item in sorted(messages, key=lambda item: item.message_id):
                photo = item.photo[-1]
                if photo.file_size is not None and photo.file_size > MAX_PHOTO_BYTES:
                    raise ValueError("Фотография слишком большая (максимум 10 МиБ).")
                file = await context.bot.get_file(photo.file_id)
                data = await file.download_as_bytearray()
                if len(data) > MAX_PHOTO_BYTES:
                    raise ValueError("Фотография слишком большая (максимум 10 МиБ).")
                images.append(bytes(data))
            assert self.service is not None
            questions = await extract_questions(
                self.service, self.config.vision_model, images
            )
            if not questions:
                await message.reply_text(
                    "Не удалось найти задания на фото. Пришлите более чёткое фото или текст."
                )
                return
            caption = "\n".join(item.caption for item in messages if item.caption)
            for index, question in enumerate(questions, 1):
                # Show the transcription so the user can catch OCR mistakes.
                heading = f"Задание {index}:\n{question}"
                for part in split_message(heading):
                    await message.reply_text(part)
                prompt = (
                    f"{question}\n\nКомментарий пользователя: {caption}"
                    if caption
                    else question
                )
                try:
                    await self.send_answer(message, prompt)
                except Exception as exc:
                    logger.error("Extracted question failed: %s", type(exc).__name__)
                    await message.reply_text(
                        "Не удалось ответить на это задание. Попробуйте отправить его текстом."
                    )
        except ValueError as exc:
            logger.warning("Photo request rejected: %s", type(exc).__name__)
            await message.reply_text(
                "Не удалось прочитать фото. Проверьте качество и размер (до 10 МиБ на фото) и попробуйте ещё раз."
            )
        except Exception as exc:
            logger.error("Photo request failed: %s", type(exc).__name__)
            await message.reply_text(
                "Не удалось обработать фото. Попробуйте ещё раз или отправьте вопрос текстом."
            )

    async def initialize(self, application: Application) -> None:
        self.service = ExamAgent.from_env()

    async def shutdown(self, application: Application) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.albums.clear()
        if self.service:
            await self.service.close()
            self.service = None


def main() -> None:
    config = BotConfig.from_env()
    bot = PrivateBot(config)
    application = (
        Application.builder()
        .token(config.token)
        .post_init(bot.initialize)
        .post_shutdown(bot.shutdown)
        .build()
    )
    application.add_handler(CommandHandler(["start", "help"], bot.start))
    application.add_handler(MessageHandler(filters.PHOTO, bot.photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()

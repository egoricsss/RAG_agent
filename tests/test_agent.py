import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import chromadb
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from agent import NO_CONTEXT, Deps, ExamAgent, create_agent, retrieve_context


def embedding_client(vector):
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=vector)]
    )
    return client


class RetrievalTests(unittest.TestCase):
    def test_real_chroma_query_and_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            collection = chromadb.PersistentClient(path=directory).create_collection(
                "test_collection", configuration={"hnsw": {"space": "cosine"}}
            )
            collection.add(
                ids=["textbook", "strong_qa", "weak_qa"],
                embeddings=[[1, 0], [0.8, 0.6], [0, 1]],
                documents=["Точный учебник", "Подходящий ответ", "Другой ответ"],
                metadatas=[
                    {"type": "textbook", "source_file": "book.pdf"},
                    {"type": "qa", "source_file": "answers.md", "question_number": 29},
                    {"type": "qa", "source_file": "answers.md", "question_number": 1},
                ],
            )
            client = embedding_client([1, 0])
            context = retrieve_context(Deps(collection, client), "вопрос")
            self.assertLess(
                context.index("Подходящий ответ"), context.index("Точный учебник")
            )
            self.assertLess(
                context.index("Точный учебник"), context.index("Другой ответ")
            )
            self.assertIn("вопрос 29", context)
            self.assertIn("book.pdf", context)
            client.embeddings.create.assert_called_once_with(
                model="qwen/qwen3-embedding-8b", input="вопрос"
            )

    def test_empty_collection_does_not_call_embedding(self):
        collection = Mock()
        collection.count.return_value = 0
        client = embedding_client([1, 0])
        self.assertEqual(
            retrieve_context(Deps(collection, client), "вопрос"), NO_CONTEXT
        )
        client.embeddings.create.assert_not_called()

    def test_six_chunks_max_and_missing_metadata(self):
        collection = Mock()
        collection.count.return_value = 8
        collection.query.return_value = {
            "documents": [[f"chunk-{i}" for i in range(8)]],
            "metadatas": [[None] * 8],
            "distances": [[0.1] * 8],
        }
        context = retrieve_context(Deps(collection, embedding_client([1, 0])), "q")
        self.assertIn("chunk-5", context)
        self.assertNotIn("chunk-6", context)
        self.assertIn("источник неизвестен", context)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_always_retrieves_and_calls_model(self):
        deps = Deps(Mock(), embedding_client([1, 0]))
        agent = create_agent(
            TestModel(call_tools=[], custom_output_text="Ответ с источником")
        )
        service = ExamAgent(agent, deps, AsyncMock())
        with patch(
            "agent.retrieve_context", return_value="Материалы из book.pdf"
        ) as retrieve:
            answer = await service.answer("  Вопрос?  ")
        self.assertEqual(answer, "Ответ с источником")
        retrieve.assert_called_once_with(deps, "Вопрос?")

    async def test_agent_can_call_additional_retrieve_tool(self):
        deps = Deps(Mock(), embedding_client([1, 0]))
        agent = create_agent(
            TestModel(call_tools=["retrieve"], custom_output_text="Ответ")
        )
        service = ExamAgent(agent, deps, AsyncMock())
        with patch("agent.retrieve_context", return_value="Материалы") as retrieve:
            self.assertEqual(await service.answer("Вопрос?"), "Ответ")
        self.assertEqual(retrieve.call_count, 2)
        self.assertEqual(retrieve.call_args_list[0].args, (deps, "Вопрос?"))

    async def test_question_and_context_are_sent_to_model(self):
        received = []

        def respond(messages, info):
            received.extend(messages)
            return ModelResponse(parts=[TextPart("Ответ")])

        deps = Deps(Mock(), embedding_client([1, 0]))
        service = ExamAgent(create_agent(FunctionModel(respond)), deps, AsyncMock())
        with patch("agent.retrieve_context", return_value="Текст из book.pdf"):
            self.assertEqual(await service.answer("Тестовый вопрос"), "Ответ")
        self.assertIn("Тестовый вопрос", str(received))
        self.assertIn("Текст из book.pdf", str(received))

    async def test_empty_context_skips_model_and_empty_question_fails(self):
        model = Mock()
        service = ExamAgent(model, Deps(Mock(), embedding_client([1, 0])), AsyncMock())
        with patch("agent.retrieve_context", return_value=NO_CONTEXT):
            self.assertEqual(await service.answer("вопрос"), NO_CONTEXT)
        model.run.assert_not_called()
        with self.assertRaisesRegex(ValueError, "пустым"):
            await service.answer("  ")

    async def test_close_clients(self):
        deps = Deps(Mock(), embedding_client([1, 0]))
        chat = AsyncMock()
        service = ExamAgent(Mock(), deps, chat)
        await service.close()
        chat.close.assert_awaited_once()
        deps.embed_client.close.assert_called_once()

    async def test_from_env_checks_existing_database_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            collection = chromadb.PersistentClient(path=directory).create_collection(
                "compilers_kb",
                metadata={"embedding_model": "another-model"},
                configuration={"hnsw": {"space": "cosine"}},
            )
            self.assertTrue((Path(directory) / "chroma.sqlite3").is_file())
            self.assertEqual(collection.count(), 0)
            with patch.dict(
                "os.environ",
                {"OPENROUTER_API_KEY": "test", "CHROMA_DB_PATH": directory},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "another-model"):
                    ExamAgent.from_env()

    async def test_from_env_success_and_missing_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            chromadb.PersistentClient(path=directory).create_collection(
                "compilers_kb",
                metadata={"embedding_model": "qwen/qwen3-embedding-8b"},
                configuration={"hnsw": {"space": "cosine"}},
            )
            env = {"OPENROUTER_API_KEY": "test", "CHROMA_DB_PATH": directory}
            with patch.dict("os.environ", env, clear=True):
                async with ExamAgent.from_env() as service:
                    self.assertEqual(await service.answer("вопрос"), NO_CONTEXT)
            with patch.dict(
                "os.environ", {**env, "CHROMA_COLLECTION": "missing"}, clear=True
            ):
                with self.assertRaisesRegex(
                    ValueError, "Коллекция ChromaDB не найдена"
                ):
                    ExamAgent.from_env()


if __name__ == "__main__":
    unittest.main()

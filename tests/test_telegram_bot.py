import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram_bot import BotConfig, PrivateBot, extract_questions, split_message


def make_update(user_id=12, chat_type="private", text="Вопрос", photos=None,
                group=None, message_id=1, caption=None):
    message = SimpleNamespace(
        text=text, photo=photos or [], media_group_id=group,
        message_id=message_id, caption=caption, reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        effective_chat=SimpleNamespace(id=123, type=chat_type),
        effective_message=message,
    )


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_IDS": " 12,34,12 "
        }, clear=True):
            config = BotConfig.from_env()
        self.assertEqual(config.allowed_ids, {12, 34})
        self.assertEqual(config.vision_model, "google/gemini-2.5-flash")

    def test_bad_config_fails_closed(self):
        for ids in ("", "12,", "12,abc", "-12", "0", "１２"):
            with self.subTest(ids=ids), patch.dict("os.environ", {
                "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_IDS": ids
            }, clear=True):
                with self.assertRaises(ValueError):
                    BotConfig.from_env()
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "12"}, clear=True):
            with self.assertRaises(ValueError):
                BotConfig.from_env()

    def test_split_keeps_all_text(self):
        for text in ("a" * 9000, "line\n" * 2000, "short", "😀" * 5000):
            chunks = split_message(text)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = PrivateBot(BotConfig("token", frozenset({12}), "vision"))
        self.bot.service = SimpleNamespace(answer=AsyncMock(return_value="Ответ"), close=AsyncMock())
        self.context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))

    async def test_denied_users_and_groups_never_trigger_handlers(self):
        for update in (make_update(user_id=99), make_update(chat_type="group"),
                       make_update(user_id=None)):
            await self.bot.start(update, self.context)
            await self.bot.text(update, self.context)
            await self.bot.photo(update, self.context)
            update.effective_message.reply_text.assert_not_awaited()
        self.bot.service.answer.assert_not_awaited()
        self.context.bot.get_file.assert_not_awaited()

    async def test_text_forwarded_and_long_answer_split(self):
        update = make_update(text="Текст вопроса")
        self.bot.service.answer.return_value = "X" * 5000
        await self.bot.text(update, self.context)
        self.bot.service.answer.assert_awaited_once_with("Текст вопроса")
        self.assertEqual(update.effective_message.reply_text.await_count, 2)

    async def test_single_photo_questions_passed_individually(self):
        photo = SimpleNamespace(file_id="id", file_size=5)
        update = make_update(photos=[photo], caption="Объясни")
        file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"image")))
        self.context.bot.get_file.return_value = file
        with patch("telegram_bot.extract_questions", new=AsyncMock(return_value=["Вопрос 1", "Вопрос 2"])) as ocr:
            await self.bot.photo(update, self.context)
        ocr.assert_awaited_once_with(self.bot.service, "vision", [b"image"])
        self.assertEqual(self.bot.service.answer.await_count, 2)
        self.bot.service.answer.assert_any_await("Вопрос 1\n\nКомментарий пользователя: Объясни")
        self.assertEqual(update.effective_message.reply_text.await_count, 4)

    async def test_album_batched_sorted_and_isolated(self):
        photo1 = SimpleNamespace(file_id="1", file_size=1)
        photo2 = SimpleNamespace(file_id="2", file_size=1)
        first = make_update(photos=[photo2], group="album", message_id=2)
        second = make_update(photos=[photo1], group="album", message_id=1)
        other_user = make_update(user_id=99, photos=[photo1], group="album")
        self.context.bot.get_file.side_effect = lambda file_id: SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=file_id.encode())
        )
        with patch("telegram_bot.ALBUM_DELAY", 0.01), patch(
            "telegram_bot.extract_questions", new=AsyncMock(return_value=["Question"])
        ) as ocr:
            await self.bot.photo(first, self.context)
            await self.bot.photo(other_user, self.context)
            await self.bot.photo(second, self.context)
            await asyncio.gather(*self.bot.tasks, return_exceptions=True)
        ocr.assert_awaited_once_with(self.bot.service, "vision", [b"1", b"2"])
        self.bot.service.answer.assert_awaited_once_with("Question")
        self.assertFalse(self.bot.albums)

    async def test_empty_and_oversized_photo(self):
        photo = SimpleNamespace(file_id="id", file_size=11 * 1024 * 1024)
        update = make_update(photos=[photo])
        await self.bot.photo(update, self.context)
        self.context.bot.get_file.assert_not_awaited()
        self.bot.service.answer.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once()

    async def test_empty_ocr_and_partial_answer_failure(self):
        photo = SimpleNamespace(file_id="id", file_size=1)
        self.context.bot.get_file.return_value = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=b"x")
        )
        update = make_update(photos=[photo])
        with patch("telegram_bot.extract_questions", new=AsyncMock(return_value=[])):
            await self.bot.photo(update, self.context)
        self.bot.service.answer.assert_not_awaited()
        self.assertIn("Не удалось найти", update.effective_message.reply_text.await_args.args[0])

        self.bot.service.answer.side_effect = [RuntimeError("failure"), "Ответ"]
        with patch("telegram_bot.extract_questions", new=AsyncMock(return_value=["1", "2"])):
            await self.bot.photo(update, self.context)
        self.assertEqual(self.bot.service.answer.await_count, 2)
        self.assertEqual(update.effective_message.reply_text.await_args.args[0], "Ответ")

    async def test_shutdown_cancels_pending_album_and_closes_service(self):
        update = make_update(photos=[SimpleNamespace(file_id="1")], group="group")
        await self.bot.photo(update, self.context)
        service = self.bot.service
        await self.bot.shutdown(None)
        service.close.assert_awaited_once()
        self.assertFalse(self.bot.albums)
        self.context.bot.get_file.assert_not_awaited()

    async def test_vision_request_contains_all_images_and_parses_result(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"questions": [" A ", "B"]}))
        )]))
        service = SimpleNamespace(chat_client=SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )))
        questions = await extract_questions(service, "vision-model", [b"a", b"b"])
        self.assertEqual(questions, ["A", "B"])
        args = create.await_args.kwargs
        self.assertEqual(args["model"], "vision-model")
        content = args["messages"][0]["content"]
        self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,YQ==")
        self.assertEqual(content[2]["image_url"]["url"], "data:image/jpeg;base64,Yg==")
        self.assertEqual(args["response_format"]["type"], "json_schema")


if __name__ == "__main__":
    unittest.main()

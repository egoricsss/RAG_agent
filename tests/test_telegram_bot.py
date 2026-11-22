import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from telegram import MessageEntity

from telegram_bot import (
    BotConfig,
    PrivateBot,
    extract_questions,
    format_answer,
    split_message,
)


def make_update(user_id=12, chat_type="private", text="Вопрос", photos=None,
                group=None, message_id=1, caption=None, username=None):
    user = SimpleNamespace(id=user_id, username=username) if user_id is not None else None
    message = SimpleNamespace(
        text=text, photo=photos or [], media_group_id=group,
        message_id=message_id, caption=caption, reply_text=AsyncMock(), from_user=user,
    )
    return SimpleNamespace(
        effective_user=user,
        effective_chat=SimpleNamespace(id=123, type=chat_type),
        effective_message=message,
    )


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_IDS": " 12,34,12 ",
            "TELEGRAM_SUPER_ADMIN_ID": "42",
        }, clear=True):
            config = BotConfig.from_env()
        self.assertEqual(config.allowed_ids, {12, 34})
        self.assertEqual(config.vision_model, "google/gemini-2.5-flash")

    def test_bad_config_fails_closed(self):
        for ids in ("12,", "12,abc", "-12", "0", "１２", "999999999999999999999"):
            with self.subTest(ids=ids), patch.dict("os.environ", {
                "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_IDS": ids,
                "TELEGRAM_SUPER_ADMIN_ID": "42",
            }, clear=True):
                with self.assertRaises(ValueError):
                    BotConfig.from_env()
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "12"}, clear=True):
            with self.assertRaises(ValueError):
                BotConfig.from_env()
        for admin in ("", "0", "-1", "abc", "１２", "999999999999999999999"):
            with self.subTest(admin=admin), patch.dict("os.environ", {
                "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_SUPER_ADMIN_ID": admin,
            }, clear=True), self.assertRaises(ValueError):
                BotConfig.from_env()

    def test_initial_ids_are_optional(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_SUPER_ADMIN_ID": "42"}, clear=True):
            self.assertEqual(BotConfig.from_env().allowed_ids, frozenset())

    def test_split_keeps_all_text(self):
        for text in ("a" * 9000, "line\n" * 2000, "short", "😀" * 5000):
            chunks = split_message(text)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))


class FormattingTests(unittest.TestCase):
    def test_emphasis_code_lists_and_literal_html(self):
        (text, entities), = format_answer(
            "😀 **Б. `id_1`**\n\n- *Курсив*\n- <b>A & B</b>\n\n```\nx < y\n```"
        )
        self.assertEqual(text, "😀 Б. id_1\n\n• Курсив\n• <b>A & B</b>\n\nx < y")
        encoded = text.encode("utf-16-le")
        self.assertEqual(
            [(e.type, encoded[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le"))
             for e in entities],
            [("bold", "Б. "), ("code", "id_1"), ("italic", "Курсив"), ("pre", "x < y")],
        )

    def test_formatted_span_survives_message_split(self):
        parts = format_answer(f"**{'😀' * 2049}**")
        self.assertEqual([text for text, _ in parts], ["😀" * 2048, "😀"])
        self.assertEqual([[(e.type, e.offset, e.length) for e in entities] for _, entities in parts], [
            [("bold", 0, 4096)],
            [("bold", 0, 2)],
        ])

    def test_unclosed_markdown_can_still_be_sent(self):
        self.assertEqual(format_answer("**без закрытия и `код"), [("**без закрытия и `код", [])])


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = PrivateBot(BotConfig("token", frozenset({12}), "vision", 42, ":memory:"))
        self.addCleanup(self.bot.access.close)
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

    async def test_markdown_answer_sent_with_telegram_entities(self):
        update = make_update()
        self.bot.service.answer.return_value = "**Б.** `id_1`"
        await self.bot.text(update, self.context)
        update.effective_message.reply_text.assert_awaited_once_with(
            "Б. id_1", entities=[MessageEntity("bold", 0, 2), MessageEntity("code", 3, 4)]
        )

    async def test_single_photo_sends_only_answers_in_order(self):
        photo = SimpleNamespace(file_id="id", file_size=5)
        update = make_update(photos=[photo], caption="Объясни")
        file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"image")))
        self.context.bot.get_file.return_value = file
        self.bot.service.answer.side_effect = ["Первый ответ", "Второй ответ"]
        with patch("telegram_bot.extract_questions", new=AsyncMock(return_value=["Вопрос 1", "Вопрос 2"])) as ocr:
            await self.bot.photo(update, self.context)
        ocr.assert_awaited_once_with(self.bot.service, "vision", [b"image"])
        self.assertEqual(self.bot.service.answer.await_args_list, [
            call("Вопрос 1\n\nКомментарий пользователя: Объясни"),
            call("Вопрос 2\n\nКомментарий пользователя: Объясни"),
        ])
        self.assertEqual(update.effective_message.reply_text.await_args_list, [
            call("Первый ответ", entities=[]),
            call("Второй ответ", entities=[]),
        ])

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
        first.effective_message.reply_text.assert_awaited_once_with("Ответ", entities=[])
        second.effective_message.reply_text.assert_not_awaited()
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

    async def test_admin_invites_and_username_binds_to_id(self):
        admin = make_update(user_id=42)
        ctx = SimpleNamespace(args=["@Example_User"])
        await self.bot.add_user(admin, ctx)
        self.assertEqual(self.bot.access.list_users()[1], ["example_user"])
        invited = make_update(user_id=99, username="eXample_User")
        await self.bot.start(invited, self.context)
        invited.effective_message.reply_text.assert_awaited_once()
        self.assertTrue(self.bot.access.contains(99))
        self.assertEqual(self.bot.access.list_users()[1], [])
        other = make_update(user_id=100, username="example_user")
        await self.bot.start(other, self.context)
        other.effective_message.reply_text.assert_not_awaited()
        # The grant is bound to the numeric ID, even when username changes.
        await self.bot.start(make_update(user_id=99, username="another_name"), self.context)
        self.assertTrue(self.bot.access.contains(99))

    async def test_admin_commands_are_private_and_admin_only(self):
        ctx = SimpleNamespace(args=["@new_user"])
        for update in (make_update(user_id=12), make_update(user_id=42, chat_type="group"), make_update(user_id=None)):
            for handler in (self.bot.add_user, self.bot.remove_user, self.bot.remove_all, self.bot.users):
                await handler(update, ctx)
            update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.bot.access.list_users(), ([(12, None)], []))

    async def test_remove_user_and_all_cancel_albums_and_invitations(self):
        admin = make_update(user_id=42)
        user = make_update(username="original_user", photos=[SimpleNamespace(file_id="1")], group="album")
        await self.bot.photo(user, self.context)
        self.assertTrue(self.bot.albums)
        await self.bot.add_user(admin, SimpleNamespace(args=["@new_user"]))
        await self.bot.remove_user(admin, SimpleNamespace(args=["@ORIGINAL_USER"]))
        self.assertFalse(self.bot.access.contains(12))
        self.assertFalse(self.bot.albums)
        await self.bot.remove_all(admin, SimpleNamespace(args=[]))
        self.assertEqual(self.bot.access.list_users(), ([], []))
        await self.bot.start(admin, self.context)
        self.assertTrue(self.bot.authorized(admin))
        self.assertFalse(self.bot.authorized(user))

    async def test_remove_pending_invitation_and_prevent_activation(self):
        admin = make_update(user_id=42)
        await self.bot.add_user(admin, SimpleNamespace(args=["@new_user"]))
        await self.bot.remove_user(admin, SimpleNamespace(args=["@NEW_USER"]))
        self.assertFalse(self.bot.authorized(make_update(user_id=99, username="new_user")))
        self.assertEqual(self.bot.access.list_users()[1], [])

    async def test_revocation_during_ocr_suppresses_reply(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def ocr(*_):
            started.set()
            await finish.wait()
            return ["Question"]

        self.context.bot.get_file.return_value = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=b"x")
        )
        user = make_update(photos=[SimpleNamespace(file_id="1", file_size=1)])
        with patch("telegram_bot.extract_questions", new=ocr):
            task = asyncio.create_task(self.bot.photo(user, self.context))
            await started.wait()
            await self.bot.remove_all(make_update(user_id=42), SimpleNamespace(args=[]))
            finish.set()
            await task
        user.effective_message.reply_text.assert_not_awaited()
        self.bot.service.answer.assert_not_awaited()

    async def test_invalid_commands_and_admin_cannot_be_removed(self):
        admin = make_update(user_id=42)
        for value in ("x", "@bad-name", "@Kelvin_name"):
            await self.bot.add_user(admin, SimpleNamespace(args=[value]))
            await self.bot.remove_user(admin, SimpleNamespace(args=[value]))
        await self.bot.remove_user(admin, SimpleNamespace(args=["999999999999999999999"]))
        await self.bot.remove_user(admin, SimpleNamespace(args=["42"]))
        self.assertIn("Нельзя", admin.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.bot.access.list_users(), ([(12, None)], []))

    async def test_revocation_while_answer_is_running_suppresses_reply(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def answer(_):
            started.set()
            await finish.wait()
            return "Ответ"

        self.bot.service.answer.side_effect = answer
        user = make_update()
        task = asyncio.create_task(self.bot.text(user, self.context))
        await started.wait()
        await self.bot.remove_user(make_update(user_id=42), SimpleNamespace(args=["12"]))
        finish.set()
        await task
        user.effective_message.reply_text.assert_not_awaited()

    async def test_state_persists_and_env_ids_not_readded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "access.sqlite3")
            config = BotConfig("token", frozenset({12}), "vision", 42, path)
            first = PrivateBot(config)
            admin = make_update(user_id=42)
            await first.add_user(admin, SimpleNamespace(args=["@new_user"]))
            await first.remove_user(admin, SimpleNamespace(args=["12"]))
            first.access.close()
            second = PrivateBot(config)
            try:
                self.assertFalse(second.access.contains(12))
                self.assertEqual(second.access.list_users()[1], ["new_user"])
                self.assertTrue(second.authorized(make_update(user_id=99, username="NEW_USER")))
            finally:
                second.access.close()

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

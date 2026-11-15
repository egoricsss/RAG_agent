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

from access_store import AccessStore, normalize_username
from agent import DEFAULT_CHAT_MODEL, ExamAgent

logger = logging.getLogger(__name__)
ALBUM_DELAY = 2.0
MAX_PHOTOS = 10  # Telegram albums contain at most 10 items.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 4096
MAX_USER_ID = 2**63 - 1


def valid_user_id(value: str) -> bool:
    return (value.isascii() and value.isdigit() and len(value) <= 19
            and 0 < int(value) <= MAX_USER_ID)


@dataclass(frozen=True)
class BotConfig:
    token: str
    allowed_ids: frozenset[int]
    vision_model: str
    super_admin_id: int
    access_db_path: str = "telegram_access.sqlite3"

    @classmethod
    def from_env(cls) -> BotConfig:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN.")
        admin = os.getenv("TELEGRAM_SUPER_ADMIN_ID", "").strip()
        if not valid_user_id(admin):
            raise ValueError("Укажите положительный TELEGRAM_SUPER_ADMIN_ID.")
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        parts = raw_ids.split(",") if raw_ids else []
        if not all(valid_user_id(part.strip()) for part in parts):
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS должен содержать ID через запятую."
            )
        ids = frozenset(int(part.strip()) for part in parts)
        model = os.getenv("OPENROUTER_VISION_MODEL", DEFAULT_CHAT_MODEL).strip()
        if not model:
            raise ValueError("Укажите OPENROUTER_VISION_MODEL.")
        path = os.getenv("TELEGRAM_ACCESS_DB_PATH", "telegram_access.sqlite3").strip()
        if not path:
            raise ValueError("Укажите TELEGRAM_ACCESS_DB_PATH.")
        return cls(token, ids, model, int(admin), path)


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
        self.access = AccessStore(config.access_db_path, config.allowed_ids - {config.super_admin_id})
        self.service: ExamAgent | None = None
        self.albums: dict[tuple[int, int, str], Album] = {}
        self.tasks: set[asyncio.Task] = set()

    def authorized(self, update: Update) -> bool:
        if not self.private_update(update):
            return False
        user = update.effective_user
        return user.id == self.config.super_admin_id or self.access.authorize(
            user.id, getattr(user, "username", None)
        )

    @staticmethod
    def private_update(update: Update) -> bool:
        return bool(update.effective_user and update.effective_chat
                    and update.effective_chat.type == "private" and update.effective_message)

    def is_admin(self, update: Update) -> bool:
        return self.private_update(update) and update.effective_user.id == self.config.super_admin_id

    def still_authorized(self, message) -> bool:
        user = message.from_user
        return bool(user and (user.id == self.config.super_admin_id or self.access.contains(user.id)))

    def cancel_albums(self, user_ids: set[int] | None = None) -> None:
        for key, album in list(self.albums.items()):
            if user_ids is None or key[1] in user_ids:
                if album.task:
                    album.task.cancel()
                del self.albums[key]

    async def add_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        args = context.args
        username = normalize_username(args[0]) if len(args) == 1 else None
        if not username:
            await update.effective_message.reply_text("Использование: /add_user @username")
            return
        if self.access.invite(username):
            text = f"Приглашение для @{username} создано. Пользователь должен написать боту /start."
        else:
            text = f"@{username} уже имеет доступ."
        await update.effective_message.reply_text(text)

    async def remove_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        args = context.args
        if len(args) != 1 or (
            not valid_user_id(args[0])
            and (not normalize_username(args[0]) or (args[0].isascii() and args[0].isdigit()))
        ):
            await update.effective_message.reply_text("Использование: /remove_user @username или ID")
            return
        if valid_user_id(args[0]) and int(args[0]) == self.config.super_admin_id:
            await update.effective_message.reply_text("Нельзя отключить супер-администратора.")
            return
        ids, removed = self.access.remove(args[0])
        self.cancel_albums(set(ids))
        await update.effective_message.reply_text("Доступ отозван." if removed else "Пользователь или приглашение не найдены.")

    async def remove_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        if context.args:
            await update.effective_message.reply_text("Использование: /remove_all")
            return
        self.access.remove_all()
        self.cancel_albums({key[1] for key in self.albums if key[1] != self.config.super_admin_id})
        await update.effective_message.reply_text("Доступ всех пользователей и приглашения удалены. Супер-администратор сохраняет доступ.")

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            return
        users, pending = self.access.list_users()
        lines = ["Пользователи:"] + [f"{id_}" + (f" @{name}" if name else "") for id_, name in users]
        lines += ["Приглашения:"] + [f"@{name}" for name in pending]
        for part in split_message("\n".join(lines)):
            await update.effective_message.reply_text(part)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        await update.effective_message.reply_text(
            "Отправьте вопрос текстом или фото теста. Несколько фото можно отправить одним альбомом."
            + ("\nКоманды: /add_user @username, /remove_user @username или ID, /remove_all, /users." if self.is_admin(update) else "")
        )

    async def send_answer(self, message, question: str) -> None:
        if not self.still_authorized(message):
            return
        assert self.service is not None
        answer = await self.service.answer(question)
        for part in split_message(answer):
            if not self.still_authorized(message):
                return
            await message.reply_text(part)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        try:
            await self.send_answer(message, message.text)
        except Exception as exc:
            logger.error("Text request failed: %s", type(exc).__name__)
            if not self.still_authorized(message):
                return
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
            if not self.still_authorized(message):
                return
            if len(messages) > MAX_PHOTOS:
                raise ValueError("Слишком много фотографий в альбоме.")
            images = []
            for item in sorted(messages, key=lambda item: item.message_id):
                if not self.still_authorized(message):
                    return
                photo = item.photo[-1]
                if photo.file_size is not None and photo.file_size > MAX_PHOTO_BYTES:
                    raise ValueError("Фотография слишком большая (максимум 10 МиБ).")
                file = await context.bot.get_file(photo.file_id)
                data = await file.download_as_bytearray()
                if len(data) > MAX_PHOTO_BYTES:
                    raise ValueError("Фотография слишком большая (максимум 10 МиБ).")
                images.append(bytes(data))
            assert self.service is not None
            if not self.still_authorized(message):
                return
            questions = await extract_questions(
                self.service, self.config.vision_model, images
            )
            if not self.still_authorized(message):
                return
            if not questions:
                await message.reply_text(
                    "Не удалось найти задания на фото. Пришлите более чёткое фото или текст."
                )
                return
            caption = "\n".join(item.caption for item in messages if item.caption)
            for index, question in enumerate(questions, 1):
                if not self.still_authorized(message):
                    return
                # Show the transcription so the user can catch OCR mistakes.
                heading = f"Задание {index}:\n{question}"
                for part in split_message(heading):
                    if not self.still_authorized(message):
                        return
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
                    if not self.still_authorized(message):
                        return
                    await message.reply_text(
                        "Не удалось ответить на это задание. Попробуйте отправить его текстом."
                    )
        except ValueError as exc:
            logger.warning("Photo request rejected: %s", type(exc).__name__)
            if not self.still_authorized(message):
                return
            await message.reply_text(
                "Не удалось прочитать фото. Проверьте качество и размер (до 10 МиБ на фото) и попробуйте ещё раз."
            )
        except Exception as exc:
            logger.error("Photo request failed: %s", type(exc).__name__)
            if not self.still_authorized(message):
                return
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
        self.access.close()


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
    application.add_handler(CommandHandler("add_user", bot.add_user))
    application.add_handler(CommandHandler("remove_user", bot.remove_user))
    application.add_handler(CommandHandler("remove_all", bot.remove_all))
    application.add_handler(CommandHandler("users", bot.users))
    application.add_handler(MessageHandler(filters.PHOTO, bot.photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()

import asyncio
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)


DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))
router = Router()

BTN_LINK = "🔗 Моя ссылка"
BTN_HELP = "ℹ️ Как это работает"
BTN_CANCEL = "❌ Отменить отправку"

ALLOWED_CONTENT_TYPES = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.ANIMATION,
    ContentType.AUDIO,
    ContentType.VOICE,
    ContentType.VIDEO_NOTE,
    ContentType.DOCUMENT,
    ContentType.STICKER,
}


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                anon_code TEXT NOT NULL UNIQUE,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_targets (
                sender_id INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )
        self.connection.commit()

    def get_or_create_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
    ) -> str:
        row = self.connection.execute(
            "SELECT anon_code FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row is None:
            while True:
                anon_code = secrets.token_urlsafe(8)
                try:
                    self.connection.execute(
                        """
                        INSERT INTO users (user_id, anon_code, username, first_name)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, anon_code, username, first_name),
                    )
                    self.connection.commit()
                    return anon_code
                except sqlite3.IntegrityError:
                    continue

        anon_code = str(row[0])
        self.connection.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (username, first_name, user_id),
        )
        self.connection.commit()
        return anon_code

    def get_user_id_by_code(self, anon_code: str) -> Optional[int]:
        row = self.connection.execute(
            "SELECT user_id FROM users WHERE anon_code = ?", (anon_code,)
        ).fetchone()
        return int(row[0]) if row else None

    def set_pending_target(self, sender_id: int, target_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_targets (sender_id, target_id)
            VALUES (?, ?)
            ON CONFLICT(sender_id) DO UPDATE SET
                target_id = excluded.target_id,
                created_at = CURRENT_TIMESTAMP
            """,
            (sender_id, target_id),
        )
        self.connection.commit()

    def get_pending_target(self, sender_id: int) -> Optional[int]:
        row = self.connection.execute(
            "SELECT target_id FROM pending_targets WHERE sender_id = ?", (sender_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def clear_pending_target(self, sender_id: int) -> None:
        self.connection.execute(
            "DELETE FROM pending_targets WHERE sender_id = ?", (sender_id,)
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


db = Database(DB_PATH)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LINK)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие 👇",
    )


def send_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder="Напиши сообщение или отправь медиа 💌",
    )


def share_keyboard(link: str) -> InlineKeyboardMarkup:
    share_url = "https://t.me/share/url?" + urlencode(
        {
            "url": link,
            "text": "💌 Отправь мне анонимное сообщение",
        }
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)]
        ]
    )


def register_user(message: Message) -> Optional[str]:
    user = message.from_user
    if user is None:
        return None
    return db.get_or_create_user(user.id, user.username, user.first_name)


async def build_link(bot: Bot, anon_code: str) -> str:
    me = await bot.get_me()
    if not me.username:
        raise RuntimeError("Bot username is not available")
    return f"https://t.me/{me.username}?start=send_{anon_code}"


async def send_personal_link(message: Message, bot: Bot) -> None:
    anon_code = register_user(message)
    if anon_code is None:
        return

    link = await build_link(bot, anon_code)
    await message.answer(
        "🔗 Твоя персональная ссылка:\n\n"
        f"{link}\n\n"
        "📤 Поделись ей с друзьями — по ней можно отправить тебе "
        "анонимный текст или медиа.",
        reply_markup=share_keyboard(link),
    )


@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    anon_code = register_user(message)
    if anon_code is None:
        return

    payload = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()

    if payload.startswith("send_"):
        target_code = payload.removeprefix("send_")
        target_id = db.get_user_id_by_code(target_code)

        if target_id is None:
            await message.answer(
                "⚠️ Эта ссылка недействительна или устарела.",
                reply_markup=main_keyboard(),
            )
            return

        if target_id == message.from_user.id:
            await message.answer(
                "😄 Себе анонимное сообщение отправить не получится.\n"
                "Лучше поделись своей ссылкой с друзьями 👇",
                reply_markup=main_keyboard(),
            )
            await send_personal_link(message, bot)
            return

        db.set_pending_target(message.from_user.id, target_id)
        await message.answer(
            "💌 Отправь анонимное сообщение\n\n"
            "Можно отправить:\n"
            "📝 текст\n"
            "🖼 фото и GIF\n"
            "🎬 видео и кружок\n"
            "🎤 голосовое и аудио\n"
            "📎 документ\n"
            "✨ стикер\n\n"
            "Получатель не увидит, кто это отправил.",
            reply_markup=send_keyboard(),
        )
        return

    db.clear_pending_target(message.from_user.id)
    await message.answer(
        "👋 Привет! Здесь можно получать анонимные сообщения.\n\n"
        "💬 Друзья переходят по твоей персональной ссылке и отправляют "
        "текст или медиа — без имени отправителя.\n\n"
        "👇 Используй кнопки ниже:",
        reply_markup=main_keyboard(),
    )
    await send_personal_link(message, bot)


@router.message(Command("link"))
@router.message(F.text == BTN_LINK)
async def link_handler(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    db.clear_pending_target(message.from_user.id)
    await send_personal_link(message, bot)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def help_handler(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    register_user(message)
    db.clear_pending_target(message.from_user.id)
    await message.answer(
        "ℹ️ Как это работает\n\n"
        "1️⃣ Нажми «🔗 Моя ссылка».\n"
        "2️⃣ Поделись ссылкой с друзьями.\n"
        "3️⃣ Друг открывает её и отправляет текст или медиа.\n"
        "4️⃣ Ты получаешь сообщение без имени отправителя. 🕵️\n\n"
        "Поддерживаются текст, фото, GIF, видео, кружки, голосовые, "
        "аудио, документы и стикеры.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel_handler(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    register_user(message)
    db.clear_pending_target(message.from_user.id)
    await message.answer(
        "✅ Отправка отменена.",
        reply_markup=main_keyboard(),
    )


@router.message(F.chat.type == ChatType.PRIVATE)
async def anonymous_message_handler(message: Message) -> None:
    if message.from_user is None:
        return

    register_user(message)

    if message.text and message.text.startswith("/"):
        await message.answer(
            "🤔 Не знаю такую команду.\n"
            "Используй /start, /link, /help или /cancel.",
            reply_markup=main_keyboard(),
        )
        return

    target_id = db.get_pending_target(message.from_user.id)
    if target_id is None:
        await message.answer(
            "💡 Чтобы отправить кому-то анонимное сообщение, "
            "открой его персональную ссылку.\n\n"
            "А свою ссылку можно получить кнопкой «🔗 Моя ссылка».",
            reply_markup=main_keyboard(),
        )
        return

    if message.content_type not in ALLOWED_CONTENT_TYPES:
        await message.answer(
            "⚠️ Такой тип сообщения пока не поддерживается.\n\n"
            "Отправь текст, фото, GIF, видео, кружок, голосовое, "
            "аудио, документ или стикер.",
            reply_markup=send_keyboard(),
        )
        return

    try:
        await message.copy_to(chat_id=target_id)
        await message.bot.send_message(
            chat_id=target_id,
            text="💌 Новое анонимное сообщение",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        db.clear_pending_target(message.from_user.id)
        await message.answer(
            "😕 Не удалось доставить сообщение.\n"
            "Возможно, получатель заблокировал бота.",
            reply_markup=main_keyboard(),
        )
        return

    db.clear_pending_target(message.from_user.id)
    await message.answer(
        "✅ Готово! Сообщение отправлено анонимно 💌",
        reply_markup=main_keyboard(),
    )


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Environment variable BOT_TOKEN is required")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

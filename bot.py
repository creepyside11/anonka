import asyncio
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message


DB_PATH = Path(os.getenv("DB_PATH", "bot.db"))
router = Router()


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
            await message.answer("Ссылка недействительна или устарела.")
            return

        if target_id == message.from_user.id:
            await message.answer("Нельзя отправить анонимное сообщение самому себе.")
            return

        db.set_pending_target(message.from_user.id, target_id)
        await message.answer(
            "Отправь одним сообщением текст, фото, видео, голосовое или другой файл. "
            "Получатель не увидит, кто его отправил.\n\n"
            "Чтобы отменить отправку: /cancel"
        )
        return

    db.clear_pending_target(message.from_user.id)
    link = await build_link(bot, anon_code)
    await message.answer(
        "Привет! Это бот для анонимных сообщений.\n\n"
        "Твоя персональная ссылка:\n"
        f"{link}\n\n"
        "Поделись ей — любой, кто откроет ссылку, сможет отправить тебе сообщение анонимно.\n"
        "Получить ссылку снова: /link"
    )


@router.message(Command("link"))
async def link_handler(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    anon_code = register_user(message)
    if anon_code is None:
        return

    link = await build_link(bot, anon_code)
    await message.answer(f"Твоя ссылка для анонимных сообщений:\n{link}")


@router.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    if message.from_user is None:
        return

    register_user(message)
    db.clear_pending_target(message.from_user.id)
    await message.answer("Отправка отменена.")


@router.message(F.chat.type == ChatType.PRIVATE)
async def anonymous_message_handler(message: Message) -> None:
    if message.from_user is None:
        return

    register_user(message)

    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Используй /start, /link или /cancel.")
        return

    target_id = db.get_pending_target(message.from_user.id)
    if target_id is None:
        await message.answer(
            "Чтобы отправить анонимное сообщение, открой персональную ссылку получателя.\n"
            "Свою ссылку можно получить командой /link."
        )
        return

    try:
        await message.copy_to(chat_id=target_id)
        await message.bot.send_message(
            chat_id=target_id,
            text="↑ Новое анонимное сообщение",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer("Не удалось доставить сообщение получателю.")
        db.clear_pending_target(message.from_user.id)
        return

    db.clear_pending_target(message.from_user.id)
    await message.answer("Сообщение отправлено анонимно ✅")


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

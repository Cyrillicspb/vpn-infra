#!/usr/bin/env python3
"""
bot.py — Telegram-бот VPN Infrastructure v4.0

Двухрежимный бот:
- Администратор: полный доступ к управлению
- Клиенты: самообслуживание (устройства, конфиги)
- Незарегистрированные: игнор (кроме /start)

Использует: aiogram 3.x, SQLite (WAL), watchdog HTTP API
"""
import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import Database
from handlers.admin import router as admin_router
from handlers.client import router as client_router
from handlers.alerts import router as alerts_router
from handlers.requests import router as requests_router

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------
bot = Bot(
    token=config.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)

dp = Dispatcher(storage=MemoryStorage())

# Подключаем роутеры
dp.include_router(admin_router)
dp.include_router(client_router)
dp.include_router(alerts_router)
dp.include_router(requests_router)

# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
async def on_startup():
    """Инициализация при старте."""
    logger.info("Telegram-бот запускается...")

    # Инициализируем базу данных
    db = Database(config.db_path)
    await db.init()

    # Сохраняем в dp для доступа из хендлеров
    dp["db"] = db
    dp["config"] = config

    # Авторегистрация администратора
    if config.admin_chat_id:
        existing = await db.get_client_by_chat_id(config.admin_chat_id)
        if not existing:
            await db.register_admin(config.admin_chat_id)
            logger.info(f"Администратор зарегистрирован: {config.admin_chat_id}")

    # Уведомляем администратора о запуске
    try:
        await bot.send_message(config.admin_chat_id, "✅ *Бот запущен* и готов к работе.")
    except Exception as e:
        logger.warning(f"Не удалось уведомить администратора: {e}")

    logger.info("Бот готов к работе")


async def on_shutdown():
    """Завершение работы."""
    logger.info("Бот завершается...")
    await bot.session.close()


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
async def main():
    # Регистрируем обработчики startup/shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info(f"Запуск polling (admin_chat_id={config.admin_chat_id})")

    # Удаляем webhook если был
    await bot.delete_webhook(drop_pending_updates=True)

    # Запускаем polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

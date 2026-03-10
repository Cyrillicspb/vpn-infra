"""
handlers/alerts.py — Обработка входящих алертов от watchdog

Watchdog отправляет алерты через HTTP POST /notify → бот рассылает клиентам.
"""
import logging
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

logger = logging.getLogger(__name__)
router = Router()

# Алерты приходят не через команды, а через сервис alerts
# Этот модуль содержит хелперы для отправки алертов клиентам


async def send_alert_to_admin(bot, admin_chat_id: str, message: str):
    """Отправка алерта администратору."""
    try:
        await bot.send_message(admin_chat_id, f"🚨 *Алерт*\n\n{message}")
    except Exception as e:
        logger.error(f"Не удалось отправить алерт: {e}")


async def send_alert_to_all_clients(bot, db, message: str):
    """Уведомление всех клиентов (например, все стеки down)."""
    try:
        clients = await db.get_all_clients()
        for client in clients:
            if not client.get("is_disabled"):
                try:
                    await bot.send_message(client["chat_id"], f"⚠️ {message}")
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Ошибка рассылки алерта: {e}")

"""
handlers/requests.py — Обработка запросов клиентов на модерации

Запросы /request vpn|direct создаются в client.py
Одобрение/отклонение — в admin.py через inline кнопки
"""
import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from database import Database

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("myrequests"))
async def cmd_myrequests(message: Message, **kwargs):
    """Показать запросы текущего пользователя."""
    db: Database = kwargs.get("db")
    if not db:
        return

    client = await db.get_client_by_chat_id(str(message.from_user.id))
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    conn = db._get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM domain_requests
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT 20
        """, (str(message.from_user.id),)).fetchall()
    finally:
        conn.close()

    if not rows:
        await message.answer("У вас нет запросов.")
        return

    status_emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    lines = ["*Ваши запросы:*\n"]
    for row in rows:
        emoji = status_emoji.get(row["status"], "❓")
        lines.append(
            f"{emoji} `{row['domain']}` ({row['direction']}) — {row['status']}"
        )

    await message.answer("\n".join(lines))

"""
handlers/alerts.py — Внешний HTTP-сервер для приёма алертов от watchdog

Watchdog вызывает POST /notify → бот рассылает сообщение клиентам.
Запускается в отдельной asyncio-задаче при старте бота.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from aiogram import Bot
    from database import Database

logger = logging.getLogger(__name__)

NOTIFY_PORT = 8090
router = None   # не используется как aiogram Router


async def _handle_notify(request: web.Request) -> web.Response:
    """POST /notify — рассылка уведомления клиентам."""
    bot: "Bot"      = request.app["bot"]
    db: "Database"  = request.app["db"]

    try:
        data    = await request.json()
        message = data.get("message", "")
        target  = data.get("target", "admin")  # "admin" | "all" | chat_id
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    if target == "admin":
        from config import config
        try:
            await bot.send_message(config.admin_chat_id, message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning(f"Алерт→admin: {exc}")

    elif target == "all":
        clients = await db.get_all_clients()
        sent = 0
        for c in clients:
            if not c.get("is_disabled"):
                try:
                    await bot.send_message(c["chat_id"], message, parse_mode="Markdown")
                    sent += 1
                except Exception:
                    pass
        logger.info(f"Broadcast алерт: {sent} клиентам")

    else:
        # Конкретный chat_id
        try:
            await bot.send_message(str(target), message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning(f"Алерт→{target}: {exc}")

    return web.json_response({"status": "ok"})


async def start_notify_server(bot: "Bot", db: "Database") -> None:
    """Запускает лёгкий HTTP-сервер для приёма алертов от watchdog."""
    app = web.Application()
    app["bot"] = bot
    app["db"]  = db
    app.router.add_post("/notify", _handle_notify)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", NOTIFY_PORT)
    await site.start()
    logger.info(f"Notify server запущен на 127.0.0.1:{NOTIFY_PORT}")

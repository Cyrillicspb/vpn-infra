"""
handlers/alerts.py — HTTP-сервер для приёма алертов

Два источника:
  1. watchdog.py  → POST /notify  {"message": "...", "target": "admin"|"all"|chat_id}
  2. Alertmanager → POST /notify  Prometheus webhook JSON (version="4", ключ "alerts")

Аутентификация: Authorization: Bearer <WATCHDOG_API_TOKEN>
Привязка: 0.0.0.0:8090 (доступ ограничен nftables — только 127.0.0.1 и 10.177.2.0/30)
"""
from __future__ import annotations

import asyncio
import logging
import os
from hmac import compare_digest
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from aiogram import Bot
    from database import Database

logger = logging.getLogger(__name__)

NOTIFY_PORT  = 8090
NOTIFY_TOKEN = os.getenv("WATCHDOG_API_TOKEN", "")
router = None  # не используется как aiogram Router

_SEVERITY_ICON = {
    "critical": "🔴",
    "warning":  "⚠️",
    "info":     "ℹ️",
}


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

def _check_auth(request: web.Request) -> bool:
    """Проверяет Bearer-токен. Если токен не задан — пропускаем (не production)."""
    if not NOTIFY_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return compare_digest(auth, f"Bearer {NOTIFY_TOKEN}")


# ---------------------------------------------------------------------------
# Форматирование Alertmanager webhook → Telegram текст
# ---------------------------------------------------------------------------

def _format_alertmanager(data: dict) -> str | None:
    """
    Преобразует Prometheus Alertmanager webhook JSON в Telegram Markdown.

    Формат входящего JSON (version=4):
      {
        "status":  "firing" | "resolved",
        "alerts":  [{"status": ..., "labels": {...}, "annotations": {...}}, ...],
        ...
      }

    Возвращает None если список alerts пустой.
    """
    alerts = data.get("alerts", [])
    if not alerts:
        return None

    overall_status = data.get("status", "firing")
    resolved = overall_status == "resolved"

    lines: list[str] = []
    for alert in alerts:
        a_status   = alert.get("status", overall_status)
        labels     = alert.get("labels", {})
        ann        = alert.get("annotations", {})
        severity   = labels.get("severity", "warning")
        alertname  = labels.get("alertname", "Alert")

        icon = "✅" if a_status == "resolved" else _SEVERITY_ICON.get(severity, "⚠️")
        summary     = ann.get("summary", alertname)
        description = ann.get("description", "")

        text = f"{icon} *{summary}*"
        if description:
            text += f"\n{description}"
        lines.append(text)

    if not lines:
        return None

    header = "🟢 *RESOLVED*\n\n" if resolved else ""
    return header + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Обработчик POST /notify
# ---------------------------------------------------------------------------

async def _handle_notify(request: web.Request) -> web.Response:
    """POST /notify — принимает алерты от watchdog и Alertmanager."""
    if not _check_auth(request):
        logger.warning("notify: неверный Bearer-токен от %s", request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)

    bot: "Bot"     = request.app["bot"]
    db:  "Database" = request.app["db"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    # ── Формат Alertmanager (есть ключ "alerts") ──────────────────────────────
    if "alerts" in data:
        message = _format_alertmanager(data)
        if not message:
            return web.json_response({"status": "ok", "note": "empty alerts list"})

        from config import config
        try:
            await bot.send_message(config.admin_chat_id, message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Alertmanager алерт→admin: %s", exc)
        return web.json_response({"status": "ok"})

    # ── Формат watchdog {"message": "...", "target": "..."} ───────────────────
    message = data.get("message", "")
    target  = data.get("target", "admin")

    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    if target == "admin":
        from config import config
        try:
            await bot.send_message(config.admin_chat_id, message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Алерт→admin: %s", exc)

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
        logger.info("Broadcast алерт: %d клиентам", sent)

    else:
        try:
            await bot.send_message(str(target), message, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Алерт→%s: %s", target, exc)

    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Запуск сервера
# ---------------------------------------------------------------------------

async def start_notify_server(bot: "Bot", db: "Database") -> None:
    """
    Запускает HTTP-сервер для приёма алертов.

    Слушает 0.0.0.0:8090.
    Доступ ограничен nftables (только 127.0.0.1 + 172.20.0.0/24 Docker + 10.177.2.0/30 Tier-2).
    Alertmanager теперь локальный Docker-контейнер → приходит с 172.20.x.x.
    """
    app = web.Application()
    app["bot"] = bot
    app["db"]  = db
    app.router.add_post("/notify", _handle_notify)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", NOTIFY_PORT)
    await site.start()
    logger.info("Notify server запущен на 0.0.0.0:%d", NOTIFY_PORT)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Notify server shutting down")
        await runner.cleanup()
        raise

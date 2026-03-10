"""
handlers/requests.py — Уведомления клиентам об итогах модерации

Этот модуль экспортирует:
  - router (пустой — сама логика создания/показа запросов в client.py,
    кнопки одобрения/отклонения — в admin.py)
  - notify_* — async хелперы, которые admin.py вызывает из callback-хендлеров

Разделение ответственности:
  client.py  →  /request, /myrequests (создание, просмотр своих)
  admin.py   →  /requests, cb_dev_approve/reject, cb_req_approve/reject (модерация)
  requests.py →  уведомить клиента об итоге (одобрено / отклонено) + helpers
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Router

if TYPE_CHECKING:
    from aiogram import Bot
    from database import Database

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Уведомления клиенту — устройства
# ---------------------------------------------------------------------------

async def notify_device_approved(
    bot: "Bot",
    db: "Database",
    device: dict,
    autodist=None,
) -> None:
    """
    Уведомить клиента что его устройство одобрено и отправить конфиг.

    device — dict возвращённый db.approve_device (содержит chat_id).
    """
    chat_id = str(device.get("chat_id", ""))
    name    = device.get("device_name", "?")

    if not chat_id:
        logger.warning("notify_device_approved: нет chat_id в device=%s", device)
        return

    try:
        await bot.send_message(
            chat_id,
            f"✅ Устройство `{name}` *одобрено*!\n\n"
            f"Конфигурация отправляется...",
        )
    except Exception as exc:
        logger.warning("notify_device_approved: send_message failed: %s", exc)

    # Отправляем конфиг через autodist
    if autodist:
        try:
            await autodist.send_to_device(chat_id, device, "Устройство одобрено")
        except Exception as exc:
            logger.error("notify_device_approved: autodist failed: %s", exc)
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Не удалось отправить конфиг автоматически.\n"
                    f"Используйте /myconfig {name}",
                )
            except Exception:
                pass


async def notify_device_rejected(
    bot: "Bot",
    chat_id: str,
    device_name: str,
    reason: str | None = None,
) -> None:
    """Уведомить клиента что его запрос на устройство отклонён."""
    text = f"❌ Запрос на устройство `{device_name}` *отклонён*."
    if reason:
        text += f"\n\nПричина: {reason}"
    text += "\n\nОбратитесь к администратору если считаете это ошибкой."
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:
        logger.warning("notify_device_rejected: %s", exc)


# ---------------------------------------------------------------------------
# Уведомления клиенту — доменные запросы
# ---------------------------------------------------------------------------

async def notify_request_approved(
    bot: "Bot",
    req: dict,
    autodist=None,
) -> None:
    """
    Уведомить клиента что его запрос на домен одобрен.
    После одобрения маршруты обновятся и придут новые конфиги.

    req — dict из db.approve_request (содержит chat_id, domain, direction).
    """
    chat_id   = str(req.get("chat_id", ""))
    domain    = req.get("domain", "?")
    direction = req.get("direction", "vpn")
    req_id    = req.get("id", "?")

    if not chat_id:
        logger.warning("notify_request_approved: нет chat_id в req=%s", req)
        return

    icon = "🔒" if direction == "vpn" else "🌐"
    try:
        await bot.send_message(
            chat_id,
            f"{icon} Запрос #{req_id} *одобрен*!\n\n"
            f"Домен: `{domain}`\n"
            f"Маршрут: `{direction}`\n\n"
            f"Маршруты обновляются. Новый конфиг придёт автоматически.",
        )
    except Exception as exc:
        logger.warning("notify_request_approved: send_message failed: %s", exc)

    # Триггерим обновление конфигов через autodist (debounce 5 мин)
    if autodist:
        try:
            autodist.trigger(f"approved request: {domain} ({direction})")
        except Exception as exc:
            logger.warning("notify_request_approved: autodist.trigger: %s", exc)


async def notify_request_rejected(
    bot: "Bot",
    req: dict,
    reason: str | None = None,
) -> None:
    """Уведомить клиента что его доменный запрос отклонён."""
    chat_id   = str(req.get("chat_id", ""))
    domain    = req.get("domain", "?")
    direction = req.get("direction", "vpn")
    req_id    = req.get("id", "?")

    if not chat_id:
        return

    text = (
        f"❌ Запрос #{req_id} на `{domain}` ({direction}) *отклонён*."
    )
    if reason:
        text += f"\n\nПричина: {reason}"
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:
        logger.warning("notify_request_rejected: %s", exc)


# ---------------------------------------------------------------------------
# Хелпер: безопасный ответ на callback (edit или fallback send)
# ---------------------------------------------------------------------------

async def safe_edit(cb, text: str) -> None:
    """Редактирует сообщение с кнопками или отправляет новое если не удалось."""
    try:
        await cb.message.edit_text(text)
    except Exception:
        try:
            await cb.message.answer(text)
        except Exception:
            pass
    try:
        await cb.answer()
    except Exception:
        pass

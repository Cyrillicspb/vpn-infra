"""
services/autodist.py — Авторассылка конфигов клиентам

Триггеры:
  1. Изменение баз маршрутов (cron 03:00, diff)
  2. /routes update
  3. Одобрение /request vpn (добавление /24 подсети IP домена)
  4. Смена внешнего IP (только если DDNS НЕ настроен)
  5. /migrate-vps
  6. /vpn add (автоматически запускает обновление)

Debounce 5 мин: множественные изменения → один финальный выпуск.
Группировка: все устройства одного клиента в одном сообщении.
Напоминание через 24 ч если клиент не обновил (config_sent_at старый).
"""
from __future__ import annotations

import asyncio
import io
import logging

from aiogram.types import BufferedInputFile
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiogram import Bot
    from database import Database
    from services.config_builder import ConfigBuilder

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 300   # 5 минут
REMINDER_HOURS   = 24


class AutoDist:
    def __init__(self, bot: "Bot", db: "Database", builder: "ConfigBuilder") -> None:
        self.bot     = bot
        self.db      = db
        self.builder = builder
        self._pending_task: Optional[asyncio.Task] = None

    def trigger(self, reason: str = "") -> None:
        """Запустить debounced рассылку."""
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()
        self._pending_task = asyncio.create_task(
            self._debounced_send(reason),
            name="autodist",
        )

    async def _debounced_send(self, reason: str) -> None:
        logger.info(f"AutoDist: ожидание {DEBOUNCE_SECONDS}с (причина: {reason})")
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self.send_all(reason)

    async def send_all(self, reason: str = "") -> tuple[int, int]:
        """
        Разослать обновлённые конфиги всем активным устройствам.
        Возвращает (отправлено_устройств, ошибок).
        """
        devices = await self.db.get_all_devices()
        if not devices:
            return 0, 0

        # Группируем по chat_id
        by_client: dict[str, list[dict]] = {}
        for d in devices:
            cid = d["chat_id"]
            by_client.setdefault(cid, []).append(d)

        sent = errors = 0
        for chat_id, client_devices in by_client.items():
            try:
                count = await self._send_to_client(chat_id, client_devices, reason)
                sent += count
            except Exception as exc:
                logger.error(f"AutoDist: ошибка для {chat_id}: {exc}")
                errors += 1

        logger.info(f"AutoDist: отправлено {sent} конфигов, ошибок {errors}")
        return sent, errors

    async def send_to_device(self, chat_id: str, device: dict, reason: str = "") -> bool:
        """Отправить конфиг одному устройству."""
        try:
            await self._send_one(chat_id, device, reason)
            return True
        except Exception as exc:
            logger.error(f"AutoDist send_to_device {device.get('device_name')}: {exc}")
            return False

    async def _send_to_client(
        self, chat_id: str, devices: list[dict], reason: str
    ) -> int:
        sent = 0
        for device in devices:
            try:
                await self._send_one(chat_id, device, reason)
                sent += 1
            except Exception as exc:
                logger.warning(f"Не удалось отправить {device['device_name']} → {chat_id}: {exc}")
        return sent

    async def _send_one(self, chat_id: str, device: dict, reason: str) -> None:
        excludes = await self.db.get_excludes(device["id"])

        # Обеспечиваем наличие ключей; сохраняем если были сгенерированы
        had_keys = bool(device.get("private_key"))
        device = await self.builder.ensure_keys(device)
        if not had_keys and device.get("private_key"):
            await self.db.update_device_keys(
                device["id"], device["private_key"], device["public_key"]
            )

        conf_text, qr_bytes, version = await self.builder.build(device, excludes)

        # Если конфиг не изменился — не отправляем
        if version == device.get("config_version"):
            return

        caption_reason = f"\nПричина: {reason}" if reason else ""

        # Предупреждение
        await self.bot.send_message(
            chat_id,
            f"⚠️ *Конфигурация содержит приватный ключ!*\n"
            f"Не передавайте никому. Рекомендуется 2FA на устройстве."
        )

        # QR-код
        if qr_bytes:
            await self.bot.send_photo(
                chat_id,
                photo=BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=f"QR-код для `{device['device_name']}`{caption_reason}",
            )

        # .conf файл
        if device.get("is_router"):
            _filename = f"vpn-{device['device_name']}.conf"
        else:
            _filename = f"{device['device_name']}_{date.today()}.conf"
        await self.bot.send_document(
            chat_id,
            document=BufferedInputFile(conf_text.encode(), filename=_filename),
            caption=f"Конфигурация `{device['device_name']}`{caption_reason}",
        )

        # Обновляем версию в БД
        await self.db.update_config_version(device["id"], version)

    async def send_reminders(self) -> None:
        """
        Напомнить клиентам которым конфиг отправлен > 24 ч назад
        и они не обновились (config_version не изменилась после отправки).
        """
        stale = await self.db.get_stale_configs(hours=REMINDER_HOURS)
        for device in stale:
            try:
                await self.bot.send_message(
                    device["chat_id"],
                    f"⏰ Напоминание: конфиг для `{device['device_name']}` обновился.\n"
                    f"Используйте /update для получения актуальной конфигурации.",
                )
            except Exception:
                pass

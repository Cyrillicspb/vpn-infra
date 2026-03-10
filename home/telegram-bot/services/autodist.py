"""
services/autodist.py — Авторассылка конфигов клиентам

Триггеры:
1. Изменение баз маршрутов (cron 03:00)
2. /routes update
3. Одобрение /request vpn (добавляет подсеть в AllowedIPs)
4. Смена внешнего IP (только если DDNS не настроен)
5. /migrate-vps

Дебаунс 5 минут: множественные изменения → один финальный конфиг.
Группировка: все устройства клиента в одном сообщении.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class AutoDistributor:
    def __init__(self, bot, db, config_builder, config):
        self.bot = bot
        self.db = db
        self.builder = config_builder
        self.config = config
        self._pending_clients: set = set()  # chat_id ожидающих рассылки
        self._debounce_task: Optional[asyncio.Task] = None
        self._debounce_seconds = config.config_debounce_seconds  # 300

    async def trigger_redistribution(self, reason: str = "routes_updated"):
        """
        Запуск рассылки конфигов с дебаунсом.
        Множественные вызовы в течение 5 мин → один финальный.
        """
        logger.info(f"Триггер рассылки: {reason}")

        # Добавляем всех клиентов в очередь
        clients = await self.db.get_all_clients()
        for client in clients:
            if not client.get("is_disabled"):
                self._pending_clients.add(client["chat_id"])

        # Перезапускаем дебаунс таймер
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        self._debounce_task = asyncio.create_task(
            self._debounced_send(reason)
        )

    async def _debounced_send(self, reason: str):
        """Ожидаем дебаунс период, затем отправляем."""
        await asyncio.sleep(self._debounce_seconds)
        await self._send_to_pending(reason)

    async def _send_to_pending(self, reason: str):
        """Рассылка конфигов всем ожидающим клиентам."""
        pending = list(self._pending_clients)
        self._pending_clients.clear()

        if not pending:
            return

        logger.info(f"Рассылка конфигов {len(pending)} клиентам (причина: {reason})")

        sent = 0
        for chat_id in pending:
            try:
                await self._send_to_client(chat_id, reason)
                sent += 1
                await asyncio.sleep(0.1)  # Не флудим Telegram
            except Exception as e:
                logger.error(f"Ошибка рассылки клиенту {chat_id}: {e}")

        logger.info(f"Рассылка завершена: {sent}/{len(pending)}")

    async def _send_to_client(self, chat_id: str, reason: str):
        """Отправка конфигов конкретному клиенту (все устройства в одном сообщении)."""
        devices = await self.db.get_devices_by_client(chat_id)
        if not devices:
            return

        configs_sent = []
        for device in devices:
            if device.get("pending_approval"):
                continue
            try:
                conf_text, qr_bytes = await self.builder.build(device)
                new_version = self.builder.config_version(conf_text)

                # Не отправляем если конфиг не изменился
                if device.get("config_version") == new_version:
                    continue

                configs_sent.append((device, conf_text, qr_bytes, new_version))
            except Exception as e:
                logger.error(f"Ошибка генерации конфига {device['device_name']}: {e}")

        if not configs_sent:
            return

        # Заголовок сообщения
        reason_text = {
            "routes_updated": "обновились маршруты РКН",
            "ip_changed": "изменился внешний IP сервера",
            "request_approved": "ваш запрос был одобрен",
            "vps_migrated": "VPS был мигрирован",
        }.get(reason, reason)

        await self.bot.send_message(
            chat_id,
            f"🔄 *Обновление конфигурации VPN*\n\n"
            f"Причина: {reason_text}\n"
            f"Установите новый конфиг на ваших устройствах.\n\n"
            f"⚠️ Конфиги содержат приватные ключи — не передавайте их!"
        )

        for device, conf_text, qr_bytes, new_version in configs_sent:
            if qr_bytes:
                await self.bot.send_photo(
                    chat_id,
                    qr_bytes,
                    caption=f"QR для `{device['device_name']}`"
                )

            import io
            await self.bot.send_document(
                chat_id,
                document=io.BytesIO(conf_text.encode()),
                filename=f"vpn-{device['device_name']}.conf",
                caption=f"Конфиг `{device['device_name']}`"
            )

            # Обновляем версию конфига в БД
            conn = self.db._get_connection()
            try:
                conn.execute(
                    "UPDATE devices SET config_version = ? WHERE id = ?",
                    (new_version, device["id"])
                )
                conn.commit()
            finally:
                conn.close()

        logger.info(f"Клиенту {chat_id} отправлено {len(configs_sent)} конфигов")

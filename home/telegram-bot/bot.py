#!/usr/bin/env python3
"""
bot.py — Telegram-бот VPN Infrastructure v4.0

Двухрежимный:
  - Администратор (ADMIN_CHAT_ID): полный доступ
  - Клиенты (зарегистрированные): самообслуживание
  - Незарегистрированные: игнор (кроме /start)

FSM middleware:
  - Таймаут 10 мин: при истечении очищает состояние и уведомляет
  - Любая команда при активном FSM → очистить FSM → выполнить команду
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update

from config import config
from database import Database
from handlers.admin import router as admin_router
from handlers.alerts import start_notify_server
from handlers.client import router as client_router
from handlers.requests import router as requests_router
from services.autodist import AutoDist
from services.config_builder import ConfigBuilder

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

FSM_TIMEOUT = config.fsm_timeout_minutes * 60   # 600 сек


# ---------------------------------------------------------------------------
# FSM Control Middleware
# ---------------------------------------------------------------------------
class FSMControlMiddleware:
    """
    Outer middleware (регистрируется на dp.update.outer_middleware).
    Выполняется ДО выбора хендлера — позволяет сбросить FSM
    перед тем как роутер начнёт матчинг.

    Поведение:
      1. Если пользователь в FSM И отправил команду → очистить FSM → дать хендлеру отработать
      2. Если пользователь в FSM И таймаут истёк → очистить FSM → уведомить → остановить
    """

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        message: Message | None = event.message
        if not message or not message.text:
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        if not state:
            return await handler(event, data)

        current = await state.get_state()
        if current is None:
            return await handler(event, data)

        # Есть активное FSM-состояние
        fsm_data = await state.get_data()
        last_ts  = fsm_data.get("_fsm_ts", 0.0)
        now      = time.time()

        # 1. Таймаут истёк
        if last_ts > 0 and now - last_ts > FSM_TIMEOUT:
            # Снимаем резерв invite-кода если он был
            db: Database | None = data.get("db")
            if db:
                await db.release_invite_reservation(str(message.from_user.id))
            await state.clear()
            if message.text.startswith("/"):
                # Команда — продолжаем обработку
                return await handler(event, data)
            await message.answer("⏱ Время ввода истекло. Начните заново.")
            return None

        # 2. Пришла команда — очищаем FSM и продолжаем
        if message.text.startswith("/"):
            db: Database | None = data.get("db")
            if db:
                await db.release_invite_reservation(str(message.from_user.id))
            await state.clear()
            return await handler(event, data)

        # 3. Обычный текст — обновляем timestamp и продолжаем
        await state.update_data(_fsm_ts=now)
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Middleware: инжектим зависимости в хендлеры
# ---------------------------------------------------------------------------
class DependencyMiddleware:
    """Добавляет db, bot, autodist в kwargs хендлеров."""

    def __init__(self, db: Database, bot: Bot, autodist: AutoDist) -> None:
        self.db       = db
        self.bot      = bot
        self.autodist = autodist

    async def __call__(
        self,
        handler: Callable[[Message, dict], Awaitable],
        event: Message,
        data: dict,
    ) -> Any:
        data["db"]       = self.db
        data["bot"]      = self.bot
        data["autodist"] = self.autodist
        # Обновляем имя пользователя из актуальных данных Telegram
        if hasattr(event, "from_user") and event.from_user:
            fu = event.from_user
            logger.debug("from_user id=%s first_name=%r username=%r", fu.id, fu.first_name, fu.username)
            if fu.first_name or fu.username:
                await self.db.update_client_info(
                    str(fu.id),
                    fu.username or "",
                    fu.first_name or fu.username or "",
                )
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Задача: напоминания о необновлённых конфигах
# ---------------------------------------------------------------------------
async def reminder_loop(autodist: AutoDist) -> None:
    """Каждые 6 часов отправляем напоминания клиентам с устаревшими конфигами."""
    try:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await autodist.send_reminders()
            except Exception as exc:
                logger.error(f"reminder_loop: {exc}")
    except asyncio.CancelledError:
        logger.info("reminder_loop stopped")
        raise


async def _bootstrap_cleanup_loop(db: "Database") -> None:
    """Каждый час удаляем истёкшие bootstrap-инвайты (пиры + DB-записи)."""
    from services.watchdog_client import WatchdogClient
    from config import config as _cfg
    try:
        while True:
            await asyncio.sleep(3600)
            try:
                expired = await db.get_expired_bootstrap_invites()
                if not expired:
                    continue
                wdc = WatchdogClient(_cfg.watchdog_url, _cfg.watchdog_token)
                for inv in expired:
                    for peer_id, iface in [
                        (inv.get("awg_peer_id"), "wg0"),
                        (inv.get("wg_peer_id"),  "wg1"),
                    ]:
                        if peer_id:
                            try:
                                await wdc.remove_peer(peer_id, interface=iface)
                            except Exception:
                                pass
                removed = await db.delete_expired_bootstrap_invites()
                if removed:
                    logger.info(f"bootstrap_cleanup: удалено {removed} истёкших инвайтов")
            except Exception as exc:
                logger.error(f"bootstrap_cleanup_loop: {exc}")
    except asyncio.CancelledError:
        logger.info("_bootstrap_cleanup_loop stopped")
        raise


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
async def on_startup(bot: Bot, dp: Dispatcher, db: Database, autodist: AutoDist) -> None:
    logger.info("Telegram-бот запускается...")

    await db.init()

    # Авторегистрация администратора и обновление имени из Telegram
    try:
        admin_user = await bot.get_chat(config.admin_chat_id)
        admin_username = admin_user.username or ""
        admin_first_name = admin_user.first_name or ""
    except Exception:
        admin_username = ""
        admin_first_name = ""

    admin = await db.get_client(config.admin_chat_id)
    if not admin:
        await db.register_admin(config.admin_chat_id, admin_username, admin_first_name)
        logger.info(f"Администратор зарегистрирован: {config.admin_chat_id}")
    elif not admin.get("first_name") and admin_first_name:
        await db.update_client_info(config.admin_chat_id, admin_username, admin_first_name)
        logger.info(f"Имя администратора обновлено: {admin_first_name}")

    # Фоновые задачи — сохраняем ссылки чтобы GC не собрал и можно было отменить
    bot._tasks = [
        asyncio.create_task(start_notify_server(bot, db), name="notify-server"),
        asyncio.create_task(reminder_loop(autodist), name="reminder-loop"),
        asyncio.create_task(_bootstrap_cleanup_loop(db), name="bootstrap-cleanup"),
    ]
    bot._db = db
    bot._autodist = autodist

    try:
        await bot.send_message(config.admin_chat_id, "✅ *Бот запущен* и готов к работе.")
    except Exception as e:
        logger.warning(f"Не удалось уведомить администратора: {e}")

    logger.info("Бот готов")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Бот завершается...")

    # 1. Отменить named background tasks
    task_names = {"notify-server", "reminder-loop", "bootstrap-cleanup"}
    to_cancel = [t for t in asyncio.all_tasks() if t.get_name() in task_names]
    for task in to_cancel:
        task.cancel()

    # 2. Дождаться завершения отменённых задач
    if to_cancel:
        await asyncio.gather(*to_cancel, return_exceptions=True)

    # 3. Остановить autodist
    if hasattr(bot, "_autodist") and bot._autodist:
        await bot._autodist.shutdown()

    # 4. Закрыть DB (WAL checkpoint)
    if hasattr(bot, "_db") and bot._db:
        await bot._db.close()

    # 5. Уведомить админа (может не дойти если Telegram недоступен)
    try:
        await bot.send_message(config.admin_chat_id, "⚠️ Бот завершается.")
    except Exception:
        pass

    # НЕ вызывать bot.session.close() — aiogram 3.x закрывает сессию сам


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
async def main() -> None:
    bot = Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Инициализируем зависимости
    db      = Database(config.db_path)
    builder = ConfigBuilder()
    autodist = AutoDist(bot, db, builder)

    # Регистрируем middlewares
    dep_mw = DependencyMiddleware(db, bot, autodist)
    dp.update.outer_middleware(FSMControlMiddleware())
    dp.message.middleware(dep_mw)
    dp.callback_query.middleware(dep_mw)

    # Роутеры (порядок важен: admin → client → requests)
    dp.include_router(admin_router)
    dp.include_router(client_router)
    dp.include_router(requests_router)

    # Lifecycle
    async def _startup() -> None:
        await on_startup(bot, dp, db, autodist)

    async def _shutdown() -> None:
        await on_shutdown(bot)

    dp.startup.register(_startup)
    dp.shutdown.register(_shutdown)

    logger.info(f"Запуск polling (admin={config.admin_chat_id})")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"delete_webhook не удался (сеть недоступна?): {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""
handlers/admin.py — Команды администратора

Все команды проверяют is_admin перед выполнением.
"""
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import config
from database import Database
from services.watchdog_client import WatchdogClient

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Middleware: проверка прав администратора
# ---------------------------------------------------------------------------
def is_admin(message: Message) -> bool:
    return str(message.from_user.id) == str(config.admin_chat_id)


def admin_only(func):
    """Декоратор: только для администратора."""
    async def wrapper(message: Message, *args, **kwargs):
        if not is_admin(message):
            return
        return await func(message, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------
class AdminFSM(StatesGroup):
    waiting_broadcast = State()
    waiting_reboot_confirm = State()
    waiting_update_confirm = State()


# ---------------------------------------------------------------------------
# /status — общий статус системы
# ---------------------------------------------------------------------------
@router.message(Command("status"))
@admin_only
async def cmd_status(message: Message, **kwargs):
    wc: WatchdogClient = kwargs.get("watchdog_client") or WatchdogClient(
        config.watchdog_url, config.watchdog_token
    )
    try:
        status = await wc.get_status()
        text = (
            f"*Статус системы*\n\n"
            f"Активный стек: `{status.get('active_stack', 'N/A')}`\n"
            f"Primary стек: `{status.get('primary_stack', 'N/A')}`\n"
            f"Внешний IP: `{status.get('external_ip', 'N/A')}`\n"
            f"Uptime: {_format_uptime(status.get('uptime_seconds', 0))}\n"
            f"Режим: {'⚠️ Деградированный' if status.get('degraded_mode') else '✅ Нормальный'}\n"
        )
        if sys_info := status.get("system"):
            text += (
                f"\n*Система:*\n"
                f"CPU: {sys_info.get('cpu_percent', 'N/A')}%\n"
                f"RAM: {sys_info.get('ram_percent', 'N/A')}%\n"
                f"Диск: {sys_info.get('disk_percent', 'N/A')}%\n"
            )
    except Exception as e:
        text = f"❌ Watchdog недоступен: {e}"

    await message.answer(text)


def _format_uptime(seconds: int) -> str:
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}д")
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    parts.append(f"{s}с")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# /tunnel — статус туннеля
# ---------------------------------------------------------------------------
@router.message(Command("tunnel"))
@admin_only
async def cmd_tunnel(message: Message, **kwargs):
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        status = await wc.get_status()
        peers = await wc.get_peers()

        text = (
            f"*Туннель*\n\n"
            f"Активный стек: `{status.get('active_stack')}`\n"
            f"Последний failover: {status.get('last_failover', 'никогда')}\n"
            f"Последняя ротация: {status.get('last_rotation', 'никогда')}\n"
            f"\n*WireGuard peers:* {len(peers.get('peers', []))}\n"
        )
    except Exception as e:
        text = f"❌ Ошибка: {e}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /switch <стек> — переключение стека
# ---------------------------------------------------------------------------
@router.message(Command("switch"))
@admin_only
async def cmd_switch(message: Message, **kwargs):
    args = message.text.split(maxsplit=1)
    stacks = ["hysteria2", "reality", "reality-grpc", "cloudflare-cdn"]

    if len(args) < 2 or args[1] not in stacks:
        await message.answer(
            f"Использование: `/switch <стек>`\n\n"
            f"Доступные стеки:\n" + "\n".join(f"• `{s}`" for s in stacks)
        )
        return

    target = args[1]
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        result = await wc.switch_stack(target)
        await message.answer(f"✅ Переключение на `{target}` запущено")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /invite — создание кода приглашения
# ---------------------------------------------------------------------------
@router.message(Command("invite"))
@admin_only
async def cmd_invite(message: Message, **kwargs):
    db: Database = kwargs.get("db")
    try:
        code = await db.create_invite_code(created_by=str(message.from_user.id))
        await message.answer(
            f"*Код приглашения:*\n`{code}`\n\n"
            f"Действителен 24 часа.\n"
            f"Перешлите клиенту для регистрации."
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /clients — список клиентов
# ---------------------------------------------------------------------------
@router.message(Command("clients"))
@admin_only
async def cmd_clients(message: Message, **kwargs):
    db: Database = kwargs.get("db")
    try:
        clients = await db.get_all_clients()
        if not clients:
            await message.answer("Нет зарегистрированных клиентов.")
            return

        lines = ["*Клиенты:*\n"]
        for c in clients:
            status = "🚫" if c.get("is_disabled") else "✅"
            lines.append(f"{status} `{c['device_name']}` (chat: `{c['chat_id']}`)")

        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /restart <сервис>
# ---------------------------------------------------------------------------
@router.message(Command("restart"))
@admin_only
async def cmd_restart(message: Message, **kwargs):
    args = message.text.split(maxsplit=1)
    allowed = ["dnsmasq", "watchdog", "hysteria2", "docker"]

    if len(args) < 2 or args[1] not in allowed:
        await message.answer(
            f"Использование: `/restart <сервис>`\n\n"
            f"Доступные: " + ", ".join(f"`{s}`" for s in allowed)
        )
        return

    service = args[1]
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        result = await wc.restart_service(service)
        status = result.get("status", "unknown")
        if status == "restarted":
            await message.answer(f"✅ Сервис `{service}` перезапущен")
        else:
            await message.answer(f"⚠️ `{service}`: {result.get('error', 'неизвестная ошибка')}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /reboot — перезагрузка сервера
# ---------------------------------------------------------------------------
@router.message(Command("reboot"))
@admin_only
async def cmd_reboot(message: Message, state: FSMContext, **kwargs):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data="reboot_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="reboot_cancel"),
    ]])
    await message.answer("⚠️ *Перезагрузить сервер?*\nВсе клиенты потеряют соединение на ~2 мин.", reply_markup=keyboard)
    await state.set_state(AdminFSM.waiting_reboot_confirm)


@router.callback_query(F.data == "reboot_confirm")
async def cb_reboot_confirm(callback: CallbackQuery, state: FSMContext, **kwargs):
    await callback.message.edit_text("🔄 Сервер перезагружается...")
    await state.clear()
    import asyncio
    asyncio.create_task(_delayed_reboot())


async def _delayed_reboot():
    import asyncio
    await asyncio.sleep(2)
    import subprocess
    subprocess.run(["reboot"])


@router.callback_query(F.data == "reboot_cancel")
async def cb_reboot_cancel(callback: CallbackQuery, state: FSMContext, **kwargs):
    await callback.message.edit_text("✅ Перезагрузка отменена.")
    await state.clear()


# ---------------------------------------------------------------------------
# /broadcast <сообщение>
# ---------------------------------------------------------------------------
@router.message(Command("broadcast"))
@admin_only
async def cmd_broadcast(message: Message, **kwargs):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/broadcast <текст сообщения>`")
        return

    text = args[1]
    db: Database = kwargs.get("db")
    from aiogram import Bot
    bot: Bot = kwargs.get("bot")

    clients = await db.get_all_clients()
    sent = 0
    for client in clients:
        if not client.get("is_disabled"):
            try:
                await bot.send_message(client["chat_id"], f"📢 *Объявление:*\n\n{text}")
                sent += 1
            except Exception:
                pass

    await message.answer(f"✅ Отправлено {sent}/{len(clients)} клиентам.")


# ---------------------------------------------------------------------------
# /routes update
# ---------------------------------------------------------------------------
@router.message(Command("routes"))
@admin_only
async def cmd_routes(message: Message, **kwargs):
    args = message.text.split()
    if len(args) < 2 or args[1] != "update":
        await message.answer("Использование: `/routes update`")
        return

    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        await wc.update_routes()
        await message.answer("✅ Обновление маршрутов запущено (занимает ~2-5 минут)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /requests — запросы клиентов
# ---------------------------------------------------------------------------
@router.message(Command("requests"))
@admin_only
async def cmd_requests(message: Message, **kwargs):
    db: Database = kwargs.get("db")
    pending = await db.get_pending_requests()

    if not pending:
        await message.answer("Нет ожидающих запросов.")
        return

    for req in pending[:10]:  # Показываем первые 10
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"req_approve_{req['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req_reject_{req['id']}"),
        ]])
        direction_emoji = "🔒" if req["direction"] == "vpn" else "🌐"
        await message.answer(
            f"{direction_emoji} *Запрос #{req['id']}*\n"
            f"Домен: `{req['domain']}`\n"
            f"Направление: {req['direction']}\n"
            f"От: `{req['chat_id']}`\n"
            f"Дата: {req['created_at']}",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("req_approve_"))
async def cb_req_approve(callback: CallbackQuery, **kwargs):
    request_id = int(callback.data.split("_")[-1])
    db: Database = kwargs.get("db")
    await db.approve_request(request_id)
    await callback.message.edit_text(f"✅ Запрос #{request_id} одобрен.")


@router.callback_query(F.data.startswith("req_reject_"))
async def cb_req_reject(callback: CallbackQuery, **kwargs):
    request_id = int(callback.data.split("_")[-1])
    db: Database = kwargs.get("db")
    await db.reject_request(request_id)
    await callback.message.edit_text(f"❌ Запрос #{request_id} отклонён.")


# ---------------------------------------------------------------------------
# /deploy, /rollback
# ---------------------------------------------------------------------------
@router.message(Command("deploy"))
@admin_only
async def cmd_deploy(message: Message, **kwargs):
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        await wc.deploy()
        await message.answer("🚀 Deploy запущен. Отчёт придёт по завершении.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("rollback"))
@admin_only
async def cmd_rollback(message: Message, **kwargs):
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        await wc.rollback()
        await message.answer("⏮️ Откат запущен...")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /vpn add|remove <домен>
# ---------------------------------------------------------------------------
@router.message(Command("vpn"))
@admin_only
async def cmd_vpn(message: Message, **kwargs):
    args = message.text.split()
    if len(args) < 3 or args[1] not in ("add", "remove"):
        await message.answer("Использование: `/vpn add|remove <домен>`")
        return

    action, domain = args[1], args[2]
    # Добавляем/удаляем из manual-vpn.txt
    manual_file = "/etc/vpn-routes/manual-vpn.txt"
    try:
        import os
        existing = set()
        if os.path.exists(manual_file):
            with open(manual_file) as f:
                existing = set(f.read().splitlines())

        if action == "add":
            existing.add(domain)
            msg = f"✅ `{domain}` добавлен в VPN-маршруты"
        else:
            existing.discard(domain)
            msg = f"✅ `{domain}` удалён из VPN-маршрутов"

        with open(manual_file, "w") as f:
            f.write("\n".join(sorted(existing)))

        # Инициируем обновление маршрутов
        wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
        await wc.update_routes()

        await message.answer(msg + "\nМаршруты обновляются...")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /logs <сервис> [кол-во строк]
# ---------------------------------------------------------------------------
@router.message(Command("logs"))
@admin_only
async def cmd_logs(message: Message, **kwargs):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/logs <сервис> [кол-во]`\nПример: `/logs watchdog 50`")
        return

    service = args[1]
    lines = int(args[2]) if len(args) > 2 else 30
    lines = min(lines, 200)

    allowed = ["watchdog", "dnsmasq", "hysteria2", "telegram-bot", "xray-client"]
    if service not in allowed:
        await message.answer(f"Допустимые сервисы: {', '.join(allowed)}")
        return

    import subprocess
    if service == "telegram-bot":
        cmd = ["docker", "logs", "--tail", str(lines), "telegram-bot"]
    else:
        cmd = ["journalctl", "-u", service, "-n", str(lines), "--no-pager"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        log_text = result.stdout or result.stderr or "(нет логов)"

        if len(log_text) > 4000:
            # Отправляем файлом
            import io
            await message.answer_document(
                document=io.BytesIO(log_text.encode()),
                filename=f"{service}.log",
                caption=f"Логи сервиса {service} (последние {lines} строк)"
            )
        else:
            await message.answer(f"*Логи {service}:*\n```\n{log_text}\n```")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# /menu — главное меню
# ---------------------------------------------------------------------------
@router.message(Command("menu"))
@admin_only
async def cmd_menu(message: Message, **kwargs):
    text = (
        "*Главное меню администратора*\n\n"
        "*Мониторинг:*\n"
        "/status — статус системы\n"
        "/tunnel — статус туннеля\n"
        "/clients — список клиентов\n"
        "/logs — просмотр логов\n\n"
        "*Управление:*\n"
        "/switch — переключить стек\n"
        "/restart — перезапустить сервис\n"
        "/routes update — обновить маршруты\n"
        "/vpn add|remove — управление доменами\n\n"
        "*Клиенты:*\n"
        "/invite — код приглашения\n"
        "/requests — запросы на модерации\n"
        "/broadcast — рассылка\n\n"
        "*Обновление:*\n"
        "/deploy — обновить систему\n"
        "/rollback — откат\n"
        "/reboot — перезагрузка"
    )
    await message.answer(text)

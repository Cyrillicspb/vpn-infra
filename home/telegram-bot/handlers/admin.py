"""
handlers/admin.py — Все команды администратора

Команды (из CLAUDE.md):
  /status /tunnel /ip /docker /speed /logs /graph
  /switch /restart /update /deploy /rollback
  /invite /clients /broadcast /requests
  /vpn add|remove   /direct add|remove   /list vpn|direct   /check
  /routes update    /vps list|add|remove  /migrate-vps
  /client disable|enable|kick|limit
  /rotate-keys  /renew-cert  /renew-ca
  /diagnose     /reboot      /menu
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from database import Database
from services.watchdog_client import WatchdogClient, WatchdogError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)
router = Router()

MANUAL_VPN    = Path("/etc/vpn-routes/manual-vpn.txt")
MANUAL_DIRECT = Path("/etc/vpn-routes/manual-direct.txt")
ALLOWED_SERVICES = {
    "dnsmasq", "watchdog", "hysteria2", "docker",
    "wg-quick@wg0", "wg-quick@wg1", "nftables",
}


# ---------------------------------------------------------------------------
# Проверка прав
# ---------------------------------------------------------------------------
def _is_admin(message: Message) -> bool:
    return str(message.from_user.id) == str(config.admin_chat_id)


def _wc() -> WatchdogClient:
    return WatchdogClient(config.watchdog_url, config.watchdog_token)


def _uptime(s: int) -> str:
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    parts.append(f"{s}с")
    return " ".join(parts)


async def _require_admin(message: Message) -> bool:
    if not _is_admin(message):
        return False
    return True


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------
class AdminFSM(StatesGroup):
    reboot_confirm  = State()
    update_confirm  = State()
    broadcast_input = State()
    migrate_confirm = State()


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
@router.message(Command("status"), StateFilter("*"))
async def cmd_status(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        s = await _wc().get_status()
        sys = s.get("system", {})
        text = (
            f"*Статус системы*\n\n"
            f"Режим: {'⚠️ Деградированный' if s.get('degraded_mode') else '✅ Нормальный'}\n"
            f"Стек: `{s.get('active_stack')}`\n"
            f"IP: `{s.get('external_ip', 'N/A')}`\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}\n"
            f"Failover: {s.get('last_failover') or 'никогда'}\n\n"
            f"CPU: {sys.get('cpu_percent', '?')}%  "
            f"RAM: {sys.get('ram_percent', '?')}%  "
            f"Диск: {sys.get('disk_percent', '?')}%"
        )
    except WatchdogError as e:
        text = f"❌ Watchdog недоступен: {e}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /tunnel
# ---------------------------------------------------------------------------
@router.message(Command("tunnel"), StateFilter("*"))
async def cmd_tunnel(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        s = await _wc().get_status()
        peers = await _wc().get_peers()
        plugins = "\n".join(
            f"  {'🟢' if p['name'] == s.get('active_stack') else '⚪'} `{p['name']}` (устойчивость {p['resilience']})"
            for p in s.get("plugins", [])
        )
        text = (
            f"*Туннель*\n\n"
            f"Активный стек: `{s.get('active_stack')}`\n"
            f"Primary: `{s.get('primary_stack')}`\n"
            f"Последний failover: {s.get('last_failover') or 'никогда'}\n"
            f"Следующая ротация: {s.get('next_rotation', 'N/A')[:16]}\n\n"
            f"*Стеки:*\n{plugins}\n\n"
            f"*WG peers:* {peers.get('count', 0)}"
        )
    except WatchdogError as e:
        text = f"❌ Ошибка: {e}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /ip
# ---------------------------------------------------------------------------
@router.message(Command("ip"), StateFilter("*"))
async def cmd_ip(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        s = await _wc().get_status()
        await message.answer(f"Внешний IP: `{s.get('external_ip', 'неизвестен')}`")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /docker
# ---------------------------------------------------------------------------
@router.message(Command("docker"), StateFilter("*"))
async def cmd_docker(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().splitlines()
        if not lines:
            await message.answer("Нет контейнеров.")
            return
        rows = []
        for line in lines:
            parts = line.split("\t")
            name   = parts[0] if len(parts) > 0 else "?"
            status = parts[1] if len(parts) > 1 else "?"
            emoji  = "🟢" if "Up" in status else ("🔴" if "Exited" in status else "🟡")
            rows.append(f"{emoji} `{name}` — {status}")
        await message.answer("*Docker контейнеры:*\n\n" + "\n".join(rows))
    except Exception as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /speed
# ---------------------------------------------------------------------------
@router.message(Command("speed"), StateFilter("*"))
async def cmd_speed(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        metrics = await _wc().get_metrics()
        await message.answer(f"*Метрики:*\n```\n{metrics[:3000]}\n```")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /logs <сервис> [N]
# ---------------------------------------------------------------------------
@router.message(Command("logs"), StateFilter("*"))
async def cmd_logs(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    allowed = ["watchdog", "dnsmasq", "hysteria2", "telegram-bot", "xray-client",
               "xray-client-2", "cloudflared", "node-exporter"]
    if len(args) < 2 or args[1] not in allowed:
        await message.answer(
            "Использование: `/logs <сервис> [N]`\n"
            "Доступные: " + ", ".join(f"`{s}`" for s in allowed)
        )
        return
    service = args[1]
    n = min(int(args[2]), 300) if len(args) > 2 and args[2].isdigit() else 50

    if service in ("telegram-bot", "xray-client", "xray-client-2", "cloudflared", "node-exporter"):
        cmd = ["docker", "logs", "--tail", str(n), service]
    else:
        cmd = ["journalctl", "-u", service, "-n", str(n), "--no-pager", "--output=short"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        text = result.stdout or result.stderr or "(нет логов)"
        if len(text) > 4000:
            await message.answer_document(
                BufferedInputFile(text.encode(), filename=f"{service}.log"),
                caption=f"Логи `{service}` ({n} строк)",
            )
        else:
            await message.answer(f"*Логи {service}:*\n```\n{text[-3900:]}\n```")
    except Exception as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /graph [panel] [period]
# ---------------------------------------------------------------------------
@router.message(Command("graph"), StateFilter("*"))
async def cmd_graph(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    panel  = args[1] if len(args) > 1 else "tunnel"
    period = args[2] if len(args) > 2 else "1h"
    panels = ["tunnel", "speed", "clients", "system"]
    if panel not in panels:
        await message.answer("Панели: " + " | ".join(f"`{p}`" for p in panels))
        return
    try:
        png = await _wc().get_graph(panel, period)
        if png:
            await message.answer_photo(
                BufferedInputFile(png, filename="graph.png"),
                caption=f"График `{panel}` за `{period}`",
            )
        else:
            await message.answer("Grafana не вернула изображение")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /switch <стек>
# ---------------------------------------------------------------------------
@router.message(Command("switch"), StateFilter("*"))
async def cmd_switch(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    stacks = ["cloudflare-cdn", "reality-grpc", "reality", "hysteria2"]
    if len(args) < 2 or args[1] not in stacks:
        await message.answer(
            "Использование: `/switch <стек>`\n\n"
            + "\n".join(f"• `{s}`" for s in stacks)
        )
        return
    try:
        await _wc().switch_stack(args[1])
        await message.answer(f"🔄 Переключение на `{args[1]}` запущено")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /restart <сервис>
# ---------------------------------------------------------------------------
@router.message(Command("restart"), StateFilter("*"))
async def cmd_restart(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2 or args[1] not in ALLOWED_SERVICES:
        await message.answer(
            "Использование: `/restart <сервис>`\n"
            "Доступные: " + ", ".join(f"`{s}`" for s in sorted(ALLOWED_SERVICES))
        )
        return
    try:
        r = await _wc().restart_service(args[1])
        st = r.get("status", "?")
        if st == "ok":
            await message.answer(f"✅ `{args[1]}` перезапущен")
        else:
            await message.answer(f"⚠️ {r.get('error', 'ошибка')}")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /update — обновление Docker образов
# ---------------------------------------------------------------------------
@router.message(Command("update"), StateFilter("*"))
async def cmd_update(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Обновить все", callback_data="update_all"),
        InlineKeyboardButton(text="❌ Отмена",       callback_data="update_cancel"),
    ]])
    await message.answer(
        "⚠️ *Обновление Docker образов*\n"
        "Сервисы будут кратковременно недоступны.",
        reply_markup=kb,
    )
    await state.set_state(AdminFSM.update_confirm)


@router.callback_query(F.data == "update_all", AdminFSM.update_confirm)
async def cb_update_all(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.message.edit_text("🔄 Обновление запущено...")
    await state.clear()
    try:
        await _wc().update_service("all")
        await cb.message.answer("✅ Обновление запущено в фоне")
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}")


@router.callback_query(F.data == "update_cancel", AdminFSM.update_confirm)
async def cb_update_cancel(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.message.edit_text("✅ Отменено.")
    await state.clear()


# ---------------------------------------------------------------------------
# /deploy / /rollback
# ---------------------------------------------------------------------------
@router.message(Command("deploy"), StateFilter("*"))
async def cmd_deploy(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        await _wc().deploy()
        await message.answer("🚀 Deploy запущен. Отчёт придёт по завершении.")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


@router.message(Command("rollback"), StateFilter("*"))
async def cmd_rollback(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        await _wc().rollback()
        await message.answer("⏮️ Откат запущен...")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /reboot
# ---------------------------------------------------------------------------
@router.message(Command("reboot"), StateFilter("*"))
async def cmd_reboot(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data="reboot_yes"),
        InlineKeyboardButton(text="❌ Отмена",            callback_data="reboot_no"),
    ]])
    await message.answer(
        "⚠️ *Перезагрузить сервер?*\nКлиенты потеряют соединение на ~2 мин.",
        reply_markup=kb,
    )
    await state.set_state(AdminFSM.reboot_confirm)


@router.callback_query(F.data == "reboot_yes", AdminFSM.reboot_confirm)
async def cb_reboot_yes(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.message.edit_text("🔄 Перезагрузка через 3 секунды...")
    await state.clear()
    asyncio.create_task(_delayed_reboot())


async def _delayed_reboot():
    await asyncio.sleep(3)
    subprocess.run(["reboot"])


@router.callback_query(F.data == "reboot_no", AdminFSM.reboot_confirm)
async def cb_reboot_no(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.message.edit_text("✅ Отменено.")
    await state.clear()


# ---------------------------------------------------------------------------
# /invite
# ---------------------------------------------------------------------------
@router.message(Command("invite"), StateFilter("*"))
async def cmd_invite(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    db: Database = kw.get("db")
    code = await db.create_invite_code(str(message.from_user.id))
    await message.answer(
        f"*Код приглашения:*\n`{code}`\n\n"
        f"Действителен 24 часа.\nПерешлите клиенту для регистрации (/start)."
    )


# ---------------------------------------------------------------------------
# /clients
# ---------------------------------------------------------------------------
@router.message(Command("clients"), StateFilter("*"))
async def cmd_clients(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    db: Database = kw.get("db")
    clients = await db.get_all_clients()
    if not clients:
        await message.answer("Нет зарегистрированных клиентов.")
        return
    lines = ["*Клиенты:*\n"]
    for c in clients:
        icon = "🚫" if c.get("is_disabled") else "✅"
        name = c.get("username") or c["chat_id"]
        lines.append(f"{icon} `{name}` (id: `{c['chat_id']}`)")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /client disable|enable|kick|limit <имя> [значение]
# ---------------------------------------------------------------------------
@router.message(Command("client"), StateFilter("*"))
async def cmd_client(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    db: Database = kw.get("db")
    args = message.text.split()
    usage = (
        "Использование:\n"
        "`/client disable <имя>`\n"
        "`/client enable <имя>`\n"
        "`/client kick <имя>`\n"
        "`/client limit <имя> <N>`"
    )
    if len(args) < 3:
        await message.answer(usage)
        return

    action, name = args[1], args[2]
    client = await db.find_client_by_name(name)
    if not client:
        await message.answer(f"Клиент `{name}` не найден.")
        return

    chat_id = client["chat_id"]

    if action == "disable":
        await db.set_client_disabled(chat_id, True)
        await message.answer(f"✅ Клиент `{name}` отключён.")

    elif action == "enable":
        await db.set_client_disabled(chat_id, False)
        await message.answer(f"✅ Клиент `{name}` включён.")

    elif action == "kick":
        # Удалить все устройства и их WG-пиры
        devices = await db.get_devices(chat_id)
        wc = _wc()
        for d in devices:
            if d.get("public_key"):
                try:
                    await wc.remove_peer(d["public_key"])
                except Exception:
                    pass
            await db.delete_device(d["id"])
        await db.set_client_disabled(chat_id, True)
        bot: "Bot" = kw.get("bot")
        try:
            await bot.send_message(chat_id, "❌ Ваш доступ к VPN отозван.")
        except Exception:
            pass
        await message.answer(f"✅ Клиент `{name}` кикнут, устройства удалены.")

    elif action == "limit":
        if len(args) < 4 or not args[3].isdigit():
            await message.answer("Использование: `/client limit <имя> <N>`")
            return
        limit = int(args[3])
        await db.set_client_limit(chat_id, limit)
        await message.answer(f"✅ Лимит устройств для `{name}` = {limit}")

    else:
        await message.answer(usage)


# ---------------------------------------------------------------------------
# /broadcast
# ---------------------------------------------------------------------------
@router.message(Command("broadcast"), StateFilter("*"))
async def cmd_broadcast(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/broadcast <текст>`")
        return
    db: Database = kw.get("db")
    bot: "Bot" = kw.get("bot")
    clients = await db.get_all_clients()
    sent = 0
    for c in clients:
        if not c.get("is_disabled"):
            try:
                await bot.send_message(c["chat_id"], f"📢 *Объявление:*\n\n{args[1]}")
                sent += 1
            except Exception:
                pass
    await message.answer(f"✅ Отправлено {sent}/{len(clients)} клиентам.")


# ---------------------------------------------------------------------------
# /requests — запросы клиентов
# ---------------------------------------------------------------------------
@router.message(Command("requests"), StateFilter("*"))
async def cmd_requests(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    db: Database = kw.get("db")

    # Ожидающие устройства
    devices = await db.get_pending_devices()
    for d in devices[:5]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"dev_approve_{d['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dev_reject_{d['id']}"),
        ]])
        await message.answer(
            f"📱 *Устройство на модерации*\n"
            f"Клиент: `{d.get('username') or d['chat_id']}`\n"
            f"Устройство: `{d['device_name']}`\n"
            f"Протокол: `{d['protocol'].upper()}`",
            reply_markup=kb,
        )

    # Ожидающие запросы доменов
    reqs = await db.get_pending_requests()
    for r in reqs[:10]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"req_approve_{r['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req_reject_{r['id']}"),
        ]])
        icon = "🔒" if r["direction"] == "vpn" else "🌐"
        await message.answer(
            f"{icon} *Запрос #{r['id']}*\n"
            f"Домен: `{r['domain']}`  ({r['direction']})\n"
            f"От: `{r['chat_id']}`  {r['created_at'][:16]}",
            reply_markup=kb,
        )

    if not devices and not reqs:
        await message.answer("Нет ожидающих запросов.")


@router.callback_query(F.data.startswith("dev_approve_"))
async def cb_dev_approve(cb: CallbackQuery, **kw):
    from handlers.requests import notify_device_approved, safe_edit
    device_id = int(cb.data.split("_")[-1])
    db: Database = kw.get("db")
    device = await db.approve_device(device_id)
    if device:
        # Добавляем пир через watchdog
        try:
            await _wc().add_peer(
                device["device_name"],
                device["protocol"],
                device.get("public_key", ""),
            )
        except Exception:
            pass
        bot: "Bot" = kw.get("bot")
        if bot:
            asyncio.create_task(
                notify_device_approved(
                    bot, db, device, autodist=kw.get("autodist")
                )
            )
    name = device["device_name"] if device else device_id
    await safe_edit(cb, f"✅ Устройство `{name}` одобрено.")


@router.callback_query(F.data.startswith("dev_reject_"))
async def cb_dev_reject(cb: CallbackQuery, **kw):
    from handlers.requests import notify_device_rejected, safe_edit
    device_id = int(cb.data.split("_")[-1])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    await db.delete_device(device_id)
    bot: "Bot" = kw.get("bot")
    if bot and device:
        asyncio.create_task(
            notify_device_rejected(bot, str(device["chat_id"]), device["device_name"])
        )
    name = device["device_name"] if device else device_id
    await safe_edit(cb, f"❌ Устройство `{name}` отклонено.")


@router.callback_query(F.data.startswith("req_approve_"))
async def cb_req_approve(cb: CallbackQuery, **kw):
    from handlers.requests import notify_request_approved, safe_edit
    req_id = int(cb.data.split("_")[-1])
    db: Database = kw.get("db")
    req = await db.approve_request(req_id)
    if req:
        # Добавляем домен в соответствующий файл и запускаем обновление маршрутов
        target = MANUAL_VPN if req["direction"] == "vpn" else MANUAL_DIRECT
        _file_add_line(target, req["domain"])
        try:
            await _wc().update_routes()
        except Exception:
            pass
        bot: "Bot" = kw.get("bot")
        if bot:
            asyncio.create_task(
                notify_request_approved(bot, req, autodist=kw.get("autodist"))
            )
    await safe_edit(cb, f"✅ Запрос #{req_id} одобрен.")


@router.callback_query(F.data.startswith("req_reject_"))
async def cb_req_reject(cb: CallbackQuery, **kw):
    from handlers.requests import notify_request_rejected, safe_edit
    req_id = int(cb.data.split("_")[-1])
    db: Database = kw.get("db")
    req = await db.get_request_by_id(req_id)
    await db.reject_request(req_id)
    bot: "Bot" = kw.get("bot")
    if bot and req:
        asyncio.create_task(notify_request_rejected(bot, req))
    await safe_edit(cb, f"❌ Запрос #{req_id} отклонён.")


# ---------------------------------------------------------------------------
# /vpn add|remove <домен>
# ---------------------------------------------------------------------------
@router.message(Command("vpn"), StateFilter("*"))
async def cmd_vpn(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 3 or args[1] not in ("add", "remove"):
        await message.answer("Использование: `/vpn add|remove <домен>`")
        return
    action, domain = args[1], args[2].lower().strip(".")
    if action == "add":
        _file_add_line(MANUAL_VPN, domain)
        msg = f"✅ `{domain}` добавлен в VPN-маршруты"
    else:
        _file_remove_line(MANUAL_VPN, domain)
        msg = f"✅ `{domain}` удалён из VPN-маршрутов"
    try:
        await _wc().update_routes()
    except WatchdogError:
        pass
    autodist = kw.get("autodist")
    if autodist:
        autodist.trigger(f"/vpn {action} {domain}")
    await message.answer(msg + "\nМаршруты обновляются...")


# ---------------------------------------------------------------------------
# /direct add|remove <домен>
# ---------------------------------------------------------------------------
@router.message(Command("direct"), StateFilter("*"))
async def cmd_direct(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 3 or args[1] not in ("add", "remove"):
        await message.answer("Использование: `/direct add|remove <домен>`")
        return
    action, domain = args[1], args[2].lower().strip(".")
    if action == "add":
        _file_add_line(MANUAL_DIRECT, domain)
        msg = f"✅ `{domain}` добавлен в прямые маршруты"
    else:
        _file_remove_line(MANUAL_DIRECT, domain)
        msg = f"✅ `{domain}` удалён из прямых маршрутов"
    try:
        await _wc().update_routes()
    except WatchdogError:
        pass
    await message.answer(msg + "\nМаршруты обновляются...")


# ---------------------------------------------------------------------------
# /list vpn|direct
# ---------------------------------------------------------------------------
@router.message(Command("list"), StateFilter("*"))
async def cmd_list(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2 or args[1] not in ("vpn", "direct"):
        await message.answer("Использование: `/list vpn|direct`")
        return
    target = MANUAL_VPN if args[1] == "vpn" else MANUAL_DIRECT
    if not target.exists():
        await message.answer("Список пуст.")
        return
    lines = [ln.strip() for ln in target.read_text().splitlines() if ln.strip()]
    if not lines:
        await message.answer("Список пуст.")
        return
    text = f"*Список {args[1]}:*\n" + "\n".join(f"• `{ln}`" for ln in lines[:50])
    if len(lines) > 50:
        text += f"\n... и ещё {len(lines) - 50}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /check <домен>
# ---------------------------------------------------------------------------
@router.message(Command("check"), StateFilter("*"))
async def cmd_check(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/check <домен>`")
        return
    domain = args[1].lower().strip(".")
    in_vpn    = MANUAL_VPN.exists() and domain in MANUAL_VPN.read_text()
    in_direct = MANUAL_DIRECT.exists() and domain in MANUAL_DIRECT.read_text()
    label = "VPN" if in_vpn else ("прямой" if in_direct else "не задан")
    await message.answer(f"`{domain}`: {label}")


# ---------------------------------------------------------------------------
# /routes update
# ---------------------------------------------------------------------------
@router.message(Command("routes"), StateFilter("*"))
async def cmd_routes(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2 or args[1] != "update":
        await message.answer("Использование: `/routes update`")
        return
    try:
        await _wc().update_routes()
        autodist = kw.get("autodist")
        if autodist:
            autodist.trigger("/routes update")
        await message.answer("✅ Обновление маршрутов запущено (~2-5 мин)")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /vps list|add|remove
# ---------------------------------------------------------------------------
@router.message(Command("vps"), StateFilter("*"))
async def cmd_vps(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2 or args[1] not in ("list", "add", "remove"):
        await message.answer("Использование:\n`/vps list`\n`/vps add <IP>`\n`/vps remove <IP>`")
        return
    try:
        if args[1] == "list":
            data = await _wc().get_vps_list()
            vps_list = data.get("vps_list", [])
            if not vps_list:
                await message.answer("VPS не добавлены.")
                return
            lines = []
            for i, v in enumerate(vps_list):
                active = "✅" if i == data.get("active_idx", 0) else "⚪"
                lines.append(f"{active} `{v['ip']}` (SSH :{v.get('ssh_port', 22)})")
            await message.answer("*VPS серверы:*\n" + "\n".join(lines))
        elif args[1] == "add":
            if len(args) < 3:
                await message.answer("/vps add <IP> [SSH_PORT]")
                return
            ip = args[2]
            port = int(args[3]) if len(args) > 3 else 443
            await _wc().add_vps(ip, port)
            await message.answer(f"✅ VPS `{ip}` добавлен")
        else:
            if len(args) < 3:
                await message.answer("/vps remove <IP>")
                return
            await _wc().remove_vps(args[2])
            await message.answer(f"✅ VPS `{args[2]}` удалён")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /migrate-vps <IP> [--from-backup]
# ---------------------------------------------------------------------------
@router.message(Command("migrate_vps"), StateFilter("*"))
async def cmd_migrate_vps(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/migrate_vps <новый_IP> [--from-backup]`")
        return
    new_ip = args[1]
    from_backup = "--from-backup" in args
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"migrate_{new_ip}_{int(from_backup)}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="migrate_cancel"),
    ]])
    await message.answer(
        f"Мигрировать на VPS `{new_ip}`?\n{'Восстановление из бэкапа.' if from_backup else ''}",
        reply_markup=kb,
    )
    await state.set_state(AdminFSM.migrate_confirm)


@router.callback_query(F.data.startswith("migrate_"), AdminFSM.migrate_confirm)
async def cb_migrate(cb: CallbackQuery, state: FSMContext, **kw):
    parts = cb.data.split("_")
    new_ip = parts[1]
    from_backup = parts[2] == "1"
    await state.clear()
    await cb.message.edit_text(f"🔄 Миграция на `{new_ip}` запущена...")
    try:
        await _wc().deploy(force=True)
        await cb.message.answer(f"✅ Миграция на `{new_ip}` инициирована")
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}")


@router.callback_query(F.data == "migrate_cancel", AdminFSM.migrate_confirm)
async def cb_migrate_cancel(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.message.edit_text("✅ Отменено.")
    await state.clear()


# ---------------------------------------------------------------------------
# /rotate-keys
# ---------------------------------------------------------------------------
@router.message(Command("rotate_keys"), StateFilter("*"))
async def cmd_rotate_keys(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    await message.answer(
        "⚠️ Ротация ключей сбросит все клиентские конфиги.\n"
        "Функция реализуется через deploy.sh --rotate-keys\n"
        "Запустите: `/deploy`"
    )


# ---------------------------------------------------------------------------
# /renew-cert / /renew-ca
# ---------------------------------------------------------------------------
@router.message(Command("renew_cert"), StateFilter("*"))
async def cmd_renew_cert(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    result = subprocess.run(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "client"],
        capture_output=True, text=True, timeout=60,
    )
    ok = result.returncode == 0
    await message.answer(
        f"{'✅' if ok else '❌'} Обновление клиентского сертификата mTLS:\n"
        f"```\n{(result.stdout or result.stderr)[:500]}\n```"
    )


@router.message(Command("renew_ca"), StateFilter("*"))
async def cmd_renew_ca(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    result = subprocess.run(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "ca"],
        capture_output=True, text=True, timeout=60,
    )
    ok = result.returncode == 0
    await message.answer(
        f"{'✅' if ok else '❌'} Обновление CA:\n"
        f"```\n{(result.stdout or result.stderr)[:500]}\n```"
    )


# ---------------------------------------------------------------------------
# /diagnose <устройство>
# ---------------------------------------------------------------------------
@router.message(Command("diagnose"), StateFilter("*"))
async def cmd_diagnose(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/diagnose <устройство>`")
        return
    device_name = args[1]
    try:
        r = await _wc().diagnose(device_name)
        text = (
            f"*Диагностика `{device_name}`:*\n\n"
            f"WG peer: {'✅' if r.get('wg_peer_found') else '❌'}\n"
            f"DNS: {'✅' if r.get('dns_ok') else '❌'}\n"
            f"Туннель: {'✅' if r.get('tunnel_ok') else '❌'} "
            f"RTT: {r.get('tunnel_rtt_ms', '?'):.0f}ms\n"
            f"Заблокированные сайты: {'✅' if r.get('blocked_sites_ok') else '❌'}"
        )
    except WatchdogError as e:
        text = f"❌ {e}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /menu
# ---------------------------------------------------------------------------
@router.message(Command("menu"), StateFilter("*"))
async def cmd_menu(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    await message.answer(
        "*Меню администратора*\n\n"
        "*Мониторинг:*\n"
        "/status /tunnel /ip /docker /speed /logs /graph\n\n"
        "*Управление:*\n"
        "/switch /restart /update /deploy /rollback /reboot\n\n"
        "*Маршруты:*\n"
        "/vpn /direct /list /check /routes\n\n"
        "*Клиенты:*\n"
        "/invite /clients /client /broadcast /requests\n\n"
        "*VPS:*\n"
        "/vps /migrate\\_vps\n\n"
        "*Безопасность:*\n"
        "/rotate\\_keys /renew\\_cert /renew\\_ca /diagnose"
    )


# ---------------------------------------------------------------------------
# Утилиты работы с файлами маршрутов
# ---------------------------------------------------------------------------
def _file_add_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        existing = {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}
    existing.add(line)
    path.write_text("\n".join(sorted(existing)) + "\n")


def _file_remove_line(path: Path, line: str) -> None:
    if not path.exists():
        return
    lines = {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}
    lines.discard(line)
    path.write_text("\n".join(sorted(lines)) + "\n")

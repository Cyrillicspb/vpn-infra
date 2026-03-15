"""
handlers/admin.py — Все команды администратора

Команды (из CLAUDE.md):
  /status /tunnel /ip /docker /speed /logs /graph
  /switch /restart /upgrade /deploy /rollback
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

from aiogram import Bot, F, Router
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
from handlers.keyboards import (
    admin_client_actions_kb,
    admin_clients_list_kb,
    admin_clients_menu,
    admin_diagnose_kb,
    admin_graph_menu,
    admin_logs_menu,
    admin_main_menu,
    admin_manage_menu,
    admin_monitor_menu,
    admin_restart_menu,
    admin_routes_menu,
    admin_security_menu,
    admin_switch_menu,
    admin_vps_list_kb,
    admin_vps_menu,
    back_to_admin_menu,
    domains_inline_kb,
    menu_reply_kb,
)
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
    reboot_confirm     = State()
    update_confirm     = State()
    broadcast_input    = State()
    migrate_confirm    = State()
    vpn_add_domain     = State()
    direct_add_domain  = State()
    check_domain       = State()
    vps_add_ip         = State()
    client_limit_input = State()


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
        import aiohttp as _aiohttp
        docker_host = os.getenv("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://")
        async with _aiohttp.ClientSession() as session:
            async with session.get(f"{docker_host}/containers/json?all=1", timeout=_aiohttp.ClientTimeout(total=10)) as r:
                containers = await r.json()
        if not containers:
            await message.answer("Нет контейнеров.")
            return
        rows = []
        for c in sorted(containers, key=lambda x: x.get("Names", [""])[0]):
            name   = (c.get("Names") or ["?"])[0].lstrip("/")
            state_ = c.get("State", "?")
            status = c.get("Status", "?")
            emoji  = "🟢" if state_ == "running" else ("🔴" if state_ == "exited" else "🟡")
            rows.append(f"{emoji} <code>{name}</code> — {status}")
        await message.answer("<b>Docker контейнеры:</b>\n\n" + "\n".join(rows), parse_mode="HTML")
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
        s = await _wc().get_status()
        metrics_raw = await _wc().get_metrics()
        metrics: dict[str, str] = {}
        for line in metrics_raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key = line.split("{")[0].split(" ")[0]
            val = line.rsplit(" ", 1)[-1]
            metrics[key] = val

        def _fmt_bytes(b_str: str) -> str:
            try:
                b = int(float(b_str))
                if b >= 1_000_000_000:
                    return f"{b/1_000_000_000:.1f} GB"
                if b >= 1_000_000:
                    return f"{b/1_000_000:.1f} MB"
                return f"{b/1_000:.1f} KB"
            except Exception:
                return "?"

        sys_info = s.get("system", {})
        rx = _fmt_bytes(metrics.get("vpn_bytes_recv_total", "0"))
        tx = _fmt_bytes(metrics.get("vpn_bytes_sent_total", "0"))
        text = (
            f"<b>Ресурсы и трафик</b>\n\n"
            f"CPU: <b>{sys_info.get('cpu_percent', '?')}%</b>  "
            f"RAM: <b>{sys_info.get('ram_percent', '?')}%</b>  "
            f"Диск: <b>{sys_info.get('disk_percent', '?')}%</b>\n\n"
            f"↓ Получено: <b>{rx}</b>\n"
            f"↑ Отправлено: <b>{tx}</b>\n\n"
            f"Стек: <code>{s.get('active_stack')}</code>\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}"
        )
        await message.answer(text, parse_mode="HTML")
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
# /assess — тест скорости всех стеков + автовыбор оптимального
# ---------------------------------------------------------------------------
@router.message(Command("assess"), StateFilter("*"))
async def cmd_assess(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    try:
        data = await _wc().assess()
        eta = data.get("eta_seconds", 40)
        stacks = ", ".join(data.get("stacks", []))
        await message.answer(
            f"🔍 <b>Тест стеков запущен</b>\n\n"
            f"Стеки: <code>{stacks}</code>\n"
            f"Ожидаемое время: ~{eta} сек\n\n"
            f"Результат придёт отдельным сообщением.",
            parse_mode="HTML",
        )
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
# /upgrade — обновление Docker образов (переименовано, /update — для клиентских конфигов)
# ---------------------------------------------------------------------------
@router.message(Command("upgrade"), StateFilter("*"))
async def cmd_upgrade(message: Message, state: FSMContext, **kw):
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
async def cmd_invite(message: Message, state: FSMContext, bot: Bot, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    db: Database = kw.get("db")
    code = await db.create_invite_code(str(message.from_user.id))
    me = await bot.get_me()
    bot_link = f"https://t.me/{me.username}" if me.username else ""
    await message.answer(
        f"🎫 <b>Код приглашения готов</b>\n\n"
        f"Действителен 24 часа.\n"
        f"Перешлите клиенту ссылку на бота и код ниже.\n\n"
        f"{bot_link}",
        parse_mode="HTML",
    )
    await message.answer(f"<code>{code}</code>", parse_mode="HTML")


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
        name = c.get("first_name") or c.get("username") or c["chat_id"]
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
        bot: "Bot" = kw.get("bot")
        autodist = kw.get("autodist")
        if bot:
            # Сначала отправляем конфиг (внутри сгенерируются и сохранятся ключи)
            await notify_device_approved(bot, db, device, autodist=autodist)
            # Потом добавляем пир (теперь public_key есть в БД)
            device = await db.get_device_by_id(device_id)
            if device and device.get("public_key"):
                try:
                    await _wc().add_peer(
                        device["device_name"],
                        device["protocol"],
                        device["public_key"],
                    )
                except Exception:
                    pass
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
# /menu — главное инлайн-меню
# ---------------------------------------------------------------------------
@router.message(Command("menu"), StateFilter("*"))
async def cmd_menu(message: Message, state: FSMContext, **kw):
    if not _is_admin(message):
        return
    await state.clear()
    await message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await message.answer("<b>Меню администратора</b>", reply_markup=admin_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Навигация по меню (callback-запросы)
# ---------------------------------------------------------------------------

async def _edit_or_answer(cb: CallbackQuery, text: str, kb=None) -> None:
    """Редактирует сообщение или отправляет новое если редактирование не удалось."""
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"_edit_or_answer: не удалось отправить сообщение: {e}")
    try:
        await cb.answer()
    except Exception:
        pass


@router.callback_query(F.data == "adm:menu")
async def cb_adm_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "<b>Меню администратора</b>", admin_main_menu())


@router.callback_query(F.data == "adm:monitor")
async def cb_adm_monitor(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📊 <b>Мониторинг</b>", admin_monitor_menu())


@router.callback_query(F.data == "adm:manage")
async def cb_adm_manage(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "⚙️ <b>Управление</b>", admin_manage_menu())


@router.callback_query(F.data == "adm:routes")
async def cb_adm_routes(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🌐 <b>Маршруты</b>", admin_routes_menu())


@router.callback_query(F.data == "adm:clients")
async def cb_adm_clients(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "👥 <b>Клиенты</b>", admin_clients_menu())


@router.callback_query(F.data == "adm:vps")
async def cb_adm_vps(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🖥️ <b>VPS серверы</b>", admin_vps_menu())


@router.callback_query(F.data == "adm:security")
async def cb_adm_security(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🔐 <b>Безопасность</b>", admin_security_menu())


@router.callback_query(F.data == "adm:switch_menu")
async def cb_adm_switch_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🔄 <b>Выберите стек:</b>", admin_switch_menu())


@router.callback_query(F.data == "adm:assess")
async def cb_adm_assess(cb: CallbackQuery, **kw):
    await cb.answer()
    try:
        data = await _wc().assess()
        eta = data.get("eta_seconds", 40)
        stacks = ", ".join(data.get("stacks", []))
        await cb.message.answer(
            f"🔍 <b>Тест стеков запущен</b>\n\n"
            f"Стеки: <code>{stacks}</code>\n"
            f"Ожидаемое время: ~{eta} сек\n\n"
            f"Результат придёт отдельным сообщением.",
            parse_mode="HTML",
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}")


@router.callback_query(F.data == "adm:restart_menu")
async def cb_adm_restart_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🔃 <b>Выберите сервис для перезапуска:</b>", admin_restart_menu())


# ---------------------------------------------------------------------------
# Действия мониторинга
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:status")
async def cb_adm_status(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        sys_info = s.get("system", {})
        mode = "⚠️ Деградированный" if s.get("degraded_mode") else "✅ Нормальный"
        failover = s.get("last_failover") or "никогда"
        rotation = (s.get("next_rotation") or "N/A")[:16].replace("T", " ")
        cpu  = sys_info.get("cpu_percent", "?")
        ram  = sys_info.get("ram_percent", "?")
        disk = sys_info.get("disk_percent", "?")
        text = (
            f"<b>Статус системы</b>\n\n"
            f"Режим: {mode}\n"
            f"Стек: <code>{s.get('active_stack')}</code>\n"
            f"Primary: <code>{s.get('primary_stack')}</code>\n"
            f"IP: <code>{s.get('external_ip', 'N/A')}</code>\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}\n"
            f"Failover: {failover}\n"
            f"Ротация: {rotation}\n\n"
            f"CPU: <b>{cpu}%</b>  RAM: <b>{ram}%</b>  Диск: <b>{disk}%</b>"
        )
    except WatchdogError as e:
        text = f"❌ Watchdog недоступен: {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "adm:tunnel")
async def cb_adm_tunnel(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        peers_data = await _wc().get_peers()
        now = int(asyncio.get_event_loop().time())
        import time as _time; now_ts = int(_time.time())

        # Стеки
        stacks_lines = []
        for p in s.get("plugins", []):
            active = p["name"] == s.get("active_stack")
            icon = "🟢" if active else "⚪"
            stacks_lines.append(f"  {icon} <code>{p['name']}</code> (устойч. {p['resilience']})")

        # Пиры: считаем активные (handshake < 3 мин назад)
        peers = peers_data.get("peers", [])
        active_peers = sum(
            1 for p in peers
            if p.get("last_handshake", 0) > 0 and now_ts - p.get("last_handshake", 0) < 180
        )
        total_peers = len(peers)

        failover = s.get("last_failover") or "никогда"
        rotation = (s.get("next_rotation") or "N/A")[:16].replace("T", " ")

        text = (
            f"<b>Туннель</b>\n\n"
            f"Активный стек: <code>{s.get('active_stack')}</code>\n"
            f"Primary: <code>{s.get('primary_stack')}</code>\n"
            f"Последний failover: {failover}\n"
            f"Следующая ротация: {rotation}\n\n"
            f"<b>Стеки:</b>\n" + "\n".join(stacks_lines) +
            f"\n\n<b>WG пиры:</b> {active_peers} активных / {total_peers} всего"
        )
    except WatchdogError as e:
        text = f"❌ Ошибка: {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "adm:ip")
async def cb_adm_ip(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        ip = s.get("external_ip") or "неизвестен"
        text = f"<b>Внешний IP:</b> <code>{ip}</code>"
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "adm:docker")
async def cb_adm_docker(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        import aiohttp as _aiohttp
        docker_host = os.getenv("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://")
        async with _aiohttp.ClientSession() as session:
            async with session.get(f"{docker_host}/containers/json?all=1", timeout=_aiohttp.ClientTimeout(total=10)) as r:
                containers = await r.json()
        if not containers:
            text = "Нет контейнеров."
        else:
            rows = []
            for c in sorted(containers, key=lambda x: x.get("Names", [""])[0]):
                name   = (c.get("Names") or ["?"])[0].lstrip("/")
                state  = c.get("State", "?")
                status = c.get("Status", "?")
                if state == "running":
                    emoji = "🟢"
                elif state == "exited":
                    emoji = "🔴"
                else:
                    emoji = "🟡"
                rows.append(f"{emoji} <code>{name}</code> — {status}")
            text = "<b>Docker контейнеры:</b>\n\n" + "\n".join(rows)
    except Exception as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "adm:speed")
async def cb_adm_speed(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        metrics_raw = await _wc().get_metrics()

        # Парсим Prometheus метрики
        metrics: dict[str, str] = {}
        for line in metrics_raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key = line.split("{")[0].split(" ")[0]
            val = line.rsplit(" ", 1)[-1]
            metrics[key] = val

        sys_info = s.get("system", {})
        cpu  = sys_info.get("cpu_percent", metrics.get("vpn_cpu_percent", "?"))
        ram  = sys_info.get("ram_percent",  metrics.get("vpn_ram_used_percent", "?"))
        disk = sys_info.get("disk_percent", metrics.get("vpn_disk_used_percent", "?"))

        def _fmt_bytes(b_str: str) -> str:
            try:
                b = int(float(b_str))
                if b >= 1_000_000_000:
                    return f"{b/1_000_000_000:.1f} GB"
                if b >= 1_000_000:
                    return f"{b/1_000_000:.1f} MB"
                return f"{b/1_000:.1f} KB"
            except Exception:
                return "?"

        rx = _fmt_bytes(metrics.get("vpn_bytes_recv_total", "0"))
        tx = _fmt_bytes(metrics.get("vpn_bytes_sent_total", "0"))

        text = (
            f"<b>Мониторинг ресурсов</b>\n\n"
            f"<b>Система:</b>\n"
            f"  CPU: <b>{cpu}%</b>\n"
            f"  RAM: <b>{ram}%</b>\n"
            f"  Диск: <b>{disk}%</b>\n\n"
            f"<b>Трафик (всего):</b>\n"
            f"  ↓ Получено: <b>{rx}</b>\n"
            f"  ↑ Отправлено: <b>{tx}</b>\n\n"
            f"<b>Стек:</b> <code>{s.get('active_stack')}</code>\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}"
        )
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Действия управления
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("adm:sw:"))
async def cb_adm_switch(cb: CallbackQuery, **kw):
    stack = cb.data[len("adm:sw:"):]
    try:
        await _wc().switch_stack(stack)
        text = f"🔄 Переключение на `{stack}` запущено"
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.answer()
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data.startswith("adm:rs:"))
async def cb_adm_restart(cb: CallbackQuery, **kw):
    svc = cb.data[len("adm:rs:"):]
    await cb.answer(f"Перезапускаю {svc}...")
    try:
        r = await _wc().restart_service(svc)
        st = r.get("status", "?")
        text = f"✅ `{svc}` перезапущен" if st == "ok" else f"⚠️ {r.get('error', 'ошибка')}"
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:update")
async def cb_adm_update(cb: CallbackQuery, state: FSMContext, **kw):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Обновить все", callback_data="update_all"),
        InlineKeyboardButton(text="❌ Отмена",       callback_data="update_cancel"),
    ]])
    await cb.answer()
    await cb.message.answer(
        "⚠️ *Обновление Docker образов*\n"
        "Сервисы будут кратковременно недоступны.",
        reply_markup=kb,
    )
    await state.set_state(AdminFSM.update_confirm)


@router.callback_query(F.data == "adm:deploy")
async def cb_adm_deploy(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю deploy...")
    try:
        await _wc().deploy()
        text = "🚀 Deploy запущен. Отчёт придёт по завершении."
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:rollback")
async def cb_adm_rollback(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю откат...")
    try:
        await _wc().rollback()
        text = "⏮️ Откат запущен..."
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:reboot")
async def cb_adm_reboot(cb: CallbackQuery, state: FSMContext, **kw):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data="reboot_yes"),
        InlineKeyboardButton(text="❌ Отмена",            callback_data="reboot_no"),
    ]])
    await cb.answer()
    await cb.message.answer(
        "⚠️ *Перезагрузить сервер?*\nКлиенты потеряют соединение на ~2 мин.",
        reply_markup=kb,
    )
    await state.set_state(AdminFSM.reboot_confirm)


# ---------------------------------------------------------------------------
# Действия маршрутов
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:list_vpn")
async def cb_adm_list_vpn(cb: CallbackQuery, **kw):
    await cb.answer()
    if not MANUAL_VPN.exists():
        await cb.message.answer("Список VPN пуст.", reply_markup=back_to_admin_menu())
        return
    lines = [ln.strip() for ln in MANUAL_VPN.read_text().splitlines() if ln.strip()]
    text = ("*Список VPN:*\n" + "\n".join(f"• `{ln}`" for ln in lines[:50])) if lines else "Список VPN пуст."
    if len(lines) > 50:
        text += f"\n... и ещё {len(lines) - 50}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:list_direct")
async def cb_adm_list_direct(cb: CallbackQuery, **kw):
    await cb.answer()
    if not MANUAL_DIRECT.exists():
        await cb.message.answer("Список Direct пуст.", reply_markup=back_to_admin_menu())
        return
    lines = [ln.strip() for ln in MANUAL_DIRECT.read_text().splitlines() if ln.strip()]
    text = ("*Список Direct:*\n" + "\n".join(f"• `{ln}`" for ln in lines[:50])) if lines else "Список Direct пуст."
    if len(lines) > 50:
        text += f"\n... и ещё {len(lines) - 50}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:routes_update")
async def cb_adm_routes_update(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю обновление маршрутов...")
    try:
        await _wc().update_routes()
        autodist = kw.get("autodist")
        if autodist:
            autodist.trigger("/routes update")
        text = "✅ Обновление маршрутов запущено (~2-5 мин)"
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:routes_info")
async def cb_adm_routes_info(cb: CallbackQuery, **kw):
    await cb.answer()
    await cb.message.answer(
        "*Управление маршрутами через команды:*\n\n"
        "`/vpn add <домен>` — добавить в VPN\n"
        "`/vpn remove <домен>` — убрать из VPN\n"
        "`/direct add <домен>` — добавить в прямые\n"
        "`/direct remove <домен>` — убрать из прямых\n"
        "`/check <домен>` — проверить домен\n"
        "`/routes update` — обновить все маршруты",
        reply_markup=back_to_admin_menu(),
    )


# ---------------------------------------------------------------------------
# Действия с клиентами
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:invite")
async def cb_adm_invite(cb: CallbackQuery, bot: Bot, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    code = await db.create_invite_code(str(cb.from_user.id))
    me = await bot.get_me()
    bot_link = f"https://t.me/{me.username}" if me.username else ""
    await cb.message.answer(
        f"🎫 <b>Код приглашения готов</b>\n\n"
        f"Действителен 24 часа.\n"
        f"Перешлите клиенту ссылку на бота и код ниже.\n\n"
        f"{bot_link}",
        parse_mode="HTML",
    )
    await cb.message.answer(f"<code>{code}</code>", parse_mode="HTML")


@router.callback_query(F.data == "adm:clients_list")
async def cb_adm_clients_list(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    clients = await db.get_all_clients()
    if not clients:
        await cb.message.answer("Нет зарегистрированных клиентов.", reply_markup=back_to_admin_menu())
        return
    await _edit_or_answer(cb, "👥 <b>Клиенты</b> — выберите для управления:", admin_clients_list_kb(clients))


@router.callback_query(F.data == "adm:requests")
async def cb_adm_requests(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_pending_devices()
    for d in devices[:5]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"dev_approve_{d['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dev_reject_{d['id']}"),
        ]])
        await cb.message.answer(
            f"📱 *Устройство на модерации*\n"
            f"Клиент: `{d.get('username') or d['chat_id']}`\n"
            f"Устройство: `{d['device_name']}`\n"
            f"Протокол: `{d['protocol'].upper()}`",
            reply_markup=kb,
        )
    reqs = await db.get_pending_requests()
    for r in reqs[:10]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"req_approve_{r['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req_reject_{r['id']}"),
        ]])
        icon = "🔒" if r["direction"] == "vpn" else "🌐"
        await cb.message.answer(
            f"{icon} *Запрос #{r['id']}*\n"
            f"Домен: `{r['domain']}`  ({r['direction']})\n"
            f"От: `{r['chat_id']}`  {r['created_at'][:16]}",
            reply_markup=kb,
        )
    if not devices and not reqs:
        await cb.message.answer("Нет ожидающих запросов.", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Действия VPS
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:vps_list")
async def cb_adm_vps_list(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        data = await _wc().get_vps_list()
        vps_list = data.get("vps_list", [])
        if not vps_list:
            await cb.message.answer("VPS не добавлены.", reply_markup=back_to_admin_menu())
            return
        await _edit_or_answer(
            cb,
            "🖥️ <b>VPS серверы</b> — нажмите для удаления:",
            admin_vps_list_kb(vps_list, data.get("active_idx", 0)),
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Действия безопасности
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:rotate_keys")
async def cb_adm_rotate_keys(cb: CallbackQuery, **kw):
    await cb.answer()
    await cb.message.answer(
        "⚠️ Ротация ключей сбросит все клиентские конфиги.\n"
        "Функция реализуется через deploy.sh --rotate-keys\n"
        "Запустите: `/deploy`",
        reply_markup=back_to_admin_menu(),
    )


@router.callback_query(F.data == "adm:renew_cert")
async def cb_adm_renew_cert(cb: CallbackQuery, **kw):
    await cb.answer("Обновляю сертификат...")
    result = subprocess.run(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "client"],
        capture_output=True, text=True, timeout=60,
    )
    ok = result.returncode == 0
    await cb.message.answer(
        f"{'✅' if ok else '❌'} Обновление клиентского сертификата mTLS:\n"
        f"```\n{(result.stdout or result.stderr)[:500]}\n```",
        reply_markup=back_to_admin_menu(),
    )


@router.callback_query(F.data == "adm:renew_ca")
async def cb_adm_renew_ca(cb: CallbackQuery, **kw):
    await cb.answer("Обновляю CA...")
    result = subprocess.run(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "ca"],
        capture_output=True, text=True, timeout=60,
    )
    ok = result.returncode == 0
    await cb.message.answer(
        f"{'✅' if ok else '❌'} Обновление CA:\n"
        f"```\n{(result.stdout or result.stderr)[:500]}\n```",
        reply_markup=back_to_admin_menu(),
    )


@router.callback_query(F.data == "adm:diagnose_menu")
async def cb_adm_diagnose_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    # Все устройства всех клиентов
    clients = await db.get_all_clients()
    devices = []
    for c in clients:
        devs = await db.get_devices(c["chat_id"])
        devices.extend(devs)
    if not devices:
        await cb.message.answer("Нет устройств для диагностики.", reply_markup=back_to_admin_menu())
        return
    await _edit_or_answer(cb, "🔍 <b>Диагностика</b> — выберите устройство:", admin_diagnose_kb(devices))


# ---------------------------------------------------------------------------
# Логи через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:logs_menu")
async def cb_adm_logs_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📋 <b>Логи</b> — выберите сервис:", admin_logs_menu())


@router.callback_query(F.data.startswith("adm:log:"))
async def cb_adm_log(cb: CallbackQuery, **kw):
    service = cb.data[len("adm:log:"):]
    await cb.answer(f"Загружаю логи {service}...")
    allowed_docker = {"telegram-bot", "xray-client", "xray-client-2", "cloudflared", "node-exporter"}
    if service in allowed_docker:
        cmd = ["docker", "logs", "--tail", "50", service]
    else:
        cmd = ["journalctl", "-u", service, "-n", "50", "--no-pager", "--output=short"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        text = result.stdout or result.stderr or "(нет логов)"
        if len(text) > 4000:
            from aiogram.types import BufferedInputFile
            await cb.message.answer_document(
                BufferedInputFile(text.encode(), filename=f"{service}.log"),
                caption=f"Логи `{service}`",
            )
        else:
            await cb.message.answer(f"*Логи {service}:*\n```\n{text[-3900:]}\n```",
                                    reply_markup=back_to_admin_menu())
    except Exception as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Графики через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:graph_menu")
async def cb_adm_graph_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📉 <b>Графики</b> — выберите панель:", admin_graph_menu())


@router.callback_query(F.data.startswith("adm:gr:"))
async def cb_adm_graph(cb: CallbackQuery, **kw):
    panel = cb.data[len("adm:gr:"):]
    await cb.answer(f"Загружаю график {panel}...")
    try:
        from aiogram.types import BufferedInputFile
        png = await _wc().get_graph(panel, "1h")
        if png:
            await cb.message.answer_photo(
                BufferedInputFile(png, filename="graph.png"),
                caption=f"График `{panel}` за 1ч",
            )
        else:
            await cb.message.answer("Grafana не вернула изображение", reply_markup=back_to_admin_menu())
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Маршруты: добавить/удалить/проверить через FSM
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:vpn_add")
async def cb_adm_vpn_add(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Введите домен для добавления в VPN\n(например: `example.com`):")
    await state.set_state(AdminFSM.vpn_add_domain)


@router.message(AdminFSM.vpn_add_domain)
async def fsm_vpn_add_domain(message: Message, state: FSMContext, **kw):
    domain = message.text.strip().lower().strip(".")
    await state.clear()
    _file_add_line(MANUAL_VPN, domain)
    try:
        await _wc().update_routes()
        autodist = kw.get("autodist")
        if autodist:
            autodist.trigger(f"/vpn add {domain}")
    except WatchdogError:
        pass
    await message.answer(f"✅ `{domain}` добавлен в VPN. Маршруты обновляются...",
                         reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:direct_add")
async def cb_adm_direct_add(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Введите домен для добавления в Direct\n(например: `example.com`):")
    await state.set_state(AdminFSM.direct_add_domain)


@router.message(AdminFSM.direct_add_domain)
async def fsm_direct_add_domain(message: Message, state: FSMContext, **kw):
    domain = message.text.strip().lower().strip(".")
    await state.clear()
    _file_add_line(MANUAL_DIRECT, domain)
    try:
        await _wc().update_routes()
    except WatchdogError:
        pass
    await message.answer(f"✅ `{domain}` добавлен в Direct. Маршруты обновляются...",
                         reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:vpn_remove")
async def cb_adm_vpn_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    if not MANUAL_VPN.exists():
        await cb.message.answer("Список VPN пуст.", reply_markup=back_to_admin_menu())
        return
    domains = [ln.strip() for ln in MANUAL_VPN.read_text().splitlines() if ln.strip()]
    if not domains:
        await cb.message.answer("Список VPN пуст.", reply_markup=back_to_admin_menu())
        return
    await _edit_or_answer(cb, "➖ <b>Удалить из VPN</b> — выберите домен:",
                          domains_inline_kb(domains, "adm:vpn_rm:", "adm:routes"))


@router.callback_query(F.data == "adm:direct_remove")
async def cb_adm_direct_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    if not MANUAL_DIRECT.exists():
        await cb.message.answer("Список Direct пуст.", reply_markup=back_to_admin_menu())
        return
    domains = [ln.strip() for ln in MANUAL_DIRECT.read_text().splitlines() if ln.strip()]
    if not domains:
        await cb.message.answer("Список Direct пуст.", reply_markup=back_to_admin_menu())
        return
    await _edit_or_answer(cb, "➖ <b>Удалить из Direct</b> — выберите домен:",
                          domains_inline_kb(domains, "adm:direct_rm:", "adm:routes"))


@router.callback_query(F.data.startswith("adm:vpn_rm:"))
async def cb_adm_vpn_rm(cb: CallbackQuery, **kw):
    domain = cb.data[len("adm:vpn_rm:"):]
    _file_remove_line(MANUAL_VPN, domain)
    try:
        await _wc().update_routes()
    except WatchdogError:
        pass
    await cb.answer(f"Удалено: {domain}")
    await cb.message.edit_text(f"✅ `{domain}` удалён из VPN. Маршруты обновляются...")


@router.callback_query(F.data.startswith("adm:direct_rm:"))
async def cb_adm_direct_rm(cb: CallbackQuery, **kw):
    domain = cb.data[len("adm:direct_rm:"):]
    _file_remove_line(MANUAL_DIRECT, domain)
    try:
        await _wc().update_routes()
    except WatchdogError:
        pass
    await cb.answer(f"Удалено: {domain}")
    await cb.message.edit_text(f"✅ `{domain}` удалён из Direct. Маршруты обновляются...")


@router.callback_query(F.data == "adm:check")
async def cb_adm_check(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Введите домен для проверки:")
    await state.set_state(AdminFSM.check_domain)


@router.message(AdminFSM.check_domain)
async def fsm_check_domain(message: Message, state: FSMContext, **kw):
    domain = message.text.strip().lower().strip(".")
    await state.clear()
    in_vpn    = MANUAL_VPN.exists() and domain in MANUAL_VPN.read_text()
    in_direct = MANUAL_DIRECT.exists() and domain in MANUAL_DIRECT.read_text()
    label = "🔒 VPN" if in_vpn else ("🌐 Direct" if in_direct else "❓ не задан")
    await message.answer(f"`{domain}`: {label}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Рассылка через меню (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Введите текст рассылки:")
    await state.set_state(AdminFSM.broadcast_input)


@router.message(AdminFSM.broadcast_input)
async def fsm_broadcast_input(message: Message, state: FSMContext, **kw):
    text = message.text.strip()
    await state.clear()
    db: Database = kw.get("db")
    bot = kw.get("bot")
    clients = await db.get_all_clients()
    sent = 0
    for c in clients:
        if not c.get("is_disabled") and c["chat_id"] != str(message.from_user.id):
            try:
                await bot.send_message(c["chat_id"], f"📢 *Объявление:*\n\n{text}")
                sent += 1
            except Exception:
                pass
    await message.answer(f"✅ Отправлено {sent}/{len(clients)} клиентам.",
                         reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Действия с конкретным клиентом
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("adm:cl:"))
async def cb_adm_client(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl:"):]
    db: Database = kw.get("db")
    client = await db.get_client(chat_id)
    if not client:
        await cb.answer("Клиент не найден")
        return
    name = client.get("first_name") or client.get("username") or chat_id
    devices = await db.get_devices(chat_id)
    text = (
        f"👤 *{name}*\n"
        f"ID: `{chat_id}`\n"
        f"Устройств: {len(devices)}\n"
        f"Статус: {'🚫 отключён' if client.get('is_disabled') else '✅ активен'}\n"
        f"Лимит устройств: {client.get('device_limit', 5)}"
    )
    await _edit_or_answer(cb, text, admin_client_actions_kb(chat_id, bool(client.get("is_disabled"))))


@router.callback_query(F.data.startswith("adm:cl_dis:"))
async def cb_adm_client_disable(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_dis:"):]
    db: Database = kw.get("db")
    await db.set_client_disabled(chat_id, True)
    await cb.answer("Отключён")
    await cb.message.edit_text(f"🚫 Клиент `{chat_id}` отключён.")


@router.callback_query(F.data.startswith("adm:cl_en:"))
async def cb_adm_client_enable(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_en:"):]
    db: Database = kw.get("db")
    await db.set_client_disabled(chat_id, False)
    await cb.answer("Включён")
    await cb.message.edit_text(f"✅ Клиент `{chat_id}` включён.")


@router.callback_query(F.data.startswith("adm:cl_kick:"))
async def cb_adm_client_kick(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_kick:"):]
    db: Database = kw.get("db")
    bot = kw.get("bot")
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
    try:
        await bot.send_message(chat_id, "❌ Ваш доступ к VPN отозван.")
    except Exception:
        pass
    await cb.answer("Кикнут")
    await cb.message.edit_text(f"🦵 Клиент `{chat_id}` кикнут, устройства удалены.")


@router.callback_query(F.data.startswith("adm:cl_lim:"))
async def cb_adm_client_limit(cb: CallbackQuery, state: FSMContext, **kw):
    chat_id = cb.data[len("adm:cl_lim:"):]
    await cb.answer()
    await state.update_data(_limit_chat_id=chat_id)
    await cb.message.answer(f"Введите новый лимит устройств для `{chat_id}`:")
    await state.set_state(AdminFSM.client_limit_input)


@router.message(AdminFSM.client_limit_input)
async def fsm_client_limit_input(message: Message, state: FSMContext, **kw):
    data = await state.get_data()
    chat_id = data.get("_limit_chat_id", "")
    await state.clear()
    if not message.text.isdigit():
        await message.answer("❌ Введите число.", reply_markup=back_to_admin_menu())
        return
    limit = int(message.text)
    db: Database = kw.get("db")
    await db.set_client_limit(chat_id, limit)
    await message.answer(f"✅ Лимит для `{chat_id}` = {limit}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# VPS: добавить/удалить через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:vps_add")
async def cb_adm_vps_add(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Введите IP-адрес нового VPS\n(опционально: `IP:PORT`, по умолчанию порт 443):")
    await state.set_state(AdminFSM.vps_add_ip)


@router.message(AdminFSM.vps_add_ip)
async def fsm_vps_add_ip(message: Message, state: FSMContext, **kw):
    await state.clear()
    parts = message.text.strip().split(":")
    ip = parts[0].strip()
    port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 443
    try:
        await _wc().add_vps(ip, port)
        await message.answer(f"✅ VPS `{ip}:{port}` добавлен.", reply_markup=back_to_admin_menu())
    except WatchdogError as e:
        await message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data.startswith("adm:vps_rm:"))
async def cb_adm_vps_remove(cb: CallbackQuery, **kw):
    ip = cb.data[len("adm:vps_rm:"):]
    try:
        await _wc().remove_vps(ip)
        await cb.answer(f"Удалён: {ip}")
        await cb.message.edit_text(f"✅ VPS `{ip}` удалён.")
    except WatchdogError as e:
        await cb.answer(f"❌ {e}", show_alert=True)


# ---------------------------------------------------------------------------
# Диагностика через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("adm:diag:"))
async def cb_adm_diagnose(cb: CallbackQuery, **kw):
    device_name = cb.data[len("adm:diag:"):]
    await cb.answer(f"Диагностика {device_name}...")
    try:
        r = await _wc().diagnose(device_name)
        text = (
            f"*Диагностика `{device_name}`:*\n\n"
            f"WG peer: {'✅' if r.get('wg_peer_found') else '❌'}\n"
            f"DNS: {'✅' if r.get('dns_ok') else '❌'}\n"
            f"Туннель: {'✅' if r.get('tunnel_ok') else '❌'} "
            f"RTT: {r.get('tunnel_rtt_ms', '?')}ms\n"
            f"Заблокированные сайты: {'✅' if r.get('blocked_sites_ok') else '❌'}"
        )
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu())


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

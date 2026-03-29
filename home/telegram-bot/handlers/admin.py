"""
handlers/admin.py — Все команды администратора

Команды (из CLAUDE.md):
  /status /tunnel /ip /docker /speed /logs /graph
  /switch /restart /upgrade /deploy /rollback
  /invite /clients /broadcast /requests
  /vpn add|remove   /direct add|remove   /list vpn|direct   /check
  /routes update    /vps list|add|remove  /migrate-vps
  /dpi [on|off|add|remove|toggle]
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
    admin_admin_actions_kb,
    admin_admins_menu,
    admin_client_actions_kb,
    admin_clients_list_kb,
    admin_clients_menu,
    admin_diagnose_kb,
    admin_dpi_menu,
    admin_graph_menu,
    admin_logs_menu,
    admin_main_menu,
    admin_manage_menu,
    admin_monitor_menu,
    admin_restart_menu,
    admin_routes_menu,
    admin_security_menu,
    admin_switch_menu,
    admin_system_menu,
    admin_tunnel_menu,
    admin_vps_actions_kb,
    admin_vps_list_kb,
    admin_vps_menu,
    back_to_admin_menu,
    client_main_menu,
    confirm_kb,
    domains_inline_kb,
    menu_reply_kb,
)
from services.watchdog_client import WatchdogClient, WatchdogError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)
router = Router()


def _installed_version_label() -> str:
    try:
        version = Path("/opt/vpn/version").read_text(encoding="utf-8").strip()
        if version and all(ch.isdigit() or ch == "." for ch in version):
            return f"v{version}"
    except Exception:
        pass
    return "неизвестно"


def _dpi_status_summary(st: dict) -> tuple[str, str, str]:
    enabled = st.get("enabled", False)
    zapret = st.get("zapret_running", False)
    services = st.get("services", [])
    active_services = [svc for svc in services if svc.get("enabled")]

    if enabled and zapret and active_services:
        return "✅ ВКЛЮЧЁН", "🟢", ""
    if enabled and not active_services:
        return "⚠️ НЕТ СЕРВИСОВ", "🔴", "Добавь хотя бы один сервис через /dpi add"
    if enabled and not zapret:
        return "⚠️ НЕАКТИВЕН", "🔴", "zapret/nfqws не запущен"
    return "❌ ВЫКЛЮЧЕН", "🔴", ""


def _display_name(client: dict, fallback: str = "") -> str:
    """Вернуть читаемое имя клиента, отфильтровав пустые/невидимые символы."""
    import re
    _re_visible = re.compile(r'\w', re.UNICODE)
    for field in ("first_name", "username"):
        val = (client.get(field) or "").strip()
        if val and _re_visible.search(val):
            return val
    return fallback or client.get("chat_id", "?")


async def _docker_logs(service: str, n: int = 50) -> str:
    """Получить логи Docker-контейнера через socket-proxy API."""
    import aiohttp as _aiohttp
    import struct
    docker_host = os.getenv("DOCKER_HOST", "tcp://socket-proxy:2375").replace("tcp://", "http://")
    url = f"{docker_host}/containers/{service}/logs?stdout=1&stderr=1&tail={n}&timestamps=0"
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 404:
                    return f"(контейнер {service} не найден)"
                raw = await r.read()
        # Docker multiplexed stream: 8-byte header per chunk
        lines = []
        offset = 0
        while offset + 8 <= len(raw):
            size = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
            chunk = raw[offset + 8:offset + 8 + size]
            lines.append(chunk.decode("utf-8", errors="replace"))
            offset += 8 + size
        return "".join(lines) or "(нет логов)"
    except Exception as e:
        return f"(ошибка получения логов: {e})"

MANUAL_VPN    = Path("/etc/vpn-routes/manual-vpn.txt")
MANUAL_DIRECT = Path("/etc/vpn-routes/manual-direct.txt")
ALLOWED_SERVICES = {
    "dnsmasq", "watchdog", "hysteria2", "docker",
    "wg-quick@wg0", "wg-quick@wg1", "nftables",
}


# ---------------------------------------------------------------------------
# Проверка прав
# ---------------------------------------------------------------------------
async def _is_admin(message: Message, db: Database | None = None, **kw) -> bool:
    uid = str(message.from_user.id)
    if uid == str(config.admin_chat_id):
        return True
    if db is not None:
        return await db.is_admin(uid)
    return False


def _is_root(message: Message) -> bool:
    return str(message.from_user.id) == str(config.admin_chat_id)


async def _is_admin_uid(uid: int, db: Database | None = None) -> bool:
    if str(uid) == str(config.admin_chat_id):
        return True
    if db is not None:
        return await db.is_admin(str(uid))
    return False


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


async def _require_admin(message: Message, db: Database | None = None, **kw) -> bool:
    if not await _is_admin(message, db=db):
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
    vps_install_ip     = State()
    vps_install_port   = State()
    vps_install_pass   = State()
    client_limit_input = State()


# ---------------------------------------------------------------------------
# FSM-прерыватели — приоритет выше всех FSM-состояний
# Любая команда или кнопка «Меню» → сброс FSM
# ---------------------------------------------------------------------------
@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel_any(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("❌ Действие отменено.", reply_markup=menu_reply_kb())
    else:
        await message.answer("Нет активного действия.", reply_markup=menu_reply_kb())


@router.message(F.text == "📋 Меню", StateFilter("*"))
async def reply_menu_any_state(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    await message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await message.answer("<b>Меню администратора</b>", reply_markup=admin_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
@router.message(Command("status"), StateFilter("*"))
async def cmd_status(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    try:
        s = await _wc().get_status()
        sys = s.get("system", {})
        lan_str = ""
        lan_clients = s.get("lan_clients")
        if lan_clients is not None:
            lan_ips = s.get("lan_client_ips", [])
            ip_str = ", ".join(lan_ips[:5]) + ("..." if len(lan_ips) > 5 else "")
            lan_str = f"\nLAN-клиентов: {lan_clients}" + (f" ({ip_str})" if ip_str else "")
        text = (
            f"*Статус системы*\n\n"
            f"Режим: {'⚠️ Деградированный' if s.get('degraded_mode') else '✅ Нормальный'}\n"
            f"Стек: `{s.get('active_stack')}`\n"
            f"IP: `{s.get('external_ip', 'N/A')}`\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}\n"
            f"Failover: {s.get('last_failover') or 'никогда'}"
            f"{lan_str}\n\n"
            f"CPU: {sys.get('cpu_percent', '?')}%  "
            f"RAM: {sys.get('ram_percent', '?')}%  "
            f"Диск: {sys.get('disk_percent', '?')}%"
        )
    except WatchdogError as e:
        text = f"❌ Watchdog недоступен: {e}"
    await message.answer(text)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
@router.message(Command("health"), StateFilter("*"))
async def cmd_health(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    try:
        h = await _wc().get_health()
    except Exception as e:
        await message.answer(f"❌ Watchdog недоступен: {e}")
        return

    score  = h.get("score", 0)
    status = h.get("status", "unknown")
    tier   = h.get("tier", "?")
    ts     = h.get("timestamp", "")
    pdw    = h.get("post_deploy_watch", False)
    summary = h.get("summary", {})

    if score >= 80:
        score_emoji = "✅"
    elif score >= 50:
        score_emoji = "⚠️"
    else:
        score_emoji = "🔴"

    lines = [
        f"{score_emoji} <b>Health Score: {score:.0f}/100</b> — {status}",
        f"Tier: {tier} | {ts}",
    ]
    if pdw:
        lines.append("🔍 <i>Post-deploy watch активен</i>")
    lines.append(
        f"✅ {summary.get('ok', 0)}  ⚠️ {summary.get('warn', 0)}  ❌ {summary.get('fail', 0)}"
    )
    lines.append("")

    icon_map = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    for check in h.get("checks", []):
        icon   = icon_map.get(check.get("status", "warn"), "❓")
        name   = check.get("name", "?")
        detail = check.get("detail", "")
        line   = f"{icon} <code>{name}</code>"
        if detail:
            line += f" — {detail}"
        lines.append(line)

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /tunnel
# ---------------------------------------------------------------------------
@router.message(Command("tunnel"), StateFilter("*"))
async def cmd_tunnel(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    args = message.text.split()
    allowed = ["watchdog", "dnsmasq", "hysteria2", "telegram-bot",
               "xray-client-vision", "xray-client-xhttp", "cloudflared", "node-exporter"]
    if len(args) < 2 or args[1] not in allowed:
        await message.answer(
            "Использование: `/logs <сервис> [N]`\n"
            "Доступные: " + ", ".join(f"`{s}`" for s in allowed)
        )
        return
    service = args[1]
    n = min(int(args[2]), 300) if len(args) > 2 and args[2].isdigit() else 50

    docker_services = {"telegram-bot", "xray-client-vision", "xray-client-xhttp", "cloudflared", "node-exporter"}
    try:
        if service in docker_services:
            text = await _docker_logs(service, n)
        else:
            result = subprocess.run(
                ["journalctl", "-u", service, "-n", str(n), "--no-pager", "--output=short"],
                capture_output=True, text=True, timeout=15,
            )
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    args = message.text.split()
    stacks = ["cloudflare-cdn", "reality-xhttp", "vless-reality-vision", "hysteria2"]
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    try:
        await _wc().deploy()
        await message.answer("🚀 Deploy запущен. Отчёт придёт по завершении.")
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


@router.message(Command("rollback"), StateFilter("*"))
async def cmd_rollback(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
# Уведомление об обновлении: [Обновить] / [Пропустить]
# callback_data: "update:confirm:<version>" / "update:skip:<version>"
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("update:confirm:"))
async def cb_update_confirm(cb: CallbackQuery, **kw):
    version = cb.data[len("update:confirm:"):]
    await cb.answer(f"Запускаю обновление до {version}...")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"🚀 Запускаю деплой {version}...")
    try:
        await _wc().deploy()
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка деплоя: {e}")


@router.callback_query(F.data.startswith("update:skip:"))
async def cb_update_skip(cb: CallbackQuery, **kw):
    version = cb.data[len("update:skip:"):]
    await cb.answer(f"Версия {version} пропущена")
    await cb.message.edit_reply_markup(reply_markup=None)
    # Записать пропущенную версию на сервер через watchdog
    try:
        await _wc().skip_version(version)
        await cb.message.answer(f"⏭ Версия `{version}` пропущена. Следующее обновление не будет напоминать о ней.",
                                 parse_mode="Markdown")
    except Exception as e:
        await cb.message.answer(f"⏭ Версия {version} пропущена (локально).\n_{e}_", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /admin list|invite|remove
# ---------------------------------------------------------------------------
@router.message(Command("admin"), StateFilter("*"))
async def cmd_admin(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    db: Database = kw.get("db")
    args = message.text.split()
    sub = args[1].lower() if len(args) > 1 else "list"

    if sub == "list":
        root_id = str(config.admin_chat_id)
        try:
            root_chat = await kw.get("bot").get_chat(int(root_id))
            root_name = f"@{root_chat.username}" if root_chat.username else (root_chat.first_name or root_id)
        except Exception:
            root_name = root_id

        extra_admins = await db.get_all_admins()
        extra_admins = [a for a in extra_admins if str(a["chat_id"]) != root_id]

        lines = ["*Администраторы*\n", f"👑 Root: {root_name} (ID: `{root_id}`)"]
        if extra_admins:
            lines.append("\nДополнительные:")
            for a in extra_admins:
                name = f"@{a['username']}" if a.get("username") else (a.get("first_name") or a["chat_id"])
                added_by = a.get("admin_added_by") or "?"
                date = (a.get("created_at") or "")[:10]
                lines.append(f"• {name} — добавил `{added_by}` ({date})")
        else:
            lines.append("\n_Дополнительных администраторов нет_")
        await message.answer("\n".join(lines))

    elif sub == "invite":
        if not _is_root(message):
            await message.answer("❌ Только root-администратор может создавать admin-инвайты.")
            return
        code = await db.create_invite_code(str(message.from_user.id), grants_admin=True)
        await message.answer(
            f"*Admin\\-invite создан*\nКод: `{code}`\nДействителен: 24 часа",
            parse_mode="MarkdownV2",
        )

    elif sub == "remove":
        if not _is_root(message):
            await message.answer("❌ Только root-администратор может снимать права администратора.")
            return
        if len(args) < 3:
            await message.answer("Использование: `/admin remove <username_или_id>`")
            return
        target_name = args[2].lstrip("@")
        target = await db.find_client_by_name(target_name)
        if not target:
            await message.answer("❌ Пользователь не найден.")
            return
        if str(target["chat_id"]) == str(config.admin_chat_id):
            await message.answer("❌ Нельзя удалить root-администратора.")
            return
        if not target.get("is_admin"):
            await message.answer("❌ Пользователь не является администратором.")
            return
        await db.set_admin(target["chat_id"], False, None)
        name = f"@{target['username']}" if target.get("username") else target["chat_id"]
        await message.answer(f"✅ {name} снят с должности администратора.")

    else:
        await message.answer(
            "Использование:\n"
            "`/admin list` — список администраторов\n"
            "`/admin invite` — создать admin-инвайт (только root)\n"
            "`/admin remove <username>` — снять права (только root)"
        )


# ---------------------------------------------------------------------------
# /invite
# ---------------------------------------------------------------------------
@router.message(Command("invite"), StateFilter("*"))
async def cmd_invite(message: Message, state: FSMContext, bot: Bot, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    db: Database = kw.get("db")

    # Попытка создать bootstrap-инвайт с предсозданными пирами AWG + WG.
    # Это позволяет получить конфиги заранее и переслать их через любой мессенджер
    # (нужно для пользователей у которых Telegram заблокирован и нет VPN-доступа).
    wdc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        from services.config_builder import ConfigBuilder, wg_genkey
        builder = ConfigBuilder()

        # Генерируем пары ключей локально
        awg_privkey, awg_pubkey = wg_genkey()
        wg_privkey,  wg_pubkey  = wg_genkey()

        # Добавляем временные пиры на сервер (watchdog выдаёт IP)
        awg_resp = await wdc.add_peer(f"bootstrap-awg-{message.from_user.id}", "awg", awg_pubkey)
        wg_resp  = await wdc.add_peer(f"bootstrap-wg-{message.from_user.id}",  "wg",  wg_pubkey)

        # Небольшая пауза чтобы watchdog записал пир в wg0/wg1 и вернул IP
        await asyncio.sleep(1.5)

        # Получаем IP из ответа watchdog (ключ "ip") или вычисляем из пулов
        awg_ip = (awg_resp or {}).get("ip") or ""
        wg_ip  = (wg_resp  or {}).get("ip") or ""

        # Если watchdog не вернул IP — читаем из wg show (поле allowed_ips = "x.x.x.x/32")
        if not awg_ip or not wg_ip:
            peers_info = await wdc.get_peers()
            for p in (peers_info or {}).get("peers", []):
                if p.get("public_key") == awg_pubkey:
                    awg_ip = p.get("allowed_ips", "").split("/")[0]
                if p.get("public_key") == wg_pubkey:
                    wg_ip = p.get("allowed_ips", "").split("/")[0]

        code = await db.create_bootstrap_invite(
            str(message.from_user.id),
            awg_peer_id=awg_pubkey,
            wg_peer_id=wg_pubkey,
            awg_ip=awg_ip,
            wg_ip=wg_ip,
            awg_privkey=awg_privkey,
            wg_privkey=wg_privkey,
        )

        # Строим конфиги для обоих пиров
        awg_device = {
            "protocol": "awg", "private_key": awg_privkey, "public_key": awg_pubkey,
            "ip_address": awg_ip, "preshared_key": "",
        }
        wg_device = {
            "protocol": "wg", "private_key": wg_privkey, "public_key": wg_pubkey,
            "ip_address": wg_ip, "preshared_key": "",
        }
        awg_conf, awg_qr, _ = await builder.build(awg_device)
        wg_conf,  wg_qr,  _ = await builder.build(wg_device)

        me = await bot.get_me()
        bot_link = f"https://t.me/{me.username}" if me.username else "(открыть Telegram-бот)"

        await message.answer(
            f"🎫 <b>Bootstrap-инвайт создан.</b>\n\n"
            f"Перешлите клиенту конфиги ниже через любой мессенджер (WhatsApp, Email и т.д.).\n"
            f"Код для регистрации: <code>{code}</code>\n\n"
            f"<b>Сценарий A — Telegram заблокирован (основной):</b>\n"
            f"1. Установите AmneziaWG или WireGuard\n"
            f"2. Импортируйте один из конфигов (.conf или QR)\n"
            f"3. Включите VPN → откройте Telegram → напишите /start\n"
            f"4. Введите код: <code>{code}</code>\n"
            f"→ Конфиг останется прежним, AWG-пир сохранится как постоянный.\n\n"
            f"<b>Сценарий B — Telegram уже доступен (без VPN):</b>\n"
            f"1. Напишите боту /start\n"
            f"2. Введите код: <code>{code}</code>\n"
            f"→ Временные конфиги будут удалены, бот создаст и пришлёт новый конфиг.\n\n"
            f"Бот: {bot_link}\n"
            f"⏳ Bootstrap-конфиги активны 24 часа.",
            parse_mode="HTML",
        )
        # AWG конфиг + QR
        await message.answer_document(
            BufferedInputFile(awg_conf.encode(), filename="vpn-bootstrap-awg.conf"),
            caption="📄 AmneziaWG конфиг (рекомендуется)",
        )
        if awg_qr:
            await message.answer_photo(BufferedInputFile(awg_qr, filename="awg-qr.png"),
                                        caption="QR для AmneziaWG")
        # WG конфиг + QR
        await message.answer_document(
            BufferedInputFile(wg_conf.encode(), filename="vpn-bootstrap-wg.conf"),
            caption="📄 WireGuard конфиг (запасной)",
        )
        if wg_qr:
            await message.answer_photo(BufferedInputFile(wg_qr, filename="wg-qr.png"),
                                        caption="QR для WireGuard")
        return

    except Exception as e:
        logger.warning(f"/invite: bootstrap не удался ({e}), создаём обычный инвайт")

    # Fallback: обычный инвайт без предсозданных пиров (watchdog недоступен)
    code = await db.create_invite_code(str(message.from_user.id))
    me = await bot.get_me()
    bot_link = f"https://t.me/{me.username}" if me.username else None
    await message.answer(
        f"🎫 <b>Код приглашения создан.</b>\n\n"
        f"Перешлите клиенту два сообщения ниже 👇",
        parse_mode="HTML",
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👉 Открыть бота", url=bot_link)
    ]]) if bot_link else None
    await message.answer(
        f"Для подключения к VPN:\n"
        f"1. Скопируйте код из следующего сообщения 👇\n"
        f"2. Нажмите кнопку ниже чтобы открыть бота\n"
        f"3. Нажмите «Старт» и введите код",
        reply_markup=kb,
    )
    await message.answer(f"<code>{code}</code>", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /clients
# ---------------------------------------------------------------------------
@router.message(Command("clients"), StateFilter("*"))
async def cmd_clients(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
        name = _display_name(c, c["chat_id"])
        lines.append(f"{icon} `{name}` (id: `{c['chat_id']}`)")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /client disable|enable|kick|limit <имя> [значение]
# ---------------------------------------------------------------------------
@router.message(Command("client"), StateFilter("*"))
async def cmd_client(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
        if str(chat_id) == str(config.admin_chat_id):
            await message.answer("❌ Нельзя кикнуть root-администратора.")
            return
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
        if client.get("is_admin"):
            await db.set_admin(chat_id, False, None)
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
    if not await _is_admin(message, db=kw.get("db")):
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
                await bot.send_message(c["chat_id"], f"📢 *Объявление:*\n\n{args[1]}", reply_markup=menu_reply_kb())
                sent += 1
                await asyncio.sleep(0.05)  # 20 msg/sec — Telegram rate limit
            except Exception:
                pass
    await message.answer(f"✅ Отправлено {sent}/{len(clients)} клиентам.")


# ---------------------------------------------------------------------------
# /requests — запросы клиентов
# ---------------------------------------------------------------------------
@router.message(Command("requests"), StateFilter("*"))
async def cmd_requests(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin_uid(cb.from_user.id, kw.get("db")):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
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
    if not await _is_admin_uid(cb.from_user.id, kw.get("db")):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
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
    if not await _is_admin_uid(cb.from_user.id, kw.get("db")):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
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
    if not await _is_admin_uid(cb.from_user.id, kw.get("db")):
        await cb.answer("❌ Доступ запрещён", show_alert=True)
        return
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/check <домен>`")
        return
    domain = args[1].lower().strip(".")
    try:
        r = await _wc().check_domain(domain)
        verdict   = r.get("verdict", "unknown")
        ips       = r.get("ips", [])
        ip_str    = ", ".join(ips[:4]) if ips else "не резолвится"
        sources   = []
        if r.get("in_manual_vpn"):     sources.append("manual-vpn")
        if r.get("in_blocked_static"): sources.append("blocked_static")
        if r.get("in_blocked_dynamic"):sources.append("blocked_dynamic")
        if r.get("in_manual_direct"):  sources.append("manual-direct")
        src = " | ".join(sources) if sources else "—"
        icon = {"vpn": "🔒", "direct": "🌐", "unknown": "❓"}.get(verdict, "❓")
        await message.answer(
            f"{icon} <code>{domain}</code>\nВердикт: <b>{verdict}</b>\nIP: <code>{ip_str}</code>\nИсточники: {src}",
            parse_mode="HTML",
        )
    except WatchdogError as e:
        await message.answer(f"❌ {e}")


# ---------------------------------------------------------------------------
# /routes update
# ---------------------------------------------------------------------------
@router.message(Command("routes"), StateFilter("*"))
async def cmd_routes(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
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
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    try:
        data = await _wc().renew_cert()
        ok = data.get("ok", False)
        out = data.get("output", "")
    except Exception as e:
        ok, out = False, str(e)
    await message.answer(
        f"{'✅' if ok else '❌'} Обновление клиентского сертификата mTLS:\n"
        f"```\n{out[:500]}\n```"
    )


@router.message(Command("renew_ca"), StateFilter("*"))
async def cmd_renew_ca(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    try:
        data = await _wc().renew_ca()
        ok = data.get("ok", False)
        out = data.get("output", "")
    except Exception as e:
        ok, out = False, str(e)
    await message.answer(
        f"{'✅' if ok else '❌'} Обновление CA:\n"
        f"```\n{out[:500]}\n```"
    )


# ---------------------------------------------------------------------------
# /diagnose <устройство>
# ---------------------------------------------------------------------------
@router.message(Command("diagnose"), StateFilter("*"))
async def cmd_diagnose(message: Message, state: FSMContext, **kw):
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/diagnose <устройство>`")
        return
    device_name = args[1]
    try:
        r = await asyncio.wait_for(_wc().diagnose(device_name), timeout=15)
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
    if not await _is_admin(message, db=kw.get("db")):
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


def _log_result_kb(origin: str) -> InlineKeyboardMarkup:
    back_cb = "adm:logs_monitor" if origin == "monitor" else "adm:logs_system"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К логам", callback_data=back_cb)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="adm:menu")],
    ])


@router.callback_query(F.data == "adm:menu")
async def cb_adm_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "<b>Меню администратора</b>", admin_main_menu())


@router.callback_query(F.data == "adm:tunnel_menu")
async def cb_adm_tunnel_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📡 <b>Туннель</b>", admin_tunnel_menu())


@router.callback_query(F.data == "adm:system")
async def cb_adm_system(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "🔧 <b>Система</b>", admin_system_menu())


@router.callback_query(F.data == "adm:monitor")
async def cb_adm_monitor(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📊 <b>Мониторинг</b>", admin_monitor_menu())


@router.callback_query(F.data == "adm:logs_system")
async def cb_adm_logs_system(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📋 <b>Логи</b> — выберите сервис:", admin_logs_menu("adm:system", "adm:log:system:"))


@router.callback_query(F.data == "adm:logs_monitor")
async def cb_adm_logs_monitor(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📋 <b>Логи</b> — выберите сервис:", admin_logs_menu("adm:monitor", "adm:log:monitor:"))


@router.callback_query(F.data == "adm:dashboard")
async def cb_adm_dashboard(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю дашборд...")
    import time as _time
    now_ts = int(_time.time())

    try:
        s, metrics_raw = await asyncio.gather(
            _wc().get_status(),
            _wc().get_metrics(),
            return_exceptions=True,
        )
        if isinstance(s, Exception):
            raise WatchdogError(str(s))

        # Parse metrics
        metrics: dict[str, str] = {}
        if not isinstance(metrics_raw, Exception):
            for line in str(metrics_raw).splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                key = line.split("{")[0].split(" ")[0]
                val = line.rsplit(" ", 1)[-1]
                metrics[key] = val

        sys_info = s.get("system", {})
        active_stack = s.get("active_stack", "N/A")
        primary = s.get("primary_stack", "—")
        external_ip = s.get("external_ip", "N/A")
        uptime = _uptime(s.get("uptime_seconds", 0))
        last_failover = s.get("last_failover") or "никогда"
        disk = sys_info.get("disk_percent", metrics.get("vpn_disk_used_percent", "?"))
        cpu = sys_info.get("cpu_percent", "?")
        ram = sys_info.get("ram_percent", "?")
        mode_str = "⚠️ Деградированный" if s.get("degraded_mode") else "✅ Нормальный"
        rotation = (s.get("next_rotation") or "—")[:16].replace("T", " ")

        # Count online peers
        online_peers = 0
        try:
            peers_data = await _wc().get_peers()
            peers = peers_data.get("peers", [])
            online_peers = sum(
                1 for p in peers
                if p.get("last_handshake", 0) > 0
                and now_ts - p.get("last_handshake", 0) < 180
                and p.get("interface", "") not in ("tun0", "tun1")
            )
        except WatchdogError:
            pass

        # VPS RTT from metrics
        rtt_str = ""
        rtt_val = metrics.get("vpn_tunnel_rtt_ms")
        if rtt_val and rtt_val not in ("", "0"):
            try:
                rtt_str = f" | RTT: {float(rtt_val):.0f}ms"
            except Exception:
                pass

        vps_ips = [v["ip"] for v in s.get("vps_list", [])]
        vps_str = ", ".join(f"<code>{ip}</code>" for ip in vps_ips) if vps_ips else "—"
        version_label = _installed_version_label()

        text = (
            f"<b>🏠 Дашборд</b>\n\n"
            f"<b>Версия:</b> <code>{version_label}</code>\n"
            f"<b>Режим:</b> {mode_str}\n"
            f"<b>Туннель:</b> <code>{active_stack}</code>{rtt_str}\n"
            f"<b>Primary:</b> <code>{primary}</code>\n"
            f"<b>Ротация:</b> {rotation}\n"
            f"<b>Домашний IP:</b> <code>{external_ip}</code>\n"
            f"<b>VPS:</b> {vps_str}\n"
            f"<b>Онлайн клиентов:</b> {online_peers}\n\n"
            f"<b>Ресурсы:</b>\n"
            f"  CPU: <b>{cpu}%</b>  RAM: <b>{ram}%</b>  Диск: <b>{disk}%</b>\n\n"
            f"<b>Uptime:</b> {uptime}\n"
            f"<b>Последний failover:</b> {last_failover}"
        )
    except WatchdogError as e:
        text = f"❌ Watchdog недоступен: {e}"

    refresh_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:dashboard")],
        [InlineKeyboardButton(text="◀️ В меню",   callback_data="adm:menu")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=refresh_kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=refresh_kb, parse_mode="HTML")


@router.callback_query(F.data == "adm:manage")
async def cb_adm_manage(cb: CallbackQuery, **kw):
    """Обратная совместимость — перенаправляет в раздел Система."""
    await _edit_or_answer(cb, "🔧 <b>Система</b>", admin_system_menu())


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
    """Обратная совместимость — перенаправляет в раздел Система."""
    await _edit_or_answer(cb, "🔧 <b>Система</b>", admin_system_menu())


@router.callback_query(F.data == "adm:switch_menu")
async def cb_adm_switch_menu(cb: CallbackQuery, **kw):
    active_stack = ""
    try:
        s = await _wc().get_status()
        active_stack = s.get("active_stack", "")
    except WatchdogError:
        pass
    await _edit_or_answer(cb, "🔄 <b>Выберите стек:</b>", admin_switch_menu(active_stack))


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
        lan_str = ""
        lan_clients = s.get("lan_clients")
        if lan_clients is not None:
            lan_ips = s.get("lan_client_ips", [])
            ip_str = ", ".join(lan_ips[:5]) + ("..." if len(lan_ips) > 5 else "")
            lan_str = f"\nLAN-клиентов: <b>{lan_clients}</b>" + (f" ({ip_str})" if ip_str else "")
        text = (
            f"<b>Статус системы</b>\n\n"
            f"Режим: {mode}\n"
            f"Стек: <code>{s.get('active_stack')}</code>\n"
            f"Primary: <code>{s.get('primary_stack')}</code>\n"
            f"IP: <code>{s.get('external_ip', 'N/A')}</code>\n"
            f"Uptime: {_uptime(s.get('uptime_seconds', 0))}\n"
            f"Failover: {failover}\n"
            f"Ротация: {rotation}"
            f"{lan_str}\n\n"
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

        # Стеки (только туннельные — без zapret, он DPI bypass, не туннель)
        stacks_lines = []
        for p in s.get("plugins", []):
            if p["name"] == "zapret":
                continue
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
    tunnel_back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Туннель", callback_data="adm:tunnel_menu")],
    ])
    await cb.message.answer(text, reply_markup=tunnel_back_kb, parse_mode="HTML")


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


@router.callback_query(F.data == "adm:stats")
async def cb_adm_stats(cb: CallbackQuery, **kw):
    """Статистика трафика по клиентам (из wg show dump)."""
    await cb.answer("Загружаю...")
    db: Database = kw.get("db")

    def _fmt_bytes(n: int) -> str:
        if n >= 1_000_000_000:
            return f"{n/1_000_000_000:.2f} GB"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f} MB"
        return f"{n/1_000:.0f} KB"

    bot: Bot = kw.get("bot")

    try:
        peers_data = await _wc().get_peers()
        peers = peers_data.get("peers", [])

        clients = await db.get_all_clients()

        # Подтягиваем имена из Telegram API для клиентов с пустым именем
        if bot:
            for c in clients:
                if not (c.get("first_name") or "").strip() and not (c.get("username") or "").strip():
                    try:
                        chat = await bot.get_chat(int(c["chat_id"]))
                        fn = (chat.first_name or "").strip()
                        un = (chat.username or "").strip()
                        if fn or un:
                            await db.update_client_info(c["chat_id"], un, fn)
                            c["first_name"] = fn
                            c["username"] = un
                    except Exception:
                        pass

        # Строим map: public_key -> device info
        pk_to_dev: dict[str, dict] = {}
        for client in clients:
            chat_id = str(client["chat_id"])
            devices = await db.get_devices(chat_id)
            for d in devices:
                pk = d.get("public_key") or d.get("peer_id")
                if pk:
                    pk_to_dev[pk] = {
                        "chat_id": chat_id,
                        "device_name": d.get("device_name", "?"),
                        "first_name": _display_name(client, chat_id),
                    }

        # Агрегируем трафик по клиентам
        import time as _time
        now_ts = int(_time.time())
        client_traffic: dict[str, dict] = {}
        orphans = []
        system_peers = []
        for p in peers:
            pk = p.get("public_key", "")
            rx = p.get("rx_bytes", 0)
            tx = p.get("tx_bytes", 0)
            hs = p.get("last_handshake", 0)
            dev_info = pk_to_dev.get(pk)
            if dev_info:
                cid = dev_info["chat_id"]
                if cid not in client_traffic:
                    client_traffic[cid] = {
                        "name": dev_info["first_name"],
                        "rx": 0, "tx": 0,
                        "devices": [],
                        "active": 0,
                    }
                client_traffic[cid]["rx"] += rx
                client_traffic[cid]["tx"] += tx
                active = hs > 0 and now_ts - hs < 180
                if active:
                    client_traffic[cid]["active"] += 1
                hs_str = f"{(now_ts - hs) // 60} мин" if hs > 0 else "никогда"
                client_traffic[cid]["devices"].append(
                    f"  {'🟢' if active else '⚪'} {dev_info['device_name']}: "
                    f"↓{_fmt_bytes(rx)} ↑{_fmt_bytes(tx)} | {hs_str}"
                )
            else:
                iface = p.get("interface", "")
                orphans.append(f"  <code>{pk[:20]}…</code> [{iface}] ↓{_fmt_bytes(rx)} ↑{_fmt_bytes(tx)}")

        if not client_traffic:
            text = "📊 <b>Статистика трафика</b>\n\nНет данных."
        else:
            lines = ["📊 <b>Статистика трафика по клиентам</b>\n"]
            for cid, info in sorted(client_traffic.items(), key=lambda x: -(x[1]["rx"] + x[1]["tx"])):
                lines.append(
                    f"👤 <b>{info['name']}</b> ({cid})\n"
                    f"  Итого: ↓{_fmt_bytes(info['rx'])} ↑{_fmt_bytes(info['tx'])}\n"
                    + "\n".join(info["devices"])
                )
            if system_peers:
                lines.append("🖧 <b>Системные пиры:</b>\n" + "\n".join(system_peers))
            if orphans:
                lines.append("⚠️ <b>Неизвестные пиры:</b>\n" + "\n".join(orphans))
            text = "\n\n".join(lines)

    except WatchdogError as e:
        text = f"❌ {e}"

    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "adm:speedtest")
async def cb_adm_speedtest(cb: CallbackQuery, **kw):
    await cb.answer("Получаю данные...")
    try:
        status = await _wc().get_status()
        active = status.get("active_stack", "—")

        metrics_raw = await _wc().get_metrics()
        metrics: dict[str, str] = {}
        for line in str(metrics_raw).splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key = line.split("{")[0].split(" ")[0]
            val = line.rsplit(" ", 1)[-1]
            metrics[key] = val

        def _f(key: str) -> str:
            try:
                v = float(metrics.get(key, "0"))
                return f"{v:.1f}" if v > 0 else "—"
            except Exception:
                return "—"

        rtt_val = metrics.get("vpn_tunnel_rtt_ms", "")
        rtt_str = f"{float(rtt_val):.0f} мс" if rtt_val and rtt_val not in ("0", "") else "—"

        dl = _f("vpn_tunnel_download_mbps")
        ul = _f("vpn_tunnel_upload_mbps")

        text = (
            f"<b>⚡ Скорость туннеля</b>\n\n"
            f"Активный стек: <b>{active}</b>\n"
            f"RTT до VPS: <b>{rtt_str}</b>\n\n"
            f"↓ Скачивание: <b>{dl} Мбит/с</b>\n"
            f"↑ Загрузка:   <b>{ul} Мбит/с</b>\n\n"
            f"<i>Фоновый пробник 100 KB — показывает актуальность канала.\n"
            f"Для реального замера throughput используйте «Тест стеков».</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Тест стеков", callback_data="adm:assess")],
            [InlineKeyboardButton(text="◀️ Мониторинг", callback_data="adm:monitor")],
        ])
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:backup")
async def cb_adm_backup(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю бэкап...")
    try:
        await _wc().backup()
        await cb.message.answer(
            "🗄 <b>Бэкап запущен</b>\n\nАрхив будет отправлен в этот чат по завершении.",
            reply_markup=back_to_admin_menu(),
            parse_mode="HTML",
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data == "adm:backup_export")
async def cb_adm_backup_export(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю полный экспорт...")
    try:
        await _wc().backup_export()
        await cb.message.answer(
            "🗂 <b>Полный экспорт запущен</b>\n\nФайл придёт в этот чат (~30–60 сек).",
            reply_markup=back_to_admin_menu(),
            parse_mode="HTML",
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


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
    await cb.answer()
    await _edit_or_answer(
        cb,
        f"⚠️ Перезапустить <b>{svc}</b>?",
        confirm_kb(f"adm:rs_ok:{svc}", "adm:restart_menu"),
    )


@router.callback_query(F.data.startswith("adm:rs_ok:"))
async def cb_adm_restart_ok(cb: CallbackQuery, **kw):
    svc = cb.data[len("adm:rs_ok:"):]
    await cb.answer(f"Перезапускаю {svc}...")
    try:
        r = await _wc().restart_service(svc)
        st = r.get("status", "?")
        text = f"✅ <code>{svc}</code> перезапущен" if st == "ok" else f"⚠️ {r.get('error', 'ошибка')}"
    except WatchdogError as e:
        text = f"❌ {e}"
    await _edit_or_answer(cb, text, back_to_admin_menu())


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
    await cb.answer()
    await _edit_or_answer(
        cb,
        "🚀 <b>Применить апдейт?</b>\nСервисы кратковременно перезапустятся.",
        confirm_kb("adm:deploy_ok", "adm:system"),
    )


@router.callback_query(F.data == "adm:deploy_ok")
async def cb_adm_deploy_ok(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю deploy...")
    try:
        await _wc().deploy()
        text = "🚀 Deploy запущен. Отчёт придёт по завершении."
    except WatchdogError as e:
        text = f"❌ {e}"
    await _edit_or_answer(cb, text, back_to_admin_menu())


@router.callback_query(F.data == "adm:rollback")
async def cb_adm_rollback(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        "⏮️ <b>Откатить до предыдущей версии?</b>",
        confirm_kb("adm:rollback_ok", "adm:system"),
    )


@router.callback_query(F.data == "adm:rollback_ok")
async def cb_adm_rollback_ok(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю откат...")
    try:
        await _wc().rollback()
        text = "⏮️ Откат запущен..."
    except WatchdogError as e:
        text = f"❌ {e}"
    await _edit_or_answer(cb, text, back_to_admin_menu())


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
    """Кнопка «Пригласить» в меню — используем bootstrap-логику из cmd_invite."""
    await cb.answer()
    # Имитируем поведение cmd_invite: сообщения отправляем через cb.message.answer
    db: Database = kw.get("db")
    wdc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        from services.config_builder import ConfigBuilder, wg_genkey
        builder = ConfigBuilder()
        awg_privkey, awg_pubkey = wg_genkey()
        wg_privkey,  wg_pubkey  = wg_genkey()
        awg_resp = await wdc.add_peer(f"bootstrap-awg-{cb.from_user.id}", "awg", awg_pubkey)
        wg_resp  = await wdc.add_peer(f"bootstrap-wg-{cb.from_user.id}",  "wg",  wg_pubkey)
        await asyncio.sleep(1.5)
        awg_ip = (awg_resp or {}).get("ip") or ""
        wg_ip  = (wg_resp  or {}).get("ip") or ""
        if not awg_ip or not wg_ip:
            peers_info = await wdc.get_peers()
            for p in (peers_info or {}).get("peers", []):
                if p.get("public_key") == awg_pubkey:
                    awg_ip = p.get("allowed_ips", "").split("/")[0]
                if p.get("public_key") == wg_pubkey:
                    wg_ip = p.get("allowed_ips", "").split("/")[0]
        code = await db.create_bootstrap_invite(
            str(cb.from_user.id),
            awg_peer_id=awg_pubkey, wg_peer_id=wg_pubkey,
            awg_ip=awg_ip, wg_ip=wg_ip,
            awg_privkey=awg_privkey, wg_privkey=wg_privkey,
        )
        awg_conf, awg_qr, _ = await builder.build(
            {"protocol": "awg", "private_key": awg_privkey, "public_key": awg_pubkey,
             "ip_address": awg_ip, "preshared_key": ""})
        wg_conf, wg_qr, _ = await builder.build(
            {"protocol": "wg", "private_key": wg_privkey, "public_key": wg_pubkey,
             "ip_address": wg_ip, "preshared_key": ""})
        me = await bot.get_me()
        bot_link = f"https://t.me/{me.username}" if me.username else "(открыть Telegram-бот)"
        await cb.message.answer(
            f"🎫 <b>Bootstrap-инвайт создан.</b>\n\n"
            f"Перешлите конфиги клиенту через любой мессенджер (WhatsApp, Email и т.д.).\n"
            f"Код: <code>{code}</code>\n\n"
            f"<b>Сценарий A — Telegram заблокирован:</b>\n"
            f"AWG/WG → включить VPN → /start → ввести код.\n"
            f"→ Конфиг останется прежним.\n\n"
            f"<b>Сценарий B — Telegram доступен без VPN:</b>\n"
            f"/start → ввести код (без VPN).\n"
            f"→ Временные конфиги удалятся, бот пришлёт новый.\n\n"
            f"Бот: {bot_link}\n⏳ Bootstrap-конфиги активны 24 часа.", parse_mode="HTML",
        )
        await cb.message.answer_document(
            BufferedInputFile(awg_conf.encode(), filename="vpn-bootstrap-awg.conf"),
            caption="📄 AmneziaWG (рекомендуется)")
        if awg_qr:
            await cb.message.answer_photo(BufferedInputFile(awg_qr, filename="awg-qr.png"))
        await cb.message.answer_document(
            BufferedInputFile(wg_conf.encode(), filename="vpn-bootstrap-wg.conf"),
            caption="📄 WireGuard (запасной)")
        if wg_qr:
            await cb.message.answer_photo(BufferedInputFile(wg_qr, filename="wg-qr.png"))
        return
    except Exception as e:
        logger.warning(f"cb_adm_invite: bootstrap не удался ({e}), создаём обычный инвайт")
    code = await db.create_invite_code(str(cb.from_user.id))
    me = await bot.get_me()
    bot_link = f"https://t.me/{me.username}" if me.username else None
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👉 Открыть бота", url=bot_link)
    ]]) if bot_link else None
    await cb.message.answer(
        f"🎫 <b>Код приглашения создан.</b>\n\nПерешлите клиенту два сообщения ниже 👇",
        parse_mode="HTML")
    await cb.message.answer(
        f"Для подключения к VPN:\n1. Скопируйте код 👇\n"
        f"2. Нажмите кнопку → откройте бота\n3. /start → введите код",
        reply_markup=kb)
    await cb.message.answer(f"<code>{code}</code>", parse_mode="HTML")


@router.callback_query(F.data == "adm:admin_list")
async def cb_adm_admin_list(cb: CallbackQuery, **kw):
    db: Database = kw.get("db")
    bot = kw.get("bot")
    root_id = str(config.admin_chat_id)
    is_root = str(cb.from_user.id) == root_id
    try:
        root_chat = await bot.get_chat(int(root_id))
        root_name = f"@{root_chat.username}" if root_chat.username else (root_chat.first_name or root_id)
    except Exception:
        root_name = root_id
    extra_admins = await db.get_all_admins()
    extra_admins = [a for a in extra_admins if str(a["chat_id"]) != root_id]
    lines = ["<b>Администраторы</b>\n", f"👑 Root: {root_name} (ID: <code>{root_id}</code>)"]
    if extra_admins:
        lines.append("\nДополнительные:")
        for a in extra_admins:
            name = f"@{a['username']}" if a.get("username") else (a.get("first_name") or a["chat_id"])
            added_by = a.get("admin_added_by") or "?"
            date = (a.get("created_at") or "")[:10]
            lines.append(f"• {name} — добавил <code>{added_by}</code> ({date})")
    else:
        lines.append("\n<i>Дополнительных администраторов нет</i>")
    if is_root:
        lines.append("\n<i>Создание и снятие прав доступны только root.</i>")
    await _edit_or_answer(cb, "\n".join(lines), admin_admins_menu(extra_admins, is_root))


@router.callback_query(F.data == "adm:admin_invite")
async def cb_adm_admin_invite(cb: CallbackQuery, **kw):
    await cb.answer()
    if str(cb.from_user.id) != str(config.admin_chat_id):
        await cb.message.answer("❌ Только root-администратор может создавать admin-инвайты.", reply_markup=back_to_admin_menu())
        return
    db: Database = kw.get("db")
    code = await db.create_invite_code(str(cb.from_user.id), grants_admin=True)
    await cb.message.answer(
        f"👥 <b>Admin-инвайт создан</b>\n\nКод: <code>{code}</code>\nДействителен: 24 часа",
        reply_markup=back_to_admin_menu(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm:admin:"))
async def cb_adm_admin_card(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:admin:"):]
    db: Database = kw.get("db")
    client = await db.get_client(chat_id)
    if not client or not client.get("is_admin"):
        await cb.answer("Администратор не найден", show_alert=True)
        return
    name = _display_name(client, chat_id)
    devices = await db.get_devices(chat_id)
    added_by = client.get("admin_added_by") or "root/bootstrap"
    created_at = (client.get("created_at") or "")[:16]
    is_root_target = str(chat_id) == str(config.admin_chat_id)
    can_demote = str(cb.from_user.id) == str(config.admin_chat_id) and not is_root_target
    role = "👑 Root-администратор" if is_root_target else "👥 Дополнительный администратор"
    text = (
        f"{role}\n\n"
        f"Имя: <b>{name}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Статус: {'🚫 отключён' if client.get('is_disabled') else '✅ активен'}\n"
        f"Устройств: {len(devices)} / {client.get('device_limit', 5)}\n"
        f"Выдал права: <code>{added_by}</code>\n"
        f"Создан: {created_at or 'неизвестно'}"
    )
    await _edit_or_answer(cb, text, admin_admin_actions_kb(chat_id, can_demote))


@router.callback_query(F.data.startswith("adm:admin_rm:"))
async def cb_adm_admin_remove(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:admin_rm:"):]
    if str(cb.from_user.id) != str(config.admin_chat_id):
        await cb.answer("Только root", show_alert=True)
        return
    if str(chat_id) == str(config.admin_chat_id):
        await cb.answer("Нельзя снять права у root", show_alert=True)
        return
    await _edit_or_answer(
        cb,
        f"⚠️ <b>Снять права администратора</b> у <code>{chat_id}</code>?",
        confirm_kb(f"adm:admin_rm_ok:{chat_id}", f"adm:admin:{chat_id}"),
    )


@router.callback_query(F.data.startswith("adm:admin_rm_ok:"))
async def cb_adm_admin_remove_ok(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:admin_rm_ok:"):]
    if str(cb.from_user.id) != str(config.admin_chat_id):
        await cb.answer("Только root", show_alert=True)
        return
    if str(chat_id) == str(config.admin_chat_id):
        await cb.answer("Нельзя снять права у root", show_alert=True)
        return
    db: Database = kw.get("db")
    changed = await db.set_admin(chat_id, False, None)
    text = f"✅ У пользователя <code>{chat_id}</code> сняты права администратора." if changed else "❌ Пользователь не найден."
    await _edit_or_answer(cb, text, back_to_admin_menu())


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
            "🖥️ <b>VPS серверы</b> — выберите для управления:",
            admin_vps_list_kb(vps_list, data.get("active_idx", 0)),
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data.startswith("adm:vps_detail:"))
async def cb_adm_vps_detail(cb: CallbackQuery, **kw):
    ip = cb.data[len("adm:vps_detail:"):]
    await cb.answer()
    try:
        data = await _wc().get_vps_list()
        vps_list = data.get("vps_list", [])
        vps = next((v for v in vps_list if v["ip"] == ip), None)
        if not vps:
            await cb.message.answer("VPS не найден.", reply_markup=back_to_admin_menu())
            return
        idx = vps_list.index(vps)
        status = "✅ Активный" if idx == data.get("active_idx", 0) else "⚪ Резервный"
        ssh_port = vps.get("ssh_port", 22)
        text = f"🖥️ <b>VPS: {ip}</b>\nSSH порт: {ssh_port}\nСтатус: {status}"
        await _edit_or_answer(cb, text, admin_vps_actions_kb(ip, ssh_port))
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data.startswith("adm:vps_test:"))
async def cb_adm_vps_test(cb: CallbackQuery, **kw):
    ip = cb.data[len("adm:vps_test:"):]
    await cb.answer("Тестирую...")
    try:
        result = await _wc().post("diagnose/vps", {"ip": ip})
        status = result.get("status", "неизвестно")
        latency = result.get("latency_ms")
        text = f"🔍 <b>Тест VPS {ip}</b>\nСтатус: {status}"
        if latency is not None:
            text += f"\nЗадержка: {latency} мс"
        await cb.message.answer(text, reply_markup=back_to_admin_menu())
    except WatchdogError as e:
        await cb.message.answer(f"❌ Тест не удался: {e}", reply_markup=back_to_admin_menu())


@router.callback_query(F.data.startswith("adm:vps_migrate:"))
async def cb_adm_vps_migrate(cb: CallbackQuery, state: FSMContext, **kw):
    ip = cb.data[len("adm:vps_migrate:"):]
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"migrate_{ip}_0"),
        InlineKeyboardButton(text="❌ Нет", callback_data="migrate_cancel"),
    ]])
    await cb.message.answer(
        f"🔄 Мигрировать на VPS <code>{ip}</code>?\n\n"
        "Текущий активный VPS будет заменён. "
        "Для восстановления из бэкапа используйте `/migrate_vps {ip} --from-backup`.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(AdminFSM.migrate_confirm)


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
    try:
        data = await _wc().renew_cert()
        ok = data.get("ok", False)
        out = data.get("output", "")
    except Exception as e:
        ok, out = False, str(e)
    await cb.message.answer(
        f"{'✅' if ok else '❌'} Обновление клиентского сертификата mTLS:\n"
        f"```\n{out[:500]}\n```",
        reply_markup=back_to_admin_menu(),
    )


@router.callback_query(F.data == "adm:renew_ca")
async def cb_adm_renew_ca(cb: CallbackQuery, **kw):
    await cb.answer("Обновляю CA...")
    try:
        data = await _wc().renew_ca()
        ok = data.get("ok", False)
        out = data.get("output", "")
    except Exception as e:
        ok, out = False, str(e)
    await cb.message.answer(
        f"{'✅' if ok else '❌'} Обновление CA:\n"
        f"```\n{out[:500]}\n```",
        reply_markup=back_to_admin_menu(),
    )


# ---------------------------------------------------------------------------
# Fail2ban
# ---------------------------------------------------------------------------
def _f2b_format_server(label: str, server_id: str, jails: list[dict]) -> tuple[str, list]:
    """Возвращает (текст, кнопки_разбана) для одного сервера."""
    if not jails:
        return f"<b>{label}</b>: fail2ban недоступен\n", []
    lines = [f"<b>{label}</b>"]
    buttons = []
    has_any = False
    for j in jails:
        banned = j["banned"]
        total = j["total_banned"]
        lines.append(f"  🔒 <code>{j['jail']}</code>: {total} забанено")
        if banned:
            has_any = True
            for ip in banned[:10]:  # показываем до 10 IP
                lines.append(f"    • <code>{ip}</code>")
                # callback_data: f2b:u:{server_id}:{jail}:{ip}
                cd = f"f2b:u:{server_id}:{j['jail']}:{ip}"
                if len(cd) <= 64:
                    buttons.append(
                        InlineKeyboardButton(
                            text=f"🔓 {ip} ({j['jail']})",
                            callback_data=cd,
                        )
                    )
    if not has_any:
        lines.append("  ✅ нет заблокированных IP")
    return "\n".join(lines) + "\n", buttons


@router.callback_query(F.data == "adm:fail2ban")
async def cb_adm_fail2ban(cb: CallbackQuery, **kw):
    await cb.answer("Запрашиваю fail2ban...")
    try:
        data = await _wc().get_fail2ban_status()
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}", reply_markup=back_to_admin_menu())
        return

    home_jails = data.get("home", [])
    vps_list = data.get("vps", [])

    text_parts = []
    all_unban_buttons = []

    h_text, h_buttons = _f2b_format_server("🏠 Домашний сервер", "home", home_jails)
    text_parts.append(h_text)
    all_unban_buttons.extend(h_buttons)

    for vps in vps_list:
        vps_ip = vps["ip"]
        label = f"🖥️ VPS {vps_ip}"
        v_text, v_buttons = _f2b_format_server(label, vps_ip, vps.get("jails", []))
        text_parts.append(v_text)
        all_unban_buttons.extend(v_buttons)

    # Собираем клавиатуру: по 1 кнопке разбана в ряд + обновить + назад
    rows = [[btn] for btn in all_unban_buttons]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:fail2ban")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:system")])

    await _edit_or_answer(
        cb,
        "🛡️ <b>Fail2ban</b>\n\n" + "\n".join(text_parts),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("f2b:u:"))
async def cb_f2b_unban(cb: CallbackQuery, **kw):
    # f2b:u:{server}:{jail}:{ip}
    parts = cb.data.split(":", 4)  # ["f2b", "u", server, jail, ip]
    if len(parts) < 5:
        await cb.answer("Ошибка формата", show_alert=True)
        return
    _, _, server, jail, ip = parts
    await cb.answer(f"Разбаниваю {ip}...")
    try:
        result = await _wc().fail2ban_unban(server=server, jail=jail, ip=ip)
        ok = result.get("ok", False)
        out = result.get("output", "")
    except Exception as e:
        ok, out = False, str(e)

    label = "домашний сервер" if server == "home" else f"VPS {server}"
    if ok:
        await cb.message.answer(
            f"✅ IP <code>{ip}</code> разбанен в jail <code>{jail}</code> ({label})",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛡️ Обновить список", callback_data="adm:fail2ban")],
                [InlineKeyboardButton(text="◀️ Система", callback_data="adm:system")],
            ]),
        )
    else:
        await cb.message.answer(
            f"❌ Не удалось разбанить <code>{ip}</code>:\n<code>{out[:200]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛡️ К списку", callback_data="adm:fail2ban")],
            ]),
        )


@router.callback_query(F.data == "adm:diagnose_menu")
async def cb_adm_diagnose_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    # Все устройства всех клиентов с owner_name
    clients = await db.get_all_clients()
    devices = []
    for c in clients:
        owner_name = _display_name(c, c["chat_id"])
        devs = await db.get_devices(c["chat_id"])
        for d in devs:
            d_copy = dict(d)
            d_copy["owner_name"] = owner_name
            devices.append(d_copy)
    if not devices:
        await cb.message.answer("Нет устройств для диагностики.", reply_markup=back_to_admin_menu())
        return
    await _edit_or_answer(cb, "🔍 <b>Диагностика</b> — выберите устройство:", admin_diagnose_kb(devices))


# ---------------------------------------------------------------------------
# Логи через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:logs_menu")
async def cb_adm_logs_menu(cb: CallbackQuery, **kw):
    await _edit_or_answer(cb, "📋 <b>Логи</b> — выберите сервис:", admin_logs_menu("adm:system", "adm:log:system:"))


@router.callback_query(F.data.startswith("adm:log:"))
async def cb_adm_log(cb: CallbackQuery, **kw):
    payload = cb.data[len("adm:log:"):]
    origin = "system"
    service = payload
    if ":" in payload:
        maybe_origin, maybe_service = payload.split(":", 1)
        if maybe_origin in {"system", "monitor"}:
            origin = maybe_origin
            service = maybe_service
    await cb.answer(f"Загружаю логи {service}...")
    allowed_docker = {"telegram-bot", "xray-client-vision", "xray-client-xhttp", "cloudflared", "node-exporter"}
    try:
        if service in allowed_docker:
            text = await _docker_logs(service, 50)
        else:
            result = subprocess.run(
                ["journalctl", "-u", service, "-n", "50", "--no-pager", "--output=short"],
                capture_output=True, text=True, timeout=15,
            )
            text = result.stdout or result.stderr or "(нет логов)"
        if len(text) > 4000:
            from aiogram.types import BufferedInputFile
            await cb.message.answer_document(
                BufferedInputFile(text.encode(), filename=f"{service}.log"),
                caption=f"Логи `{service}`",
                reply_markup=_log_result_kb(origin),
            )
        else:
            await cb.message.answer(f"*Логи {service}:*\n```\n{text[-3900:]}\n```",
                                    reply_markup=_log_result_kb(origin))
    except Exception as e:
        await cb.message.answer(f"❌ {e}", reply_markup=_log_result_kb(origin))


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
    except Exception as e:
        logger.exception("cb_adm_graph error: %s", e)
        await cb.message.answer(f"❌ Ошибка рендеринга: {e}", reply_markup=back_to_admin_menu())


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
    domain = message.text.strip().lower().strip(".").split("/")[0]
    await state.clear()
    try:
        r = await _wc().check_domain(domain)
        verdict = r.get("verdict", "unknown")
        ips     = r.get("ips", [])
        ip_str  = ", ".join(ips[:4]) if ips else "не резолвится"
        sources = []
        if r.get("in_manual_vpn"):      sources.append("manual-vpn")
        if r.get("in_blocked_static"):  sources.append("blocked_static")
        if r.get("in_blocked_dynamic"): sources.append("blocked_dynamic")
        if r.get("in_manual_direct"):   sources.append("manual-direct")
        src  = " | ".join(sources) if sources else "—"
        icon = {"vpn": "🔒", "direct": "🌐", "unknown": "❓"}.get(verdict, "❓")
        text = (
            f"{icon} <code>{domain}</code>\n"
            f"Вердикт: <b>{verdict}</b>\n"
            f"IP: <code>{ip_str}</code>\n"
            f"Источники: {src}"
        )
    except WatchdogError as e:
        text = f"❌ {e}"
    await message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


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
                await bot.send_message(c["chat_id"], f"📢 *Объявление:*\n\n{text}", reply_markup=menu_reply_kb())
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
    name = _display_name(client, chat_id)
    devices = await db.get_devices(chat_id)

    # Загружаем трафик из WG
    def _fmt_bytes(n: int) -> str:
        if n >= 1_000_000_000:
            return f"{n/1_000_000_000:.2f} GB"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f} MB"
        return f"{n/1_000:.0f} KB"

    import time as _time
    now_ts = int(_time.time())
    try:
        peers_data = await _wc().get_peers()
        pk_to_peer = {p["public_key"]: p for p in peers_data.get("peers", [])}
    except WatchdogError:
        pk_to_peer = {}

    dev_lines = []
    for d in devices:
        pk = d.get("public_key") or d.get("peer_id", "")
        proto = d.get("protocol", "?").upper()
        dname = d.get("device_name", "?")
        p = pk_to_peer.get(pk, {})
        hs = p.get("last_handshake", 0)
        rx = p.get("rx_bytes", 0)
        tx = p.get("tx_bytes", 0)
        if hs > 0:
            mins = (now_ts - hs) // 60
            hs_str = f"{mins} мин" if mins < 120 else f"{mins//60} ч"
            icon = "🟢" if mins < 3 else "🟡"
        else:
            hs_str = "никогда"
            icon = "⚪"
        dev_lines.append(
            f"{icon} <b>{dname}</b> [{proto}] — {hs_str} | ↓{_fmt_bytes(rx)} ↑{_fmt_bytes(tx)}"
        )

    devs_text = "\n".join(dev_lines) if dev_lines else "нет"
    if client.get("is_admin"):
        added_by = client.get("admin_added_by") or "root/bootstrap"
        is_root_target = str(chat_id) == str(config.admin_chat_id)
        can_demote = str(cb.from_user.id) == str(config.admin_chat_id) and not is_root_target
        role = "👑 Root-администратор" if is_root_target else "👥 Дополнительный администратор"
        text = (
            f"{role}\n\n"
            f"Имя: <b>{name}</b>\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Статус: {'🚫 отключён' if client.get('is_disabled') else '✅ активен'}\n"
            f"Устройств: {len(devices)} / {client.get('device_limit', 5)}\n"
            f"Выдал права: <code>{added_by}</code>\n\n"
            f"<b>Устройства:</b>\n{devs_text}"
        )
        await _edit_or_answer(cb, text, admin_admin_actions_kb(chat_id, can_demote))
        return
    text = (
        f"👤 <b>{name}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Статус: {'🚫 отключён' if client.get('is_disabled') else '✅ активен'}\n"
        f"Устройств: {len(devices)} / {client.get('device_limit', 5)}\n\n"
        f"<b>Устройства:</b>\n{devs_text}"
    )
    await _edit_or_answer(cb, text, admin_client_actions_kb(chat_id, bool(client.get("is_disabled"))))


@router.callback_query(F.data.startswith("adm:cl_dis:"))
async def cb_adm_client_disable(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_dis:"):]
    db: Database = kw.get("db")
    client = await db.get_client(chat_id)
    if client and client.get("is_admin"):
        await cb.answer("Сначала снимите права администратора", show_alert=True)
        return
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
    client = await db.get_client(chat_id) if db else None
    if client and client.get("is_admin"):
        await cb.answer("Для администратора сначала снимите права", show_alert=True)
        return
    await cb.answer()
    await _edit_or_answer(
        cb,
        f"🦵 <b>Кикнуть клиента {chat_id}?</b>\nВсе устройства будут удалены, доступ отозван.",
        confirm_kb(f"adm:cl_kick_ok:{chat_id}", f"adm:cl:{chat_id}"),
    )


@router.callback_query(F.data.startswith("adm:cl_kick_ok:"))
async def cb_adm_client_kick_ok(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_kick_ok:"):]
    if str(chat_id) == str(config.admin_chat_id):
        await cb.answer("❌ Нельзя кикнуть root-администратора", show_alert=True)
        return
    db: Database = kw.get("db")
    bot = kw.get("bot")
    client = await db.get_client(chat_id)
    devices = await db.get_devices(chat_id)
    wc = _wc()
    for d in devices:
        if d.get("public_key"):
            try:
                await wc.remove_peer(d["public_key"])
            except Exception:
                pass
        await db.delete_device(d["id"])
    if client and client.get("is_admin"):
        await db.set_admin(chat_id, False, None)
    await db.set_client_disabled(chat_id, True)
    try:
        await bot.send_message(chat_id, "❌ Ваш доступ к VPN отозван.")
    except Exception:
        pass
    await cb.answer("Кикнут")
    await _edit_or_answer(cb, f"🦵 Клиент <code>{chat_id}</code> кикнут, устройства удалены.", back_to_admin_menu())


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


@router.callback_query(F.data.startswith("adm:cl_reconnect:"))
async def cb_adm_client_reconnect(cb: CallbackQuery, **kw):
    chat_id = cb.data[len("adm:cl_reconnect:"):]
    await cb.answer("Сбрасываю подключения...")
    db: Database = kw.get("db")
    devices = await db.get_devices(chat_id)
    if not devices:
        await cb.message.answer("Нет устройств у клиента.", reply_markup=back_to_admin_menu())
        return
    results = []
    for d in devices:
        pubkey = d.get("public_key", "")
        dname = d.get("device_name", "?")
        proto = d.get("protocol", "wg")
        if not pubkey:
            results.append(f"⚪ {dname} — нет публичного ключа")
            continue
        iface = "wg0" if proto == "awg" else "wg1"
        try:
            result = subprocess.run(
                ["awg" if proto == "awg" else "wg", "set", iface, "peer", pubkey, "endpoint", ""],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                results.append(f"✅ {dname} — endpoint сброшен")
            else:
                results.append(f"⚠️ {dname} — {result.stderr.strip() or 'ошибка'}")
        except Exception as e:
            results.append(f"❌ {dname} — {e}")
    text = (
        f"🔄 <b>Реконнект клиента {chat_id}</b>\n\n"
        + "\n".join(results)
        + "\n\nКлиент должен переподключиться при следующем handshake."
    )
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# VPS: добавить/удалить через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:vps_add")
async def cb_adm_vps_add(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer(
        "🖥️ *Установка нового VPS*\n\n"
        "Введите *IP-адрес* свежеустановленного сервера Ubuntu:",
        parse_mode="Markdown",
    )
    await state.set_state(AdminFSM.vps_install_ip)


@router.message(AdminFSM.vps_install_ip)
async def fsm_vps_install_ip(message: Message, state: FSMContext, **kw):
    ip = message.text.strip()
    if not ip.replace(".", "").isdigit() or len(ip.split(".")) != 4:
        await message.answer("❌ Неверный формат IP. Попробуйте снова:")
        return
    await state.update_data(vps_ip=ip)
    await message.answer(
        f"IP: `{ip}` ✅\n\nВведите *SSH порт* (нажмите Enter или отправьте `22` для стандартного):",
        parse_mode="Markdown",
    )
    await state.set_state(AdminFSM.vps_install_port)


@router.message(AdminFSM.vps_install_port)
async def fsm_vps_install_port(message: Message, state: FSMContext, **kw):
    text = message.text.strip()
    port = int(text) if text.isdigit() else 22
    await state.update_data(vps_port=port)
    await message.answer(
        f"Порт: `{port}` ✅\n\n"
        "⚠️ *Введите root пароль.*\n"
        "Сообщение будет немедленно удалено из чата после получения.\n\n"
        "Пароль используется однократно для первичной настройки SSH-ключей "
        "и после установки root-доступ будет закрыт.",
        parse_mode="Markdown",
    )
    await state.set_state(AdminFSM.vps_install_pass)


@router.message(AdminFSM.vps_install_pass)
async def fsm_vps_install_pass(message: Message, state: FSMContext, **kw):
    password = message.text.strip()
    data = await state.get_data()
    ip = data.get("vps_ip", "")
    port = data.get("vps_port", 22)
    await state.clear()

    # Удалить сообщение с паролем из чата
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        f"🚀 Запускаю установку VPS `{ip}:{port}`...\n\n"
        f"Прогресс будет приходить сюда. Установка занимает *5–10 минут*.",
        parse_mode="Markdown",
    )
    try:
        await _wc().install_vps(ip, password, port)
    except WatchdogError as e:
        await message.answer(f"❌ Не удалось запустить установку: {e}", reply_markup=back_to_admin_menu())


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
    await cb.answer()
    await _edit_or_answer(
        cb,
        f"❌ <b>Удалить VPS {ip}?</b>",
        confirm_kb(f"adm:vps_rm_ok:{ip}", f"adm:vps_detail:{ip}"),
    )


@router.callback_query(F.data.startswith("adm:vps_rm_ok:"))
async def cb_adm_vps_remove_ok(cb: CallbackQuery, **kw):
    ip = cb.data[len("adm:vps_rm_ok:"):]
    try:
        await _wc().remove_vps(ip)
        await cb.answer(f"Удалён: {ip}")
        await _edit_or_answer(cb, f"✅ VPS <code>{ip}</code> удалён.", back_to_admin_menu())
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


# ---------------------------------------------------------------------------
# /dpi — управление DPI bypass (zapret lane)
# ---------------------------------------------------------------------------
KNOWN_PRESETS = {"youtube", "twitch", "discord"}


@router.message(Command("dpi"), StateFilter("*"))
async def cmd_dpi(message: Message, state: FSMContext, **kw):
    """
    /dpi                  — статус
    /dpi on               — включить
    /dpi off              — выключить
    /dpi add <preset|домен> [домен2 ...]  — добавить сервис
    /dpi remove <name>    — удалить
    /dpi toggle <name>    — вкл/выкл конкретный сервис
    """
    if not await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()

    parts = (message.text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    extra = parts[3:] if len(parts) > 3 else []

    wc = _wc()
    try:
        if sub == "on":
            await wc.dpi_enable()
            await message.answer("⚡ DPI bypass включается...")

        elif sub == "off":
            await wc.dpi_disable()
            await message.answer("⚡ DPI bypass выключается...")

        elif sub == "add":
            if not arg:
                await message.answer(
                    "Использование:\n"
                    "/dpi add youtube\n"
                    "/dpi add twitch\n"
                    "/dpi add discord\n"
                    "/dpi add <домен> [домен2 ...] — произвольные домены\n\n"
                    "Пресеты: youtube, twitch, discord"
                )
                return
            if arg.lower() in KNOWN_PRESETS:
                await wc.dpi_add_service(preset=arg.lower())
                await message.answer(f"✅ Сервис *{arg}* добавлен из пресета.")
            else:
                # Произвольные домены
                domains = [arg] + extra
                name = arg.split(".")[0]  # первый домен как имя
                await wc.dpi_add_service(name=name, display=arg, domains=domains)
                await message.answer(
                    f"✅ Добавлен кастомный сервис `{name}`:\n"
                    + "\n".join(f"• `{d}`" for d in domains)
                )

        elif sub == "remove":
            if not arg:
                await message.answer("Использование: /dpi remove <name>")
                return
            await wc.dpi_remove_service(arg)
            await message.answer(f"🗑 Сервис `{arg}` удалён.")

        elif sub == "toggle":
            if not arg:
                await message.answer("Использование: /dpi toggle <name>")
                return
            st = await wc.get_dpi_status()
            svc = next((s for s in st.get("services", []) if s["name"] == arg), None)
            if not svc:
                await message.answer(f"❌ Сервис `{arg}` не найден.")
                return
            new_state = not svc.get("enabled", True)
            await wc.dpi_toggle_service(arg, new_state)
            icon = "✅" if new_state else "❌"
            await message.answer(f"{icon} Сервис `{arg}`: {'включён' if new_state else 'выключен'}.")

        else:
            # Статус
            st = await wc.get_dpi_status()
            services = st.get("services", [])
            ip_count = st.get("dpi_direct_ip_count", 0)
            presets = st.get("presets", [])

            status_icon, zapret_icon, hint = _dpi_status_summary(st)

            lines = [
                f"⚡ *DPI bypass: {status_icon}*",
                f"nfqws: {zapret_icon}  |  IP в dpi_direct: {ip_count}",
                "",
            ]
            if hint:
                lines.append(f"_{hint}_")
                lines.append("")
            if services:
                lines.append("*Сервисы:*")
                for svc in services:
                    icon = "✅" if svc.get("enabled") else "❌"
                    n = len(svc.get("domains", []))
                    lines.append(
                        f"{icon} {svc.get('display', svc['name'])} "
                        f"(`{svc['name']}`, {n} доменов)"
                    )
            else:
                lines.append("_Сервисы не добавлены_")
                lines.append(f"\nДоступные пресеты: {', '.join(presets)}")

            lines += [
                "",
                "/dpi on · /dpi off",
                "/dpi add youtube · /dpi add twitch · /dpi add discord",
                "/dpi add <домен> — произвольный",
                "/dpi toggle <name> · /dpi remove <name>",
            ]
            await message.answer("\n".join(lines), parse_mode="Markdown")

    except WatchdogError as e:
        await message.answer(f"❌ Watchdog недоступен: {e}")


# ---------------------------------------------------------------------------
# Callback: adm:dpi — inline-меню DPI bypass
# ---------------------------------------------------------------------------

async def _show_dpi_menu(cb: CallbackQuery):
    """Отрисовать меню DPI bypass с текущим статусом."""
    wc = _wc()
    try:
        st = await wc.get_dpi_status()
    except WatchdogError as e:
        await cb.answer(f"Watchdog недоступен: {e}", show_alert=True)
        return
    enabled = st.get("enabled", False)
    services = st.get("services", [])
    ip_count = st.get("dpi_direct_ip_count", 0)
    status_icon, zapret_icon, hint = _dpi_status_summary(st)
    text = (
        f"⚡ <b>DPI bypass: {status_icon}</b>\n"
        f"nfqws: {zapret_icon}  |  IP в dpi_direct: {ip_count}"
    )
    if hint:
        text += f"\n<i>{hint}</i>"
    await _edit_or_answer(cb, text, admin_dpi_menu(enabled, services))


@router.callback_query(F.data == "adm:dpi")
async def cb_adm_dpi(cb: CallbackQuery, **kw):
    await cb.answer()
    await _show_dpi_menu(cb)


@router.callback_query(F.data == "adm:dpi_on")
async def cb_adm_dpi_on(cb: CallbackQuery, **kw):
    await cb.answer("Включаю DPI bypass...")
    try:
        await _wc().dpi_enable()
    except WatchdogError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    await _show_dpi_menu(cb)


@router.callback_query(F.data == "adm:dpi_off")
async def cb_adm_dpi_off(cb: CallbackQuery, **kw):
    await cb.answer("Выключаю DPI bypass...")
    try:
        await _wc().dpi_disable()
    except WatchdogError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    await _show_dpi_menu(cb)


@router.callback_query(F.data.startswith("adm:dpi_add:"))
async def cb_adm_dpi_add(cb: CallbackQuery, **kw):
    preset = cb.data.split(":", 2)[2]
    await cb.answer(f"Добавляю {preset}...")
    try:
        await _wc().dpi_add_service(preset=preset)
    except WatchdogError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    await _show_dpi_menu(cb)


@router.callback_query(F.data.startswith("adm:dpi_tog:"))
async def cb_adm_dpi_toggle(cb: CallbackQuery, **kw):
    name = cb.data.split(":", 2)[2]
    await cb.answer()
    wc = _wc()
    try:
        st = await wc.get_dpi_status()
        svc = next((s for s in st.get("services", []) if s["name"] == name), None)
        if not svc:
            await cb.answer(f"Сервис {name} не найден", show_alert=True)
            return
        new_state = not svc.get("enabled", True)
        await wc.dpi_toggle_service(name, new_state)
    except WatchdogError as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return
    await _show_dpi_menu(cb)


# ---------------------------------------------------------------------------
# Callback: adm:user_menu — показать меню пользователя
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:user_menu")
async def cb_adm_user_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        "👤 <b>Меню пользователя</b>\n\nВы просматриваете меню клиента.",
        client_main_menu(),
    )


# ---------------------------------------------------------------------------
# Callback: adm:nft_stats — статистика nft sets
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:nft_stats")
async def cb_adm_nft_stats(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        stats = await _wc().get_nft_stats()
        blocked_static  = stats.get("blocked_static", -1)
        blocked_dynamic = stats.get("blocked_dynamic", -1)
        dpi_direct      = stats.get("dpi_direct", -1)
        text = (
            f"<b>📊 Статистика nft sets</b>\n\n"
            f"<b>blocked_static</b> (базы РКН + геоблок): <b>{blocked_static}</b> IP\n"
            f"<b>blocked_dynamic</b> (DNS кэш): <b>{blocked_dynamic}</b> IP\n"
            f"<b>dpi_direct</b> (zapret): <b>{dpi_direct}</b> IP\n\n"
            f"<i>blocked_static</i> — обновляется раз в сутки:\n"
            f"  • antifilter.download — IP из реестра РКН\n"
            f"  • community.antifilter.download — сообщество\n"
            f"  • iplist.opencck.org — расширенный реестр\n"
            f"  • zapret-info/z-i — выгрузка Роскомнадзора\n"
            f"  • RockBlack-VPN — геоблок (230+ сервисов)\n\n"
            f"<i>blocked_dynamic</i> — наполняется dnsmasq при резолве заблокированных доменов (timeout 24ч)\n"
            f"<i>dpi_direct</i> — IP из DPI bypass (zapret/nfqws)"
        )
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: adm:dpi_test — тест DPI bypass
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:dpi_test")
async def cb_adm_dpi_test(cb: CallbackQuery, **kw):
    await cb.answer("Тестирую DPI...")
    try:
        data = await _wc().dpi_test()
        status = data.get("status", "?")
        results = data.get("results", [])
        if status == "disabled":
            await cb.message.answer("⚡ DPI bypass выключен.", reply_markup=back_to_admin_menu())
            return
        if not results:
            await cb.message.answer("Нет активных DPI сервисов для теста.", reply_markup=back_to_admin_menu())
            return
        lines = [f"<b>🧪 Тест DPI bypass</b>\n"]
        for r in results:
            icon = "✅" if r["ok"] else "❌"
            ips = ", ".join(r.get("resolved", [])[:2]) or "не резолвится"
            lines.append(f"{icon} <code>{r['domain']}</code>\n   IP: {ips}\n   В dpi_direct: {'да' if r['in_dpi_set'] else 'нет'}")
        text = "\n\n".join(lines)
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: adm:dpi_recheck — запустить probe zapret
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:dpi_recheck")
async def cb_adm_dpi_recheck(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю quick probe zapret...")
    try:
        data = await _wc().zapret_probe(mode="quick")
        status = data.get("status", "?")
        await cb.message.answer(
            f"🔄 <b>zapret probe запущен</b>\n\nРежим: quick\nСтатус: {status}\n\n"
            "Результат придёт в этот чат через ~30–60 секунд.",
            parse_mode="HTML",
            reply_markup=back_to_admin_menu(),
        )
    except WatchdogError as e:
        await cb.message.answer(f"❌ {e}", reply_markup=back_to_admin_menu())


# ---------------------------------------------------------------------------
# Callback: adm:dpi_history — история смен пресета zapret
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:dpi_history")
async def cb_adm_dpi_history(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        data = await _wc().get_zapret_history()
        history = data.get("history", [])
        if not history:
            text = "📋 <b>История пресетов zapret</b>\n\nИстория пуста — probe ещё не запускался."
        else:
            lines = ["📋 <b>История пресетов zapret</b>\n"]
            for entry in history:
                parts = entry.split(None, 2)
                if len(parts) >= 2:
                    ts = f"{parts[0]} {parts[1]}"
                    preset_id = parts[2].split(None, 1)[0] if len(parts) > 2 else "?"
                    desc = parts[2].split(None, 1)[1] if len(parts[2].split(None, 1)) > 1 else ""
                    lines.append(f"<code>{ts}</code>  <b>{preset_id}</b>  <i>{desc}</i>")
                else:
                    lines.append(f"<code>{entry}</code>")
            text = "\n".join(lines)
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: adm:rotation_log — журнал переключений стека
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:rotation_log")
async def cb_adm_rotation_log(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        data = await _wc().get_rotation_log()
        log = data.get("log", [])
        if not log:
            text = "📋 <b>Журнал ротаций</b>\n\nИстория переключений пуста."
        else:
            lines = ["📋 <b>Журнал переключений стека</b>\n"]
            for entry in log[:15]:
                ts = entry.get("ts", "?")[:16].replace("T", " ")
                frm = entry.get("from", "?")
                to = entry.get("to", "?")
                reason = entry.get("reason", "?")
                lines.append(f"<code>{ts}</code>\n  {frm} → <b>{to}</b>\n  <i>{reason}</i>")
            text = "\n\n".join(lines)
    except WatchdogError as e:
        text = f"❌ {e}"
    await cb.message.answer(text, reply_markup=back_to_admin_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: adm:broadcast_configs — рассылка конфигов всем клиентам
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:broadcast_configs")
async def cb_adm_broadcast_configs(cb: CallbackQuery, **kw):
    await cb.answer("Запускаю рассылку...")
    db: Database = kw.get("db")
    bot: "Bot" = kw.get("bot")
    from services.config_builder import ConfigBuilder
    import asyncio as _asyncio

    async def _do_broadcast():
        devices = await db.get_all_devices()
        builder = ConfigBuilder()
        sent = 0
        failed = 0
        for device in devices:
            try:
                chat_id = str(device["chat_id"])
                excludes_raw = await db.get_excludes(device["id"])
                excludes = [e["subnet"] for e in excludes_raw]
                conf_text, qr_bytes, version = await builder.build(device, excludes)
                if version == device.get("config_version"):
                    continue  # не изменился
                caption = (
                    f"📋 Конфиг <b>{device['device_name']}</b> обновлён\n"
                    f"⚠️ Приватный ключ — не пересылайте!"
                )
                from datetime import date as _date
                _fname = f"vpn-{device['device_name']}.conf" if device.get("is_router") \
                    else f"{device['device_name']}_{_date.today()}.conf"
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(conf_text.encode(), filename=_fname),
                    caption=caption,
                    parse_mode="HTML",
                )
                if qr_bytes:
                    await bot.send_photo(chat_id, BufferedInputFile(qr_bytes, filename="qr.png"))
                await db.update_config_version(device["id"], version)
                sent += 1
                await _asyncio.sleep(0.3)  # rate limit Telegram
            except Exception as exc:
                logger.warning(f"broadcast_configs: {device.get('device_name')}: {exc}")
                failed += 1
        await bot.send_message(
            str(cb.from_user.id),
            f"📤 <b>Рассылка завершена</b>\n✅ Отправлено: {sent}\n⏭ Без изменений: {len(devices)-sent-failed}\n❌ Ошибки: {failed}",
            parse_mode="HTML",
        )

    asyncio.create_task(_do_broadcast())
    await cb.message.answer(
        "📤 <b>Рассылка конфигов запущена</b>\n\nОбновлённые конфиги будут отправлены всем активным клиентам. Результат придёт отдельным сообщением.",
        reply_markup=back_to_admin_menu(),
        parse_mode="HTML",
    )

"""
handlers/client.py — Команды клиентов (самообслуживание)

Команды (из CLAUDE.md):
  /start — регистрация (FSM: invite → имя → протокол)
  /mydevices /myconfig /adddevice /removedevice
  /update /request /myrequests
  /exclude add|remove|list
  /route add|remove|list
  /report /status /help

FSM:
  - Таймаут 10 мин (middleware в bot.py обнуляет FSM при команде или таймауте)
  - Любая команда → сброс FSM → выполнить команду (StateFilter("*") на всех командах)
  - Invite-код резервируется на 10 мин при вводе, снимается при таймауте
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from datetime import date
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from handlers.keyboards import (
    client_connect_menu,
    client_devices_menu,
    client_excludes_menu,
    client_main_menu,
    client_request_type_kb,
    client_routes_hub_menu,
    device_excludes_inline_kb,
    device_excludes_menu,
    device_server_routes_inline_kb,
    device_server_routes_menu,
    client_server_routes_menu,
    client_sites_menu,
    client_support_menu,
    confirm_kb,
    device_detail_kb,
    devices_inline_kb,
    excludes_inline_kb,
    menu_reply_kb,
    platform_inline_kb,
    proto_inline_kb,
    server_routes_inline_kb,
)
from handlers.screen import edit_or_answer, result_text, return_kb, screen_text, section_text, start_prompt

from config import config
from database import Database
from services.config_builder import ConfigBuilder
from services.watchdog_client import WatchdogClient

if TYPE_CHECKING:
    from aiogram import Bot
    from services.autodist import AutoDist

logger = logging.getLogger(__name__)
router = Router()
_edit_or_answer = edit_or_answer

# Per-user lock to prevent race condition in adddevice (TOCTOU between count check and insert)
_adddevice_locks: dict[int, asyncio.Lock] = {}

_DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$')
_MOBILE_DNS_WARNING = (
    "Важно: оставьте только tunnel DNS из конфига.\n"
    "Отключите Private DNS / Secure DNS / DoH на устройстве, иначе YouTube и другие "
    "split-tunnel сервисы могут обходить dnsmasq.\n"
    "Если тоннель с таким именем уже есть в приложении, удалите старый и импортируйте этот конфиг заново."
)


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------
class RegFSM(StatesGroup):
    invite_code  = State()
    device_name  = State()
    protocol     = State()


class AddDeviceFSM(StatesGroup):
    device_name = State()
    protocol    = State()


class RemoveDeviceFSM(StatesGroup):
    confirm = State()


class RequestFSM(StatesGroup):
    domain = State()


class ExcludeFSM(StatesGroup):
    subnet = State()


class ServerRouteFSM(StatesGroup):
    target = State()


class ReportFSM(StatesGroup):
    text = State()


class CheckSiteFSM(StatesGroup):
    domain = State()


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
def _wc() -> WatchdogClient:
    return WatchdogClient(config.watchdog_url, config.watchdog_token)


async def _is_admin(message: Message, db: Database | None = None, **kw) -> bool:
    uid = str(message.from_user.id)
    if uid == str(config.admin_chat_id):
        return True
    if db is not None:
        return await db.is_admin(uid)
    return False


async def _get_client(message: Message, **kw) -> dict | None:
    db: Database = kw.get("db")
    return await db.get_client(str(message.from_user.id))


def _parse_protocol(text: str) -> str:
    t = text.lower()
    return "awg" if ("awg" in t or "amnezia" in t) else "wg"


def _normalize_policy_target(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("empty target")
    try:
        addr = ipaddress.ip_address(raw)
        return str(addr)
    except ValueError:
        net = ipaddress.ip_network(raw, strict=False)
        return str(net)


async def _device_policy_lists(db: Database, device_id: int) -> tuple[list[str], list[str]]:
    excludes_raw = await db.get_excludes(device_id)
    routes_raw = await db.get_server_routes(device_id)
    excludes = [item["subnet"] for item in excludes_raw]
    server_routes = [item["subnet"] for item in routes_raw]
    return excludes, server_routes


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    chat_id = str(message.from_user.id)

    client = await db.get_client(chat_id)
    if client:
        if client.get("is_disabled"):
            await message.answer("❌ Ваш аккаунт отключён. Обратитесь к администратору.")
            return
        devices = await db.get_devices(chat_id)
        name = message.from_user.first_name or "друг"
        await message.answer("📋 Меню", reply_markup=menu_reply_kb())
        await message.answer(
            f"👋 Добро пожаловать, *{name}*!\n\n"
            f"Устройств подключено: *{len(devices)}*\n\n"
            f"Выберите действие:",
            reply_markup=client_main_menu(),
        )
    else:
        await message.answer(
            "Добро пожаловать в VPN!\n\n"
            "Для регистрации введите *код приглашения*:"
        )
        await state.update_data(_fsm_ts=_now())
        await state.set_state(RegFSM.invite_code)


# ---------------------------------------------------------------------------
# Регистрация: invite
# ---------------------------------------------------------------------------
@router.message(RegFSM.invite_code)
async def reg_invite(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    code = message.text.strip()
    if not await db.reserve_invite_code(code, str(message.from_user.id)):
        await message.answer("❌ Код неверный, использован или истёк.\nПопробуйте снова:")
        return
    # Запоминаем флаг bootstrap для этапа завершения регистрации
    bootstrap = await db.get_invite_bootstrap_info(code)
    await state.update_data(invite_code=code, _fsm_ts=_now(),
                            is_bootstrap=bool(bootstrap))
    if bootstrap:
        await message.answer(
            "✅ Код принят!\n\n"
            "📌 Это *bootstrap-инвайт* — конфиги были созданы заранее.\n\n"
            "• Если вы *уже подключились* по одному из присланных конфигов — "
            "просто введите имя устройства. Конфиг останется рабочим.\n"
            "• Если вы *ещё не использовали* конфиги — тоже введите имя. "
            "Временные конфиги будут удалены и бот создаст новый.\n\n"
            "Введите *имя устройства* (iPhone, MacBook, PC…):"
        )
    else:
        await message.answer(
            "✅ Код принят!\n\nВведите *имя вашего устройства* (iPhone, MacBook, PC…):"
        )
    await state.set_state(RegFSM.device_name)


# ---------------------------------------------------------------------------
# Bootstrap-регистрация (reuse уже созданного AWG/WG пира)
# ---------------------------------------------------------------------------
async def _complete_bootstrap_registration(
    message: Message,
    state: FSMContext,
    kw: dict,
) -> None:
    """Завершить регистрацию по bootstrap-инвайту.

    Два случая:
    - last_handshake > 0 на AWG или WG: принимаем использованный bootstrap пир
      как постоянный, второй temp пир удаляем.
    - last_handshake == 0 на обоих: конфиги не использовались →
      удаляем оба temp пира, переходим к стандартному выбору протокола.
    """
    db: Database = kw.get("db")
    data        = await state.get_data()
    invite_code = data.get("invite_code", "")
    device_name = data.get("device_name", "")
    chat_id     = str(message.from_user.id)
    username    = message.from_user.username or ""
    first_name  = message.from_user.first_name or ""

    bootstrap = await db.get_invite_bootstrap_info(invite_code)
    if not bootstrap:
        # Fallback: пройти обычный путь
        await message.answer("Выберите *протокол*:", reply_markup=proto_inline_kb())
        await state.set_state(RegFSM.protocol)
        return

    awg_peer_id = bootstrap.get("awg_peer_id", "")
    wg_peer_id  = bootstrap.get("wg_peer_id", "")

    # Проверяем, подключался ли пользователь через bootstrap AWG или WG пир.
    selected_protocol = ""
    selected_peer_id = ""
    selected_privkey = ""
    selected_ip = ""
    selected_iface = ""
    stale_peer_id = ""
    stale_iface = ""
    try:
        wdc = WatchdogClient(config.watchdog_url, config.watchdog_token)
        peers_info = await wdc.get_peers()
        for p in (peers_info or {}).get("peers", []):
            if p.get("public_key") == awg_peer_id and p.get("last_handshake", 0) > 0:
                selected_protocol = "awg"
                selected_peer_id = awg_peer_id
                selected_privkey = bootstrap.get("awg_privkey", "")
                selected_ip = bootstrap.get("awg_ip") or None
                selected_iface = "wg0"
                stale_peer_id = wg_peer_id
                stale_iface = "wg1"
                break
            if p.get("public_key") == wg_peer_id and p.get("last_handshake", 0) > 0:
                selected_protocol = "wg"
                selected_peer_id = wg_peer_id
                selected_privkey = bootstrap.get("wg_privkey", "")
                selected_ip = bootstrap.get("wg_ip") or None
                selected_iface = "wg1"
                stale_peer_id = awg_peer_id
                stale_iface = "wg0"
                break
    except Exception:
        # Если watchdog недоступен — безопаснее сохранить основной bootstrap AWG пир.
        selected_protocol = "awg"
        selected_peer_id = awg_peer_id
        selected_privkey = bootstrap.get("awg_privkey", "")
        selected_ip = bootstrap.get("awg_ip") or None
        selected_iface = "wg0"
        stale_peer_id = wg_peer_id
        stale_iface = "wg1"

    if not selected_peer_id:
        # Пользователь не подключался через bootstrap конфиги.
        # Удаляем оба temp пира и переходим к стандартной регистрации.
        try:
            wdc2 = WatchdogClient(config.watchdog_url, config.watchdog_token)
            await wdc2.remove_peer(awg_peer_id, interface="wg0")
            await wdc2.remove_peer(wg_peer_id,  interface="wg1")
        except Exception:
            pass
        # Сбрасываем bootstrap флаг — дальше пройдёт обычная регистрация
        await state.update_data(is_bootstrap=False)
        await message.answer(
            "ℹ️ Временные конфиги не использовались и удалены.\n\n"
            "Выберите *протокол* для нового подключения:",
            reply_markup=proto_inline_kb(),
        )
        await state.set_state(RegFSM.protocol)
        return  # reg_protocol_cb сделает register_client() + add_device() + watchdog add_peer

    # Пользователь подключился через bootstrap конфиг — принимаем использованный пир.
    try:
        await db.register_client(chat_id, username, invite_code, first_name)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        await state.clear()
        return

    # Регистрируем устройство с предсозданными ключами и IP (пир уже на сервере).
    device = await db.add_device(
        chat_id, device_name, selected_protocol,
        public_key=selected_peer_id,
        private_key=selected_privkey,
        ip_address=selected_ip,
    )
    # Удаляем только неиспользованный temp пир.
    try:
        await WatchdogClient(config.watchdog_url, config.watchdog_token).remove_peer(
            stale_peer_id, interface=stale_iface
        )
    except Exception:
        pass

    await message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"Устройство: `{device_name}`\n"
        f"Протокол: `{selected_protocol.upper()}`\n\n"
        f"Ваш конфиг уже работает — ничего менять не нужно.\n"
        f"/myconfig — посмотреть или переслать конфиг ещё раз.",
        reply_markup=client_main_menu(),
    )
    await state.clear()

    # Bootstrap-reuse: не пересылаем конфиг повторно.
    # Вместо этого фиксируем текущую config_version, чтобы AutoDist не считал
    # устройство "необслуженным" и не отправлял тот же .conf ещё раз.
    if device:
        builder = ConfigBuilder()
        excludes, server_routes = await _device_policy_lists(db, device["id"])
        _, _, version = await builder.build(device, excludes, server_routes)
        await db.update_config_version(device["id"], version)


# ---------------------------------------------------------------------------
# Регистрация: имя устройства
# ---------------------------------------------------------------------------
@router.message(RegFSM.device_name)
async def reg_name(message: Message, state: FSMContext, **kw):
    name = message.text.strip()
    if not (2 <= len(name) <= 30):
        await message.answer("Имя должно быть от 2 до 30 символов:")
        return
    await state.update_data(device_name=name, _fsm_ts=_now())
    data = await state.get_data()
    if data.get("is_bootstrap"):
        # Bootstrap-инвайт: пиры уже созданы, всегда используем AWG.
        # Сразу завершаем регистрацию без выбора протокола.
        await _complete_bootstrap_registration(message, state, kw)
        return
    await message.answer("Выберите *протокол*:", reply_markup=proto_inline_kb())
    await state.set_state(RegFSM.protocol)


# ---------------------------------------------------------------------------
# Регистрация: протокол через инлайн-кнопку
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("proto:"), RegFSM.protocol)
async def reg_protocol_cb(cb: CallbackQuery, state: FSMContext, **kw):
    raw      = cb.data.split(":")[1]   # "awg", "wg" или "wg_router"
    is_router = raw == "wg_router"
    protocol  = "wg" if is_router else raw
    await cb.answer()
    # Используем общую логику завершения регистрации
    db: Database = kw.get("db")
    data        = await state.get_data()
    invite_code = data.get("invite_code", "")
    device_name = data.get("device_name", "")
    chat_id     = str(cb.from_user.id)
    username    = cb.from_user.username or ""
    first_name  = cb.from_user.first_name or ""

    try:
        await db.register_client(chat_id, username, invite_code, first_name)
    except ValueError as e:
        await cb.message.answer(f"❌ {e}")
        await state.clear()
        return

    builder = ConfigBuilder()
    device = await db.add_device(chat_id, device_name, protocol, pending=False, is_router=is_router)
    device = await builder.ensure_keys(device)
    await db.update_device_keys(device["id"], device["private_key"], device["public_key"])

    try:
        from services.watchdog_client import WatchdogClient
        await WatchdogClient(config.watchdog_url, config.watchdog_token).add_peer(
            device_name, protocol, device.get("public_key", ""), device.get("ip_address", "")
        )
    except Exception:
        pass

    await cb.message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await cb.message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"Устройство: `{device_name}`\n"
        f"Протокол: `{protocol.upper()}`\n\n"
        f"Конфиг отправляется...",
        reply_markup=client_main_menu(),
    )
    await state.clear()

    autodist: "AutoDist" = kw.get("autodist")
    if autodist:
        asyncio.create_task(autodist.send_to_device(chat_id, device, "Регистрация"))

    # Онбординг — инструкция по установке
    if protocol == "awg":
        await cb.message.answer(
            "📖 <b>Как установить:</b>\n"
            "1. Скачайте AmneziaWG: "
            "<a href='https://apps.apple.com/app/amneziawg/id6478942951'>iOS</a> | "
            "<a href='https://play.google.com/store/apps/details?id=org.amnezia.awg'>Android</a>\n"
            "2. Откройте приложение → \"+\" → \"Импортировать из QR-кода\" (или из файла)\n"
            "3. Включите переключатель — готово!\n\n"
            "❓ Если что-то не работает — нажмите \"🔍 Почему не работает сайт?\"",
            parse_mode="HTML",
        )
    else:
        await cb.message.answer(
            "📖 <b>Как установить:</b>\n"
            "1. Скачайте WireGuard: "
            "<a href='https://apps.apple.com/app/wireguard/id1441195209'>iOS</a> | "
            "<a href='https://play.google.com/store/apps/details?id=com.wireguard.android'>Android</a>\n"
            "2. Откройте приложение → \"+\" → \"Создать из QR-кода\" (или импортируйте файл)\n"
            "3. Включите переключатель — готово!",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Регистрация: протокол через текст (fallback)
# ---------------------------------------------------------------------------
@router.message(RegFSM.protocol)
async def reg_protocol(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    protocol    = _parse_protocol(message.text)
    data        = await state.get_data()
    invite_code = data.get("invite_code", "")
    device_name = data.get("device_name", "")
    chat_id     = str(message.from_user.id)
    username    = message.from_user.username or ""
    first_name  = message.from_user.first_name or ""

    try:
        await db.register_client(chat_id, username, invite_code, first_name)
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    # Добавляем первое устройство
    builder = ConfigBuilder()
    device = await db.add_device(chat_id, device_name, protocol, pending=False)
    device = await builder.ensure_keys(device)
    await db.update_device_keys(device["id"], device["private_key"], device["public_key"])

    # Добавляем WG пир
    try:
        await _wc().add_peer(device_name, protocol, device.get("public_key", ""), device.get("ip_address", ""))
    except Exception:
        pass

    await message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"Устройство: `{device_name}`\n"
        f"Протокол: `{protocol.upper()}`\n\n"
        f"Конфиг отправляется...",
        reply_markup=menu_reply_kb(),
    )
    await message.answer("Выберите действие:", reply_markup=client_main_menu())
    await state.clear()

    # Отправляем конфиг сразу
    autodist: "AutoDist" = kw.get("autodist")
    if autodist:
        asyncio.create_task(autodist.send_to_device(chat_id, device, "Регистрация"))

    # Онбординг — инструкция по установке
    if protocol == "awg":
        await message.answer(
            "📖 <b>Как установить:</b>\n"
            "1. Скачайте AmneziaWG: "
            "<a href='https://apps.apple.com/app/amneziawg/id6478942951'>iOS</a> | "
            "<a href='https://play.google.com/store/apps/details?id=org.amnezia.awg'>Android</a>\n"
            "2. Откройте приложение → \"+\" → \"Импортировать из QR-кода\" (или из файла)\n"
            "3. Включите переключатель — готово!\n\n"
            "❓ Если что-то не работает — нажмите \"🔍 Почему не работает сайт?\"",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "📖 <b>Как установить:</b>\n"
            "1. Скачайте WireGuard: "
            "<a href='https://apps.apple.com/app/wireguard/id1441195209'>iOS</a> | "
            "<a href='https://play.google.com/store/apps/details?id=com.wireguard.android'>Android</a>\n"
            "2. Откройте приложение → \"+\" → \"Создать из QR-кода\" (или импортируйте файл)\n"
            "3. Включите переключатель — готово!",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# /mydevices
# ---------------------------------------------------------------------------
@router.message(Command("mydevices"), StateFilter("*"))
async def cmd_mydevices(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return
    devices = await db.get_devices(str(message.from_user.id))
    if not devices:
        await message.answer("Устройств нет. /adddevice — добавить.")
        return
    lines = ["*Ваши устройства:*\n"]
    for d in devices:
        icon = "⏳" if d.get("pending_approval") else "✅"
        lines.append(
            f"{icon} `{d['device_name']}` ({d['protocol'].upper()}) — `{d.get('ip_address', 'N/A')}`"
        )
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /myconfig [имя]
# ---------------------------------------------------------------------------
@router.message(Command("myconfig"), StateFilter("*"))
async def cmd_myconfig(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database  = kw.get("db")
    client        = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split(maxsplit=1)
    devices = await db.get_devices(str(message.from_user.id))
    if not devices:
        await message.answer("Нет устройств. /adddevice")
        return

    if len(args) > 1:
        device = next((d for d in devices if d["device_name"] == args[1]), None)
        if not device:
            await message.answer(f"Устройство `{args[1]}` не найдено.")
            return
        if device.get("pending_approval"):
            await message.answer("⏳ Устройство ещё ожидает одобрения администратора.")
            return
        await _send_config(message, db, device, kw)
    elif len(devices) == 1:
        device = devices[0]
        if device.get("pending_approval"):
            await message.answer("⏳ Устройство ещё ожидает одобрения администратора.")
            return
        await _send_config(message, db, device, kw)
    else:
        await message.answer(
            "Выберите устройство для получения конфига:",
            reply_markup=devices_inline_kb(devices, "cfg:"),
        )


# ---------------------------------------------------------------------------
# /update — обновить конфиги всех устройств
# ---------------------------------------------------------------------------
@router.message(Command("update"), StateFilter("*"))
async def cmd_update(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    devices = await db.get_devices(str(message.from_user.id))
    active  = [d for d in devices if not d.get("pending_approval")]
    if not active:
        await message.answer("Нет активных устройств.")
        return

    builder = ConfigBuilder()
    updated = 0
    same = 0
    for device in active:
        try:
            excludes, server_routes = await _device_policy_lists(db, device["id"])
            conf_text, _, version = await builder.build(device, excludes, server_routes)
            if version == device.get("config_version"):
                same += 1
                continue
            await _send_config(message, db, device, kw)
            updated += 1
        except Exception as exc:
            logger.warning(f"cmd_update: {device.get('device_name')}: {exc}")
    if same > 0 and updated == 0:
        await message.answer("✅ Все конфиги актуальны (версия не изменилась).")
    elif same > 0:
        await message.answer(f"ℹ️ {same} устройств без изменений.")


# ---------------------------------------------------------------------------
# /adddevice
# ---------------------------------------------------------------------------
@router.message(Command("adddevice"), StateFilter("*"))
async def cmd_adddevice(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return
    if client.get("is_disabled"):
        await message.answer("❌ Ваш аккаунт отключён. Обратитесь к администратору.")
        return

    chat_id = str(message.from_user.id)
    if chat_id not in _adddevice_locks:
        _adddevice_locks[chat_id] = asyncio.Lock()
    async with _adddevice_locks[chat_id]:
        count = await db.count_devices(chat_id)
        limit = client.get("device_limit", config.device_limit_per_client)
        if count >= limit:
            await message.answer(
                f"Достигнут лимит устройств: {count}/{limit}.\n"
                f"Обратитесь к администратору."
            )
            return

    await message.answer("Введите *имя нового устройства*:")
    await state.update_data(_fsm_ts=_now())
    await state.set_state(AddDeviceFSM.device_name)


@router.message(AddDeviceFSM.device_name)
async def adddev_name(message: Message, state: FSMContext, **kw):
    name = message.text.strip()
    if not (2 <= len(name) <= 30):
        await message.answer("Имя от 2 до 30 символов:")
        return
    await state.update_data(device_name=name, _fsm_ts=_now())
    await message.answer("Выберите *протокол*:", reply_markup=proto_inline_kb())
    await state.set_state(AddDeviceFSM.protocol)


@router.callback_query(F.data.startswith("proto:"), AddDeviceFSM.protocol)
async def adddev_protocol_cb(cb: CallbackQuery, state: FSMContext, **kw):
    db: Database = kw.get("db")
    raw       = cb.data.split(":")[1]
    is_router = raw == "wg_router"
    protocol  = "wg" if is_router else raw
    data      = await state.get_data()
    return_to = data.get("_return_to", "cl:devices_menu")
    return_home = data.get("_return_home", "cl:menu")
    chat_id   = str(cb.from_user.id)
    await state.clear()
    await cb.answer()

    try:
        device = await db.add_device(chat_id, data["device_name"], protocol, pending=True, is_router=is_router)
        await cb.message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Конфиг придёт после одобрения.",
            reply_markup=return_kb(return_to, return_home),
        )
        bot: "Bot" = kw.get("bot")
        if bot and device.get("id"):
            dev_id = device["id"]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"dev_approve_{dev_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dev_reject_{dev_id}"),
            ]])
            asyncio.create_task(
                bot.send_message(
                    config.admin_chat_id,
                    f"📱 Новый запрос на устройство!\n"
                    f"Клиент: `{cb.from_user.username or cb.from_user.first_name or chat_id}`  Устройство: `{data['device_name']}`\n"
                    f"Протокол: `{protocol.upper()}`",
                    reply_markup=kb,
                )
            )
    except Exception as e:
        await cb.message.answer(f"❌ {e}", reply_markup=return_kb(return_to, return_home))


@router.message(AddDeviceFSM.protocol)
async def adddev_protocol(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    protocol = _parse_protocol(message.text)
    data     = await state.get_data()
    return_to = data.get("_return_to", "cl:devices_menu")
    return_home = data.get("_return_home", "cl:menu")
    chat_id  = str(message.from_user.id)
    await state.clear()

    try:
        device = await db.add_device(chat_id, data["device_name"], protocol, pending=True)
        await message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Конфиг придёт после одобрения.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer("Выберите действие:", reply_markup=return_kb(return_to, return_home))
        bot: "Bot" = kw.get("bot")
        if bot and device.get("id"):
            dev_id = device["id"]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"dev_approve_{dev_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dev_reject_{dev_id}"),
            ]])
            asyncio.create_task(
                bot.send_message(
                    config.admin_chat_id,
                    f"📱 Новый запрос на устройство!\n"
                    f"Клиент: `{message.from_user.username or message.from_user.first_name or chat_id}`  Устройство: `{data['device_name']}`\n"
                    f"Протокол: `{protocol.upper()}`",
                    reply_markup=kb,
                )
            )
    except Exception as e:
        await message.answer(f"❌ {e}", reply_markup=ReplyKeyboardRemove())


# ---------------------------------------------------------------------------
# /removedevice [имя]
# ---------------------------------------------------------------------------
@router.message(Command("removedevice"), StateFilter("*"))
async def cmd_removedevice(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args    = message.text.split(maxsplit=1)
    devices = await db.get_devices(str(message.from_user.id))
    if not devices:
        await message.answer("Нет устройств.")
        return

    if len(args) > 1:
        device = next((d for d in devices if d["device_name"] == args[1]), None)
        if not device:
            await message.answer(f"Устройство `{args[1]}` не найдено.")
            return
        await _do_remove_device(message, db, device)
    elif len(devices) == 1:
        await _do_remove_device(message, db, devices[0])
    else:
        await message.answer(
            "Выберите устройство для удаления:",
            reply_markup=devices_inline_kb(devices, "rm:"),
        )


# ---------------------------------------------------------------------------
# /request vpn|direct <домен>
# ---------------------------------------------------------------------------
@router.message(Command("request"), StateFilter("*"))
async def cmd_request(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split()
    if len(args) < 3 or args[1] not in ("vpn", "direct"):
        await message.answer(
            "Использование: `/request vpn|direct <домен>`\n"
            "Пример: `/request vpn example.com`"
        )
        return

    direction = args[1]
    domain    = args[2].lower().strip(".")
    if not _DOMAIN_RE.match(domain) or len(domain) > 253:
        await message.answer("❌ Неверный формат домена. Пример: `example.com`")
        return
    req_id    = await db.create_domain_request(str(message.from_user.id), domain, direction)
    await message.answer(
        f"✅ Запрос #{req_id} отправлен.\n"
        f"Домен: `{domain}` → {direction}"
    )
    bot: "Bot" = kw.get("bot")
    if bot:
        asyncio.create_task(
            bot.send_message(
                config.admin_chat_id,
                f"{'🔒' if direction == 'vpn' else '🌐'} Запрос #{req_id} на `{domain}` ({direction})\n"
                f"От: `{message.from_user.id}`\n/requests — для модерации",
            )
        )


# ---------------------------------------------------------------------------
# /myrequests
# ---------------------------------------------------------------------------
@router.message(Command("myrequests"), StateFilter("*"))
async def cmd_myrequests(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    reqs = await db.get_requests_by_client(str(message.from_user.id))
    if not reqs:
        await message.answer("У вас нет запросов.")
        return
    icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    lines = ["*Ваши запросы:*\n"]
    for r in reqs[:15]:
        icon = icons.get(r["status"], "?")
        lines.append(
            f"{icon} `{r['domain']}` ({r['direction']}) — {r['status']}\n"
            f"   {r['created_at'][:10]}"
        )
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /exclude add|remove|list <подсеть>
# ---------------------------------------------------------------------------
@router.message(Command("exclude"), StateFilter("*"))
async def cmd_exclude(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split()
    if len(args) < 2 or args[1] not in ("add", "remove", "list"):
        await message.answer(
            "Использование:\n"
            "`/exclude add <подсеть>` — исключить из VPN\n"
            "`/exclude remove <подсеть>` — вернуть в VPN\n"
            "`/exclude list` — список исключений"
        )
        return

    action = args[1]
    chat_id = str(message.from_user.id)

    if action == "list":
        devices = await db.get_devices(chat_id)
        if not devices:
            await message.answer("Нет устройств.")
            return
        lines = []
        for d in devices:
            exs = await db.get_excludes(d["id"])
            if exs:
                lines.append(f"*{d['device_name']}:*")
                lines.extend(f"  • `{e['subnet']}`" for e in exs)
        await message.answer("\n".join(lines) if lines else "Исключений нет.")
        return

    if len(args) < 3:
        await message.answer("Укажите подсеть, например: `192.168.1.0/24`")
        return

    try:
        subnet = _normalize_policy_target(args[2])
    except ValueError:
        await message.answer(f"Неверный формат подсети/адреса: `{args[2]}`")
        return

    # Берём первое устройство (или по имени если передано)
    devices = await db.get_devices(chat_id)
    device_name = args[3] if len(args) > 3 else None
    device = (
        next((d for d in devices if d["device_name"] == device_name), None)
        if device_name
        else (devices[0] if devices else None)
    )
    if not device:
        await message.answer("Устройство не найдено.")
        return

    if action == "add":
        await db.remove_server_route(device["id"], subnet)
        await db.add_exclude(device["id"], subnet)
        await message.answer(f"✅ `{subnet}` исключён из VPN для `{device['device_name']}`")
    else:
        await db.remove_exclude(device["id"], subnet)
        await message.answer(f"✅ `{subnet}` возвращён в VPN для `{device['device_name']}`")


# ---------------------------------------------------------------------------
# /route add|remove|list <ip|подсеть> [устройство]
# ---------------------------------------------------------------------------
@router.message(Command("route"), StateFilter("*"))
async def cmd_route(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await _get_client(message, **kw)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split()
    if len(args) < 2 or args[1] not in ("add", "remove", "list"):
        await message.answer(
            "Использование:\n"
            "`/route add <ip|подсеть>` — вести через сервер\n"
            "`/route remove <ip|подсеть>` — убрать из списка\n"
            "`/route list` — список маршрутов через сервер"
        )
        return

    action = args[1]
    chat_id = str(message.from_user.id)

    if action == "list":
        devices = await db.get_devices(chat_id)
        if not devices:
            await message.answer("Нет устройств.")
            return
        lines = []
        for d in devices:
            routes = await db.get_server_routes(d["id"])
            if routes:
                lines.append(f"*{d['device_name']}:*")
                lines.extend(f"  • `{item['subnet']}`" for item in routes)
        await message.answer("\n".join(lines) if lines else "Маршрутов через сервер нет.")
        return

    if len(args) < 3:
        await message.answer("Укажите IP или подсеть, например: `192.168.1.200` или `192.168.1.0/24`")
        return

    try:
        target = _normalize_policy_target(args[2])
    except ValueError:
        await message.answer(f"Неверный формат IP/подсети: `{args[2]}`")
        return

    devices = await db.get_devices(chat_id)
    device_name = args[3] if len(args) > 3 else None
    device = (
        next((d for d in devices if d["device_name"] == device_name), None)
        if device_name
        else (devices[0] if devices else None)
    )
    if not device:
        await message.answer("Устройство не найдено.")
        return

    if action == "add":
        await db.remove_exclude(device["id"], target)
        await db.add_server_route(device["id"], target)
        await message.answer(f"✅ `{target}` пойдёт через сервер для `{device['device_name']}`")
    else:
        await db.remove_server_route(device["id"], target)
        await message.answer(f"✅ `{target}` убран из маршрутов через сервер для `{device['device_name']}`")


# ---------------------------------------------------------------------------
# /report <описание>
# ---------------------------------------------------------------------------
@router.message(Command("report"), StateFilter("*"))
async def cmd_report(message: Message, state: FSMContext, **kw):
    await state.clear()
    client = await _get_client(message, **kw)
    if not client:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/report <описание проблемы>`")
        return

    text = args[1]
    bot: "Bot" = kw.get("bot")
    if bot:
        asyncio.create_task(
            bot.send_message(
                config.admin_chat_id,
                f"📝 *Жалоба от клиента*\n"
                f"ID: `{message.from_user.id}`\n"
                f"Username: @{message.from_user.username or 'N/A'}\n\n"
                f"{text}",
            )
        )
    await message.answer("✅ Сообщение отправлено администратору.")


# ---------------------------------------------------------------------------
# /status (клиентский)
# ---------------------------------------------------------------------------
@router.message(Command("status"), StateFilter("*"))
async def cmd_status_client(message: Message, state: FSMContext, **kw):
    if await _is_admin(message, db=kw.get("db")):
        return   # admin.py обработает
    await state.clear()
    client = await _get_client(message, **kw)
    if not client:
        return
    try:
        s = await _wc().get_status()
        ok    = s.get("status") == "ok"
        stack = s.get("active_stack", "N/A")
        await message.answer(
            f"{'✅ VPN работает' if ok else '⚠️ VPN деградирован'}\n"
            f"Протокол: `{stack}`"
        )
    except Exception:
        await message.answer("❌ Не удалось получить статус")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
@router.message(Command("help"), StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext, **kw):
    if await _is_admin(message, db=kw.get("db")):
        return
    await state.clear()
    client = await _get_client(message, **kw)
    if not client:
        await message.answer(
            "Для использования бота необходима регистрация.\n"
            "Запросите код приглашения у администратора → /start"
        )
        return
    await message.answer(
        "*Доступные команды:*\n\n"
        "/start — главная\n"
        "/mydevices — список устройств\n"
        "/myconfig [имя] — получить конфиг\n"
        "/update — обновить конфиги\n"
        "/adddevice — добавить устройство\n"
        "/removedevice [имя] — удалить устройство\n"
        "/request vpn|direct <домен> — запросить маршрут\n"
        "/myrequests — мои запросы\n"
        "/exclude add|remove|list <подсеть> — исключения\n"
        "/route add|remove|list <ip|подсеть> — через сервер\n"
        "/report <текст> — сообщить о проблеме\n"
        "/status — статус VPN\n"
        "/help — эта справка\n"
        "/menu — кнопочное меню",
        reply_markup=client_main_menu(),
    )


# ---------------------------------------------------------------------------
# Кнопка «📋 Меню» (постоянная ReplyKeyboard)
# ---------------------------------------------------------------------------
@router.message(F.text == "📋 Меню", StateFilter("*"))
async def btn_menu(message: Message, state: FSMContext, **kw):
    await state.clear()
    db: Database = kw.get("db")
    client = await db.get_client(str(message.from_user.id))
    if not client:
        return
    if await _is_admin(message, db=kw.get("db")):
        from handlers.keyboards import admin_main_menu
        await message.answer("<b>Меню администратора</b>", reply_markup=admin_main_menu(), parse_mode="HTML")
    else:
        await message.answer("<b>Меню</b>", reply_markup=client_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /menu для клиентов
# ---------------------------------------------------------------------------
@router.message(Command("menu"), StateFilter("*"))
async def cmd_menu_client(message: Message, state: FSMContext, **kw):
    if await _is_admin(message, db=kw.get("db")):
        return  # admin.py обработает
    await state.clear()
    client = await _get_client(message, **kw)
    if not client:
        return
    await message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await message.answer("*Меню*", reply_markup=client_main_menu())


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _menu_header() -> str:
    """Однострочный статус VPN для шапки клиентского меню."""
    try:
        s = await _wc().get_status()
        ok = s.get("status") == "ok"
        stack = s.get("active_stack", "—")
        icon = "✅" if ok else "⚠️"
        label = "работает" if ok else "деградирован"
        return f"{icon} <b>VPN {label}</b> · <code>{stack}</code>"
    except Exception:
        return "📋 <b>Меню</b>"


# Callback «cl:menu» — вернуться в главное меню клиента
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "cl:menu")
async def cb_cl_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    header = await _menu_header()
    await _edit_or_answer(
        cb,
        f"{header}\n\n<blockquote>Меню</blockquote>\n\n<i>Выберите, что хотите сделать: управлять устройствами, обновить подключение, настроить маршруты или обратиться за помощью.</i>",
        client_main_menu(),
    )


@router.callback_query(F.data == "cl:devices_menu")
async def cb_cl_devices_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        screen_text(
            "Устройства",
            "Здесь можно посмотреть свои устройства, добавить новое, удалить старое и получить конфиг.",
            icon="📱",
            details=["открыть карточку устройства", "получить конфиг", "добавить или удалить устройство"],
            trail=["Меню", "Устройства"],
        ),
        client_devices_menu(),
    )


@router.callback_query(F.data == "cl:connect_menu")
async def cb_cl_connect_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        screen_text(
            "Подключение",
            "Проверьте, работает ли VPN, и при необходимости обновите конфиги.",
            icon="🔌",
            details=["посмотреть состояние VPN", "обновить все конфиги", "заново получить конфиг"],
            trail=["Меню", "Подключение"],
        ),
        client_connect_menu(),
    )


@router.callback_query(F.data == "cl:routes_menu")
async def cb_cl_routes_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        screen_text(
            "Маршруты",
            "Здесь настраивается, что идёт через VPN, что обходится напрямую и что направляется через сервер.",
            icon="🌐",
            details=["запросить сайт", "добавить исключение", "направить адрес через сервер"],
            trail=["Меню", "Маршруты"],
        ),
        client_routes_hub_menu(),
    )


@router.callback_query(F.data == "cl:support_menu")
async def cb_cl_support_menu(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        screen_text(
            "Поддержка",
            "Если что-то не работает, отсюда можно проверить ситуацию и написать администратору.",
            icon="🆘",
            details=["посмотреть помощь", "открыть свои запросы", "сообщить о проблеме"],
            trail=["Меню", "Поддержка"],
        ),
        client_support_menu(),
    )


# ---------------------------------------------------------------------------
# Callback: «cl:removedevice» — удалить устройство через меню
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "cl:removedevice")
async def cb_cl_removedevice(cb: CallbackQuery, **kw):
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    if not devices:
        await cb.answer("Нет устройств")
        await _edit_or_answer(cb, "Нет устройств.", client_devices_menu())
        return
    if len(devices) == 1:
        await cb.answer()
        await _do_remove_device(cb.message, db, devices[0])
    else:
        await cb.answer()
        await _edit_or_answer(
            cb,
            "Выберите устройство для удаления:",
            devices_inline_kb(devices, "rm:", "cl:devices_menu"),
        )


# ---------------------------------------------------------------------------
# Запрос маршрута через меню (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:sites")
async def cb_cl_sites(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        "🌐 <b>Сайты через VPN</b>\n\nЗапросить добавление сайта или посмотреть статус запросов.",
        client_sites_menu(),
    )


@router.callback_query(F.data == "cl:request")
async def cb_cl_request(cb: CallbackQuery, **kw):
    await cb.answer()
    await edit_or_answer(cb, "Куда направить домен?", client_request_type_kb())


@router.callback_query(F.data.startswith("cl:req:"))
async def cb_cl_request_type(cb: CallbackQuery, state: FSMContext, **kw):
    direction = cb.data[len("cl:req:"):]
    label = "VPN" if direction == "vpn" else "Direct"
    await start_prompt(
        cb,
        state,
        RequestFSM.domain,
        f"Введите домен для маршрута <b>{label}</b>\n(например: <code>example.com</code>):",
        "cl:sites",
        home_cb="cl:menu",
        extra_data={"_req_direction": direction},
    )


@router.message(RequestFSM.domain)
async def fsm_request_domain(message: Message, state: FSMContext, **kw):
    data = await state.get_data()
    direction = data.get("_req_direction", "vpn")
    return_to = data.get("_return_to", "cl:sites")
    return_home = data.get("_return_home", "cl:menu")
    domain = message.text.strip().lower().strip(".")
    if not _DOMAIN_RE.match(domain) or len(domain) > 253:
        await message.answer("❌ Неверный формат домена. Пример: `example.com`")
        return
    await state.clear()
    db: Database = kw.get("db")
    bot = kw.get("bot")
    req_id = await db.create_domain_request(str(message.from_user.id), domain, direction)
    await message.answer(
        f"✅ Запрос #{req_id} отправлен.\nДомен: `{domain}` → {direction}",
        reply_markup=return_kb(return_to, return_home),
    )
    if bot:
        asyncio.create_task(
            bot.send_message(
                config.admin_chat_id,
                f"{'🔒' if direction == 'vpn' else '🌐'} Запрос #{req_id} на `{domain}` ({direction})\n"
                f"От: `{message.from_user.id}`\n/requests — для модерации",
            )
        )


# ---------------------------------------------------------------------------
# Исключения через меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:excludes")
async def cb_cl_excludes(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(cb, "🚫 <b>Исключения из VPN</b>", client_excludes_menu())


@router.callback_query(F.data == "cl:ex_list")
async def cb_cl_ex_list(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    lines = []
    for d in devices:
        exs = await db.get_excludes(d["id"])
        if exs:
            lines.append(f"<b>{d['device_name']}</b>")
            lines.extend(f"• <code>{e['subnet']}</code>" for e in exs)
            lines.append("")
    text = "\n".join(lines).strip() if lines else "Исключений нет."
    await _edit_or_answer(cb, text, client_excludes_menu())


@router.callback_query(F.data == "cl:ex_add")
async def cb_cl_ex_add(cb: CallbackQuery, state: FSMContext, **kw):
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    if not devices:
        await cb.message.answer("Нет устройств.", reply_markup=client_devices_menu())
        return
    await start_prompt(
        cb,
        state,
        ExcludeFSM.subnet,
        "Введите подсеть для исключения\n(например: <code>192.168.1.0/24</code>):",
        "cl:excludes",
        home_cb="cl:menu",
        extra_data={"_ex_device_id": devices[0]["id"]},
    )


@router.message(ExcludeFSM.subnet)
async def fsm_exclude_subnet(message: Message, state: FSMContext, **kw):
    import ipaddress
    try:
        subnet = _normalize_policy_target(message.text)
    except ValueError:
        await message.answer(f"❌ Неверный формат: `{message.text.strip()}`\nПримеры: `192.168.1.202`, `192.168.1.0/24`")
        return
    data = await state.get_data()
    device_id = data.get("_ex_device_id")
    back_to_device = data.get("_ex_back_device_id")
    return_to = data.get("_return_to", "cl:excludes")
    return_home = data.get("_return_home", "cl:menu")
    await state.clear()
    db: Database = kw.get("db")
    await db.remove_server_route(device_id, subnet)
    await db.add_exclude(device_id, subnet)
    if back_to_device:
        await message.answer(
            f"✅ `{subnet}` добавлен в исключения.",
            reply_markup=device_excludes_menu(int(back_to_device)),
        )
    else:
        await message.answer(f"✅ `{subnet}` добавлен в исключения.", reply_markup=return_kb(return_to, return_home))


@router.callback_query(F.data == "cl:ex_remove")
async def cb_cl_ex_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    # Показываем исключения первого устройства с исключениями
    for d in devices:
        exs = await db.get_excludes(d["id"])
        if exs:
            await _edit_or_answer(
                cb,
                f"🚫 <b>Исключения устройства</b>\n<code>{d['device_name']}</code>",
                reply_markup=excludes_inline_kb(exs, d["id"]),
            )
            return
    await _edit_or_answer(cb, "Исключений нет.", client_excludes_menu())


@router.callback_query(F.data.startswith("cl:ex_del:"))
async def cb_cl_ex_del(cb: CallbackQuery, **kw):
    parts = cb.data[len("cl:ex_del:"):].split(":", 1)
    device_id = int(parts[0])
    subnet = parts[1]
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device or str(device.get("chat_id", "")) != str(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await db.remove_exclude(device_id, subnet)
    await cb.answer(f"Удалено: {subnet}")
    await cb.message.edit_text(f"✅ `{subnet}` удалён из исключений.")


@router.callback_query(F.data.startswith("cl:devex:"))
async def cb_cl_device_excludes(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devex:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    await _edit_or_answer(
        cb,
        f"🚫 <b>Исключения из VPN</b>\nУстройство: <code>{device['device_name']}</code>",
        device_excludes_menu(device_id),
    )


@router.callback_query(F.data.startswith("cl:devex_list:"))
async def cb_cl_devex_list(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devex_list:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    excludes = await db.get_excludes(device_id)
    text = (
        f"<b>{device['device_name']}</b>\n" + "\n".join(f"• <code>{item['subnet']}</code>" for item in excludes)
        if excludes else
        f"Для <code>{device['device_name']}</code> исключений нет."
    )
    await _edit_or_answer(cb, text, device_excludes_menu(device_id))


@router.callback_query(F.data.startswith("cl:devex_add:"))
async def cb_cl_devex_add(cb: CallbackQuery, state: FSMContext, **kw):
    device_id = int(cb.data[len("cl:devex_add:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.", reply_markup=client_devices_menu())
        return
    await start_prompt(
        cb,
        state,
        ExcludeFSM.subnet,
        f"Устройство: <code>{device['device_name']}</code>\n"
        "Введите подсеть для исключения\n"
        "(например: <code>192.168.1.0/24</code>):",
        f"cl:devex:{device_id}",
        home_cb="cl:devices_menu",
        extra_data={"_ex_device_id": device_id, "_ex_back_device_id": device_id},
    )


@router.callback_query(F.data.startswith("cl:devex_remove:"))
async def cb_cl_devex_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devex_remove:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    excludes = await db.get_excludes(device_id)
    if not excludes:
        await _edit_or_answer(cb, f"Для <code>{device['device_name']}</code> исключений нет.", device_excludes_menu(device_id))
        return
    await _edit_or_answer(cb, f"🚫 <b>Исключения устройства</b>\n<code>{device['device_name']}</code>", device_excludes_inline_kb(excludes, device_id))


@router.callback_query(F.data.startswith("cl:devex_del:"))
async def cb_cl_devex_del(cb: CallbackQuery, **kw):
    parts = cb.data[len("cl:devex_del:"):].split(":", 1)
    device_id = int(parts[0])
    subnet = parts[1]
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.answer("❌ Устройство не найдено", show_alert=True)
        return
    await db.remove_exclude(device_id, subnet)
    await cb.answer(f"Удалено: {subnet}")
    await cb.message.edit_text(
        f"✅ `{subnet}` удалён из исключений.",
        reply_markup=device_excludes_menu(device_id),
    )


# ---------------------------------------------------------------------------
# Маршруты через сервер
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:sroutes")
async def cb_cl_server_routes(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(cb, "📍 <b>Маршруты через сервер</b>", client_server_routes_menu())


@router.callback_query(F.data.startswith("cl:devsr:"))
async def cb_cl_device_server_routes(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devsr:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    await _edit_or_answer(
        cb,
        f"📍 <b>Маршруты через сервер</b>\nУстройство: <code>{device['device_name']}</code>",
        device_server_routes_menu(device_id),
    )


@router.callback_query(F.data == "cl:sr_list")
async def cb_cl_sr_list(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    lines = []
    for d in devices:
        routes = await db.get_server_routes(d["id"])
        if routes:
            lines.append(f"<b>{d['device_name']}</b>")
            lines.extend(f"• <code>{item['subnet']}</code>" for item in routes)
            lines.append("")
    text = "\n".join(lines).strip() if lines else "Маршрутов через сервер нет."
    await _edit_or_answer(cb, text, client_server_routes_menu())


@router.callback_query(F.data == "cl:sr_add")
async def cb_cl_sr_add(cb: CallbackQuery, state: FSMContext, **kw):
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    if not devices:
        await cb.message.answer("Нет устройств.", reply_markup=client_devices_menu())
        return
    if len(devices) == 1:
        await start_prompt(
            cb,
            state,
            ServerRouteFSM.target,
            "Введите IP или подсеть для маршрута через сервер\n"
            "(например: <code>192.168.1.200</code> или <code>192.168.1.0/24</code>):",
            "cl:sroutes",
            home_cb="cl:menu",
            extra_data={"_sr_device_id": devices[0]["id"]},
        )
        return
    await edit_or_answer(
        cb,
        "Выберите устройство для добавления маршрута через сервер:",
        devices_inline_kb(devices, "cl:sr_add_dev:", "cl:sroutes"),
    )


@router.callback_query(F.data.startswith("cl:sr_add_dev:"))
async def cb_cl_sr_add_dev(cb: CallbackQuery, state: FSMContext, **kw):
    device_id = int(cb.data[len("cl:sr_add_dev:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.", reply_markup=client_server_routes_menu())
        return
    await start_prompt(
        cb,
        state,
        ServerRouteFSM.target,
        f"Устройство: <code>{device['device_name']}</code>\n"
        "Введите IP или подсеть для маршрута через сервер\n"
        "(например: <code>192.168.1.200</code> или <code>192.168.1.0/24</code>):",
        "cl:sroutes",
        home_cb="cl:menu",
        extra_data={"_sr_device_id": device_id},
    )


@router.message(ServerRouteFSM.target)
async def fsm_server_route_target(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    try:
        target = _normalize_policy_target(message.text)
    except ValueError:
        await message.answer("❌ Неверный формат.\nПримеры: `192.168.1.200`, `192.168.1.0/24`")
        return
    data = await state.get_data()
    device_id = data.get("_sr_device_id")
    back_to_device = data.get("_sr_back_device_id")
    return_to = data.get("_return_to", "cl:sroutes")
    return_home = data.get("_return_home", "cl:menu")
    await state.clear()
    if not device_id:
        await message.answer("❌ Не выбрано устройство.", reply_markup=client_server_routes_menu())
        return
    await db.remove_exclude(device_id, target)
    await db.add_server_route(device_id, target)
    if back_to_device:
        await message.answer(
            f"✅ `{target}` добавлен в маршруты через сервер.",
            reply_markup=device_server_routes_menu(int(back_to_device)),
        )
    else:
        await message.answer(f"✅ `{target}` добавлен в маршруты через сервер.", reply_markup=return_kb(return_to, return_home))


@router.callback_query(F.data == "cl:sr_remove")
async def cb_cl_sr_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    for d in devices:
        routes = await db.get_server_routes(d["id"])
        if routes:
            await _edit_or_answer(
                cb,
                f"📍 <b>Маршруты через сервер</b>\n<code>{d['device_name']}</code>",
                reply_markup=server_routes_inline_kb(routes, d["id"]),
            )
            return
    await _edit_or_answer(cb, "Маршрутов через сервер нет.", client_server_routes_menu())


@router.callback_query(F.data.startswith("cl:sr_del:"))
async def cb_cl_sr_del(cb: CallbackQuery, **kw):
    parts = cb.data[len("cl:sr_del:"):].split(":", 1)
    device_id = int(parts[0])
    subnet = parts[1]
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.answer("❌ Устройство не найдено", show_alert=True)
        return
    await db.remove_server_route(device_id, subnet)
    await cb.answer(f"Удалено: {subnet}")
    await cb.message.edit_text(f"✅ `{subnet}` удалён из маршрутов через сервер.")


@router.callback_query(F.data.startswith("cl:devsr_list:"))
async def cb_cl_devsr_list(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devsr_list:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    routes = await db.get_server_routes(device_id)
    text = (
        f"<b>{device['device_name']}</b>\n" + "\n".join(f"• <code>{item['subnet']}</code>" for item in routes)
        if routes else
        f"Для <code>{device['device_name']}</code> маршрутов через сервер нет."
    )
    await _edit_or_answer(cb, text, device_server_routes_menu(device_id))


@router.callback_query(F.data.startswith("cl:devsr_add:"))
async def cb_cl_devsr_add(cb: CallbackQuery, state: FSMContext, **kw):
    device_id = int(cb.data[len("cl:devsr_add:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.", reply_markup=client_devices_menu())
        return
    await start_prompt(
        cb,
        state,
        ServerRouteFSM.target,
        f"Устройство: <code>{device['device_name']}</code>\n"
        "Введите IP или подсеть для маршрута через сервер\n"
        "(например: <code>192.168.1.200</code> или <code>192.168.1.0/24</code>):",
        f"cl:devsr:{device_id}",
        home_cb="cl:devices_menu",
        extra_data={"_sr_device_id": device_id, "_sr_back_device_id": device_id},
    )


@router.callback_query(F.data.startswith("cl:devsr_remove:"))
async def cb_cl_devsr_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:devsr_remove:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    routes = await db.get_server_routes(device_id)
    if not routes:
        await _edit_or_answer(
            cb,
            f"Для <code>{device['device_name']}</code> маршрутов через сервер нет.",
            device_server_routes_menu(device_id),
        )
        return
    await _edit_or_answer(
        cb,
        f"📍 <b>Маршруты через сервер</b>\n<code>{device['device_name']}</code>",
        device_server_routes_inline_kb(routes, device_id),
    )


@router.callback_query(F.data.startswith("cl:devsr_del:"))
async def cb_cl_devsr_del(cb: CallbackQuery, **kw):
    parts = cb.data[len("cl:devsr_del:"):].split(":", 1)
    device_id = int(parts[0])
    subnet = parts[1]
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.answer("❌ Устройство не найдено", show_alert=True)
        return
    await db.remove_server_route(device_id, subnet)
    await cb.answer(f"Удалено: {subnet}")
    await cb.message.edit_text(
        f"✅ `{subnet}` удалён из маршрутов через сервер.",
        reply_markup=device_server_routes_menu(device_id),
    )


# ---------------------------------------------------------------------------
# Сообщить о проблеме через меню (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:report")
async def cb_cl_report(cb: CallbackQuery, state: FSMContext, **kw):
    await start_prompt(
        cb,
        state,
        ReportFSM.text,
        "Опишите проблему:",
        "cl:support_menu",
        home_cb="cl:menu",
    )


@router.message(ReportFSM.text)
async def fsm_report_text(message: Message, state: FSMContext, **kw):
    text = message.text.strip()
    data = await state.get_data()
    return_to = data.get("_return_to", "cl:support_menu")
    return_home = data.get("_return_home", "cl:menu")
    await state.clear()
    bot = kw.get("bot")
    if bot:
        asyncio.create_task(
            bot.send_message(
                config.admin_chat_id,
                f"📝 *Жалоба от клиента*\n"
                f"ID: `{message.from_user.id}`\n"
                f"Username: @{message.from_user.username or 'N/A'}\n\n"
                f"{text}",
            )
        )
    await message.answer("✅ Сообщение отправлено администратору.", reply_markup=return_kb(return_to, return_home))


# ---------------------------------------------------------------------------
# Проверка сайта: почему не работает?
# ---------------------------------------------------------------------------

_MANUAL_VPN    = "/etc/vpn-routes/manual-vpn.txt"
_MANUAL_DIRECT = "/etc/vpn-routes/manual-direct.txt"


@router.callback_query(F.data == "cl:checksite")
async def cb_cl_checksite(cb: CallbackQuery, state: FSMContext, **kw):
    db: Database = kw.get("db")
    client = await db.get_client(str(cb.from_user.id))
    if not client:
        await cb.message.answer("Сначала зарегистрируйтесь: /start")
        return
    await start_prompt(
        cb,
        state,
        CheckSiteFSM.domain,
        "Введите адрес сайта (например: <code>youtube.com</code>):",
        "cl:support_menu",
        home_cb="cl:menu",
    )


@router.message(CheckSiteFSM.domain)
async def fsm_checksite_domain(message: Message, state: FSMContext, **kw):
    import os as _os
    domain = message.text.strip().lower().strip(".").replace("https://", "").replace("http://", "").split("/")[0]
    data = await state.get_data()
    return_to = data.get("_return_to", "cl:support_menu")
    return_home = data.get("_return_home", "cl:menu")
    await state.clear()

    try:
        r = await _wc().check_domain(domain)
        verdict   = r.get("verdict", "unknown")
        ips       = r.get("ips", [])
        in_static = r.get("in_blocked_static", False)
        in_dyn    = r.get("in_blocked_dynamic", False)
        in_manual = r.get("in_manual_vpn", False)
        service   = str(r.get("latency_service") or "")
        ip_str    = ", ".join(ips[:3]) + ("…" if len(ips) > 3 else "") if ips else "не резолвится"

        if verdict == "vpn":
            source = []
            if in_manual:  source.append("ручной список")
            if in_static:  source.append("база РКН")
            if in_dyn:     source.append("DNS-кэш")
            src_str = " + ".join(source) if source else ""
            text = (
                f"✅ <b>{domain}</b> — идёт через VPN\n"
                f"IP: <code>{ip_str}</code>\n"
                + (f"Источник: {src_str}\n" if src_str else "") +
                "\nЕсли сайт не открывается:\n"
                "• Переподключите VPN\n"
                "• Нажмите «🔄 Обновить настройки VPN»\n"
                "• Если не помогло — «🆘 Сообщить о проблеме»"
            )
        elif verdict == "direct":
            text = (
                f"🌐 <b>{domain}</b> — идёт напрямую (без VPN)\n"
                f"IP: <code>{ip_str}</code>\n\n"
                "Этот сайт настроен на прямое соединение. "
                "Чтобы пустить через VPN — нажмите «🌐 Открыть сайт через VPN»."
            )
        elif verdict == "latency_sensitive_direct":
            text = (
                f"⚡ <b>{domain}</b> — direct-first маршрут\n"
                f"IP: <code>{ip_str}</code>\n"
                + (f"Сервис: <b>{service}</b>\n" if service else "") +
                "\nСайт и его bootstrap-зависимости специально оставлены прямыми, "
                "чтобы не ломались вход, геопривязка и загрузка приложения."
            )
        else:
            text = (
                f"❓ <b>{domain}</b> — не в списках блокировок\n"
                f"IP: <code>{ip_str}</code>\n\n"
                "Этот сайт не числится заблокированным — скорее всего открывается напрямую.\n"
                "Если он не открывается:\n"
                "• Нажмите «🌐 Открыть сайт через VPN»\n"
                "• Или «🆘 Сообщить о проблеме»"
            )
    except Exception:
        text = f"⚠️ Не удалось проверить <b>{domain}</b>. Попробуйте позже."

    await message.answer(text, reply_markup=return_kb(return_to, return_home), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Default handler
# ---------------------------------------------------------------------------
@router.message()
async def default_handler(message: Message, **kw):
    if await _is_admin(message, db=kw.get("db")):
        return
    db: Database = kw.get("db")
    if not db:
        return
    client = await db.get_client(str(message.from_user.id))
    if client:
        await message.answer("Неизвестная команда. /help")
    # Незарегистрированные — игнор


# ---------------------------------------------------------------------------
# Клиентские callback-обработчики: главное меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:mydevices")
async def cb_cl_mydevices(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    devices = await db.get_devices(chat_id)
    if not devices:
        await _edit_or_answer(
            cb,
            "Устройств нет. Нажмите «Добавить устройство».",
            client_devices_menu(),
        )
        return
    await _edit_or_answer(
        cb,
        "📱 <b>Ваши устройства</b> — выберите для управления:",
        devices_inline_kb(
            devices, "cl:dev:", "cl:devices_menu",
            footer=[InlineKeyboardButton(text="🔄 Обновить все конфиги", callback_data="cl:update")],
        ),
    )


@router.callback_query(F.data.startswith("cl:dev:"))
async def cb_cl_device_detail(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:dev:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    # Get peer status
    import time as _time
    now_ts = int(_time.time())
    hs_str = "никогда"
    try:
        peers_data = await _wc().get_peers()
        pk = device.get("public_key", "")
        for p in peers_data.get("peers", []):
            if p.get("public_key") == pk:
                hs = p.get("last_handshake", 0)
                if hs > 0:
                    mins = (now_ts - hs) // 60
                    hs_str = f"{mins} мин назад" if mins < 120 else f"{mins//60} ч назад"
                break
    except Exception:
        pass
    excludes_count = len(await db.get_excludes(device_id))
    server_routes_count = len(await db.get_server_routes(device_id))
    icon = "⏳" if device.get("pending_approval") else "✅"
    text = (
        f"<blockquote>Меню → Устройства → {device['device_name']}</blockquote>\n\n"
        f"{icon} <b>{device['device_name']}</b>\n"
        f"Состояние: <b>{'⏳ ожидает одобрения' if device.get('pending_approval') else '✅ активно'}</b>\n"
        f"Протокол: <code>{device['protocol'].upper()}</code>\n"
        f"IP: <code>{device.get('ip_address', 'N/A')}</code>\n"
        f"Последний handshake: <b>{hs_str}</b>\n"
        f"Исключения: <code>{excludes_count}</code> · Через сервер: <code>{server_routes_count}</code>"
    )
    await _edit_or_answer(cb, text, device_detail_kb(device_id))


@router.callback_query(F.data.startswith("cl:getconf:"))
async def cb_cl_getconf(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:getconf:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(
            cb,
            result_text("Устройство не найдено", "Возможно, оно уже было удалено.", status="warn", trail=["Меню", "Устройства"]),
            client_devices_menu(),
        )
        return
    if device.get("pending_approval"):
        await _edit_or_answer(
            cb,
            "⏳ Устройство ещё ожидает одобрения администратора.",
            client_devices_menu(),
        )
        return
    await _edit_or_answer(
        cb,
        f"<blockquote>Меню → Устройства → {device['device_name']} → Получить конфиг</blockquote>\n\n📱 <b>{device['device_name']}</b> — выберите формат конфига:",
        platform_inline_kb(device_id),
    )


@router.callback_query(F.data.startswith("cl:del:"))
async def cb_cl_del_device(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:del:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(
            cb,
            result_text("Устройство не найдено", "Возможно, оно уже было удалено.", status="warn", trail=["Меню", "Устройства"]),
            client_devices_menu(),
        )
        return
    await _edit_or_answer(
        cb,
        result_text(
            "Удалить устройство?",
            f"Устройство <code>{device['device_name']}</code> будет удалено вместе с его доступом.",
            status="warn",
            trail=["Меню", "Устройства", device["device_name"]],
            next_steps=["подтвердите удаление", "или вернитесь в карточку устройства"],
        ),
        confirm_kb(f"cl:del_ok:{device_id}", f"cl:dev:{device_id}"),
    )


@router.callback_query(F.data.startswith("cl:del_ok:"))
async def cb_cl_del_device_ok(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:del_ok:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(
            cb,
            result_text("Устройство не найдено", "Возможно, оно уже было удалено.", status="warn", trail=["Меню", "Устройства"]),
            client_devices_menu(),
        )
        return
    await _do_remove_device(cb.message, db, device)


@router.callback_query(F.data == "cl:myconfig")
async def cb_cl_myconfig(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    devices = await db.get_devices(chat_id)
    if not devices:
        await _edit_or_answer(cb, "Нет устройств.", client_devices_menu())
        return
    if len(devices) == 1:
        if devices[0].get("pending_approval"):
            await _edit_or_answer(
                cb,
                "⏳ Устройство ещё ожидает одобрения администратора.",
                client_devices_menu(),
            )
            return
        await _send_config(cb.message, db, devices[0], kw)
    else:
        await _edit_or_answer(
            cb,
            "Выберите устройство для получения конфига:",
            devices_inline_kb(devices, "cfg:", "cl:devices_menu"),
        )


@router.callback_query(F.data == "cl:adddevice")
async def cb_cl_adddevice(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    client = await db.get_client(chat_id)
    if not client:
        await _edit_or_answer(cb, "Сначала зарегистрируйтесь: /start", client_main_menu())
        return
    if client.get("is_disabled"):
        await cb.answer("❌ Ваш аккаунт отключён", show_alert=True)
        return
    if chat_id not in _adddevice_locks:
        _adddevice_locks[chat_id] = asyncio.Lock()
    async with _adddevice_locks[chat_id]:
        count = await db.count_devices(chat_id)
        limit = client.get("device_limit", config.device_limit_per_client)
        if count >= limit:
            await _edit_or_answer(
                cb,
                f"Достигнут лимит устройств: {count}/{limit}.\nОбратитесь к администратору.",
                client_devices_menu(),
            )
            return
    await start_prompt(
        cb,
        state,
        AddDeviceFSM.device_name,
        "Введите <b>имя нового устройства</b>:",
        "cl:devices_menu",
        home_cb="cl:menu",
    )


@router.callback_query(F.data == "cl:update")
async def cb_cl_update(cb: CallbackQuery, **kw):
    await cb.answer("Отправляю конфиги...")
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    client = await db.get_client(chat_id)
    if not client:
        await _edit_or_answer(cb, "Сначала зарегистрируйтесь: /start", client_main_menu())
        return
    devices = await db.get_devices(chat_id)
    active = [d for d in devices if not d.get("pending_approval")]
    if not active:
        await _edit_or_answer(cb, "Нет активных устройств.", client_connect_menu())
        return
    builder = ConfigBuilder()
    updated = 0
    same = 0
    for device in active:
        try:
            excludes, server_routes = await _device_policy_lists(db, device["id"])
            conf_text, _, version = await builder.build(device, excludes, server_routes)
            if version == device.get("config_version"):
                same += 1
                continue
            await _send_config(cb.message, db, device, kw)
            updated += 1
        except Exception as exc:
            logger.warning(f"cb_cl_update: {device.get('device_name')}: {exc}")
    if same > 0 and updated == 0:
        await _edit_or_answer(cb, "✅ Все конфиги актуальны (версия не изменилась).", client_connect_menu())
    elif same > 0:
        await _edit_or_answer(cb, f"ℹ️ {same} устройств без изменений.", client_connect_menu())


@router.callback_query(F.data.startswith("cl:upd1:"))
async def cb_cl_upd1_device(cb: CallbackQuery, **kw):
    """Обновить конфиг конкретного устройства (из device detail)."""
    await cb.answer("Проверяю конфиг...")
    device_id = int(cb.data[len("cl:upd1:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await _edit_or_answer(cb, "Устройство не найдено.", client_devices_menu())
        return
    if device.get("pending_approval"):
        await _edit_or_answer(
            cb,
            "⏳ Устройство ожидает одобрения администратора.",
            device_detail_kb(device_id),
        )
        return
    try:
        builder = ConfigBuilder()
        excludes, server_routes = await _device_policy_lists(db, device_id)
        _, _, version = await builder.build(device, excludes, server_routes)
        if version == device.get("config_version"):
            await _edit_or_answer(
                cb,
                "✅ Конфиг актуален, изменений нет.",
                device_detail_kb(device_id),
            )
        else:
            await _send_config(cb.message, db, device, kw)
    except Exception as exc:
        await _edit_or_answer(cb, f"❌ Ошибка: {exc}", device_detail_kb(device_id))


@router.callback_query(F.data == "cl:status")
async def cb_cl_status(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        ok    = s.get("status") == "ok"
        stack = s.get("active_stack", "N/A")
        text  = (
            f"{'✅ VPN работает' if ok else '⚠️ VPN деградирован'}\n"
            f"Протокол: <code>{stack}</code>"
        )
    except Exception:
        text = "❌ Не удалось получить статус"
    await _edit_or_answer(cb, text, client_connect_menu())


@router.callback_query(F.data == "cl:myrequests")
async def cb_cl_myrequests(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    reqs = await db.get_requests_by_client(str(cb.from_user.id))
    if not reqs:
        await _edit_or_answer(cb, "У вас нет запросов.", client_support_menu())
        return
    icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    lines = ["<b>Ваши запросы:</b>\n"]
    for r in reqs[:15]:
        icon = icons.get(r["status"], "?")
        lines.append(
            f"{icon} <code>{r['domain']}</code> ({r['direction']}) — {r['status']}\n"
            f"   {r['created_at'][:10]}"
        )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as IKB
    await _edit_or_answer(
        cb,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[
            [IKB(text="◀️ Назад", callback_data="cl:sites")],
        ]),
    )


@router.callback_query(F.data == "cl:help")
async def cb_cl_help(cb: CallbackQuery, **kw):
    await cb.answer()
    await _edit_or_answer(
        cb,
        section_text(
            "Помощь",
            "Обычному пользователю почти всё доступно через кнопки. Команды ниже можно использовать как быстрые сокращения.",
            icon="ℹ️",
            details=[
                "<code>/start</code> — открыть главное меню",
                "<code>/mydevices</code> — список устройств",
                "<code>/myconfig</code> — получить конфиг",
                "<code>/update</code> — обновить конфиги",
                "<code>/request vpn|direct &lt;домен&gt;</code> — запросить маршрут",
                "<code>/report &lt;текст&gt;</code> — сообщить о проблеме",
            ],
        ),
        client_support_menu(),
    )


# ---------------------------------------------------------------------------
# Callback: выбор устройства для конфига / удаления
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("cfg:"))
async def cb_device_config(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[4:])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.")
        return
    if device.get("pending_approval"):
        await cb.message.answer("⏳ Устройство ещё ожидает одобрения администратора.")
        return
    await cb.message.answer(
        f"📱 <b>{device['device_name']}</b> — выберите формат конфига:",
        reply_markup=platform_inline_kb(device_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cfgp:"))
async def cb_device_config_platform(cb: CallbackQuery, **kw):
    """Отправить конфиг устройства в выбранном формате."""
    await cb.answer()
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.message.answer("Некорректный запрос.")
        return
    device_id = int(parts[1])
    platform = parts[2]
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.")
        return
    if device.get("pending_approval"):
        await cb.message.answer("⏳ Устройство ещё ожидает одобрения администратора.")
        return

    builder = ConfigBuilder()
    excludes, server_routes = await _device_policy_lists(db, device["id"])

    had_keys = bool(device.get("private_key"))
    device = await builder.ensure_keys(device)
    if not had_keys and device.get("private_key"):
        from database import Database as _DB
        await db.update_device_keys(device["id"], device["private_key"], device["public_key"])
    conf_text, qr_bytes, version = await builder.build(device, excludes, server_routes)

    if platform == "ios":
        # QR + инструкция
        await cb.message.answer(
            "⚠️ <b>Конфигурация содержит приватный ключ!</b> Не передавайте никому.\n\n"
            "📱 <b>Установка на iOS/Android:</b>\n"
            "1. Установите приложение:\n"
            "   • iOS: <a href='https://apps.apple.com/app/amneziawg/id6478942951'>AmneziaWG</a> / <a href='https://apps.apple.com/app/wireguard/id1441195209'>WireGuard</a>\n"
            "   • Android: <a href='https://play.google.com/store/apps/details?id=org.amnezia.awg'>AmneziaWG</a> / <a href='https://play.google.com/store/apps/details?id=com.wireguard.android'>WireGuard</a>\n"
            "2. Отсканируйте QR-код ниже или импортируйте .conf файл.",
            parse_mode="HTML",
        )
        await cb.message.answer(_MOBILE_DNS_WARNING)
        if qr_bytes:
            await cb.message.answer_photo(
                BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=f"QR-код `{device['device_name']}`",
            )
        _dated = f"{device['device_name']}_{date.today()}"
        await cb.message.answer_document(
            BufferedInputFile(conf_text.encode(), filename=f"{_dated}.conf"),
            caption=f"Конфигурация `{device['device_name']}` · {date.today()}\n"
                    f"Если тоннель с таким именем уже есть в приложении — удалите старый и добавьте этот.",
        )
    elif platform == "conf":
        await cb.message.answer(
            "⚠️ <b>Конфигурация содержит приватный ключ!</b> Не передавайте никому.",
            parse_mode="HTML",
        )
        if qr_bytes:
            await cb.message.answer_photo(
                BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=f"QR-код `{device['device_name']}`",
            )
        _dated = f"{device['device_name']}_{date.today()}"
        await cb.message.answer_document(
            BufferedInputFile(conf_text.encode(), filename=f"{_dated}.conf"),
            caption=f"Конфигурация `{device['device_name']}` · {date.today()}\n"
                    f"Если тоннель с таким именем уже есть в приложении — удалите старый и добавьте этот.",
        )
    else:
        # windows / macos / linux — сохранить платформу, отправить .conf + installer script
        await db.update_device_platform(device_id, platform)
        from services.config_builder import build_installer, PLATFORM_SCRIPTS
        import re as _re
        _safe_name = _re.sub(r'[^\w\-]', '_', device["device_name"])
        _protocol = device.get("protocol", "awg")
        installer_bytes = build_installer(device["device_name"], conf_text, platform, protocol=_protocol)

        await cb.message.answer(
            "⚠️ <b>Конфигурация содержит приватный ключ!</b> Не передавайте никому.",
            parse_mode="HTML",
        )

        # Инструкция перед скриптом (отдельным сообщением)
        if platform == "windows":
            _install_hint = (
                "⚠️ Сохраните файл и запустите от администратора (ПКМ → Запуск от администратора). "
                "Windows может показать предупреждение — нажмите «Подробнее» → «Выполнить»."
            )
        elif platform == "macos":
            _install_hint = (
                "⚠️ Сохраните файл. При первом запуске: правый клик → Открыть. "
                "macOS покажет предупреждение о неизвестном разработчике — нажмите «Открыть»."
            )
        else:
            _install_hint = (
                "Сохраните файл и выполните:\n"
                "<code>chmod +x install-vpn-*.sh &amp;&amp; sudo ./install-vpn-*.sh</code>"
            )
        await cb.message.answer(_install_hint, parse_mode="HTML")
        _dated = f"{device['device_name']}_{date.today()}"
        await cb.message.answer_document(
            BufferedInputFile(conf_text.encode(), filename=f"{_dated}.conf"),
            caption=f"Конфигурация `{device['device_name']}` · {date.today()}",
        )
        if installer_bytes:
            ext = PLATFORM_SCRIPTS[platform]["ext"]
            label = PLATFORM_SCRIPTS[platform]["label"]
            await cb.message.answer_document(
                BufferedInputFile(installer_bytes, filename=f"install-vpn-{_safe_name}.{ext}"),
                caption=f"Установщик для {label}",
            )

    await db.update_config_version(device["id"], version)


@router.callback_query(F.data.startswith("rm:"))
async def cb_device_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[3:])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.")
        return
    await _do_remove_device(cb.message, db, device)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _now() -> float:
    import time
    return time.time()


async def _do_remove_device(message: Message, db: Database, device: dict) -> None:
    if device.get("public_key"):
        try:
            await _wc().remove_peer(device["public_key"])
        except Exception:
            pass
    await db.delete_device(device["id"])
    await message.answer(
        result_text(
            "Устройство удалено",
            f"Устройство <code>{device['device_name']}</code> удалено из вашего списка.",
            trail=["Меню", "Устройства"],
            next_steps=["при необходимости добавьте новое устройство", "или получите конфиг для оставшихся устройств"],
        ),
        reply_markup=client_main_menu(),
        parse_mode="HTML",
    )


async def _send_config(message: Message, db: Database, device: dict, kw: dict) -> None:
    """Отправить конфиг одного устройства пользователю."""
    builder = ConfigBuilder()
    excludes, server_routes = await _device_policy_lists(db, device["id"])

    had_keys = bool(device.get("private_key"))
    device = await builder.ensure_keys(device)
    if not had_keys and device.get("private_key"):
        await db.update_device_keys(device["id"], device["private_key"], device["public_key"])
    conf_text, qr_bytes, version = await builder.build(device, excludes, server_routes)

    # Предупреждение + пояснение типа конфига
    if device.get("is_router"):
        mode_note = (
            "🖥️ *Конфиг для роутера* — `AllowedIPs = 0.0.0.0/0`\n"
            "Весь трафик устройств за роутером идёт через VPN-сервер. "
            "Разделение трафика (российские сайты напрямую, заблокированные через VPN) "
            "выполняется автоматически на сервере.\n\n"
        )
    else:
        mode_note = (
            "📱 *Конфиг для телефона/ноутбука* — split tunneling на клиенте.\n"
            "Только заблокированные ресурсы идут через VPN, остальное — напрямую.\n"
            "Для отдельных IP/подсетей используйте раздел «📍 Через сервер».\n\n"
        )
    await message.answer(
        mode_note +
        "⚠️ *Конфигурация содержит приватный ключ!*\n"
        "Не передавайте никому. Рекомендуется включить 2FA."
    )
    if not device.get("is_router"):
        await message.answer(_MOBILE_DNS_WARNING)

    # QR
    if qr_bytes:
        await message.answer_photo(
            BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=f"QR-код `{device['device_name']}`",
        )

    # .conf файл
    if device.get("is_router"):
        _filename = f"vpn-{device['device_name']}.conf"
        _caption = f"Конфигурация `{device['device_name']}`"
    else:
        _filename = f"{device['device_name']}_{date.today()}.conf"
        _caption = (
            f"Конфигурация `{device['device_name']}` · {date.today()}\n"
            f"Если тоннель с таким именем уже есть в приложении — удалите старый и добавьте этот."
        )
    await message.answer_document(
        BufferedInputFile(conf_text.encode(), filename=_filename),
        caption=_caption,
    )

    # Установщик — если у устройства сохранена desktop-платформа
    _platform = device.get("platform")
    if _platform in ("windows", "macos", "linux"):
        from services.config_builder import build_installer, PLATFORM_SCRIPTS
        import re as _re
        _safe_name = _re.sub(r'[^\w\-]', '_', device["device_name"])
        _protocol = device.get("protocol", "awg")
        _installer = build_installer(device["device_name"], conf_text, _platform, protocol=_protocol)
        if _installer:
            _ext = PLATFORM_SCRIPTS[_platform]["ext"]
            _label = PLATFORM_SCRIPTS[_platform]["label"]
            _captions = {
                "windows": "Запустите .bat от имени администратора (ПКМ → Запуск от администратора)",
                "macos": "Дважды кликните .command. При первом запуске: ПКМ → Открыть",
                "linux": "chmod +x install-vpn-*.sh && sudo ./install-vpn-*.sh",
            }
            await message.answer_document(
                BufferedInputFile(_installer, filename=f"install-vpn-{_safe_name}.{_ext}"),
                caption=f"Установщик для {_label}\n{_captions[_platform]}",
            )

    await db.update_config_version(device["id"], version)

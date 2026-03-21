"""
handlers/client.py — Команды клиентов (самообслуживание)

Команды (из CLAUDE.md):
  /start — регистрация (FSM: invite → имя → протокол)
  /mydevices /myconfig /adddevice /removedevice
  /update /request /myrequests
  /exclude add|remove|list
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
    client_excludes_menu,
    client_main_menu,
    client_request_type_kb,
    client_sites_menu,
    confirm_kb,
    device_detail_kb,
    devices_inline_kb,
    excludes_inline_kb,
    menu_reply_kb,
    platform_inline_kb,
    proto_inline_kb,
)

from config import config
from database import Database
from services.config_builder import ConfigBuilder
from services.watchdog_client import WatchdogClient

if TYPE_CHECKING:
    from aiogram import Bot
    from services.autodist import AutoDist

logger = logging.getLogger(__name__)
router = Router()


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


class ReportFSM(StatesGroup):
    text = State()


class CheckSiteFSM(StatesGroup):
    domain = State()


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
def _wc() -> WatchdogClient:
    return WatchdogClient(config.watchdog_url, config.watchdog_token)


def _is_admin(message: Message) -> bool:
    return str(message.from_user.id) == str(config.admin_chat_id)


async def _get_client(message: Message, **kw) -> dict | None:
    db: Database = kw.get("db")
    return await db.get_client(str(message.from_user.id))


def _parse_protocol(text: str) -> str:
    t = text.lower()
    return "awg" if ("awg" in t or "amnezia" in t) else "wg"


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
# Bootstrap-регистрация (без выбора протокола — AWG пир уже создан)
# ---------------------------------------------------------------------------
async def _complete_bootstrap_registration(
    message: Message,
    state: FSMContext,
    kw: dict,
) -> None:
    """Завершить регистрацию по bootstrap-инвайту.

    Два случая:
    - last_handshake > 0: пользователь подключился через bootstrap AWG пир →
      принимаем пир как постоянный, удаляем только WG temp пир.
    - last_handshake == 0: конфиги не использовались →
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

    # Проверяем, подключался ли пользователь через bootstrap AWG пир
    peer_was_used = False
    try:
        wdc = WatchdogClient(config.watchdog_url, config.watchdog_token)
        peers_info = await wdc.get_peers()
        for p in (peers_info or {}).get("peers", []):
            if p.get("public_key") == awg_peer_id and p.get("last_handshake", 0) > 0:
                peer_was_used = True
                break
    except Exception:
        # Если watchdog недоступен — предполагаем что пользовался (безопаснее)
        peer_was_used = True

    if not peer_was_used:
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

    # Пользователь подключился через bootstrap AWG пир — принимаем его.
    try:
        await db.register_client(chat_id, username, invite_code, first_name)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        await state.clear()
        return

    # Регистрируем AWG устройство с предсозданными ключами и IP (пир уже в wg0)
    device = await db.add_device(
        chat_id, device_name, "awg",
        public_key=awg_peer_id,
        private_key=bootstrap.get("awg_privkey", ""),
        ip_address=bootstrap.get("awg_ip") or None,
    )
    # Удаляем только WG temp пир (AWG остаётся)
    try:
        await WatchdogClient(config.watchdog_url, config.watchdog_token).remove_peer(
            wg_peer_id, interface="wg1"
        )
    except Exception:
        pass

    await message.answer("📋 Меню", reply_markup=menu_reply_kb())
    await message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"Устройство: `{device_name}`\n"
        f"Протокол: `AWG`\n\n"
        f"Ваш конфиг уже работает — ничего менять не нужно.\n"
        f"/myconfig — посмотреть или переслать конфиг ещё раз.",
        reply_markup=client_main_menu(),
    )
    await state.clear()

    autodist: "AutoDist" = kw.get("autodist")
    if autodist and device:
        asyncio.create_task(autodist.send_to_device(chat_id, device, "Регистрация"))


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
            excludes_raw = await db.get_excludes(device["id"])
            excludes = [e["subnet"] for e in excludes_raw]
            conf_text, _, version = await builder.build(device, excludes)
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

    count = await db.count_devices(str(message.from_user.id))
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
    chat_id   = str(cb.from_user.id)
    await state.clear()
    await cb.answer()

    try:
        device = await db.add_device(chat_id, data["device_name"], protocol, pending=True, is_router=is_router)
        await cb.message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Конфиг придёт после одобрения.",
            reply_markup=client_main_menu(),
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
        await cb.message.answer(f"❌ {e}", reply_markup=client_main_menu())


@router.message(AddDeviceFSM.protocol)
async def adddev_protocol(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    protocol = _parse_protocol(message.text)
    data     = await state.get_data()
    chat_id  = str(message.from_user.id)
    await state.clear()

    try:
        device = await db.add_device(chat_id, data["device_name"], protocol, pending=True)
        await message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Конфиг придёт после одобрения.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer("Выберите действие:", reply_markup=client_main_menu())
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

    subnet = args[2]
    try:
        ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        await message.answer(f"Неверный формат подсети: `{subnet}`")
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
        await db.add_exclude(device["id"], subnet)
        await message.answer(f"✅ `{subnet}` исключён из VPN для `{device['device_name']}`")
    else:
        await db.remove_exclude(device["id"], subnet)
        await message.answer(f"✅ `{subnet}` возвращён в VPN для `{device['device_name']}`")


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
    if _is_admin(message):
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
    if _is_admin(message):
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
        "/report <текст> — сообщить о проблеме\n"
        "/status — статус VPN\n"
        "/help — эта справка",
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
    if _is_admin(message):
        from handlers.keyboards import admin_main_menu
        await message.answer("<b>Меню администратора</b>", reply_markup=admin_main_menu(), parse_mode="HTML")
    else:
        await message.answer("<b>Меню</b>", reply_markup=client_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /menu для клиентов
# ---------------------------------------------------------------------------
@router.message(Command("menu"), StateFilter("*"))
async def cmd_menu_client(message: Message, state: FSMContext, **kw):
    if _is_admin(message):
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
    try:
        await cb.message.edit_text(header, reply_markup=client_main_menu(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(header, reply_markup=client_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: «cl:removedevice» — удалить устройство через меню
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "cl:removedevice")
async def cb_cl_removedevice(cb: CallbackQuery, **kw):
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    if not devices:
        await cb.answer("Нет устройств")
        await cb.message.answer("Нет устройств.", reply_markup=client_main_menu())
        return
    if len(devices) == 1:
        await cb.answer()
        await _do_remove_device(cb.message, db, devices[0])
    else:
        await cb.answer()
        await cb.message.answer("Выберите устройство для удаления:",
                                reply_markup=devices_inline_kb(devices, "rm:", "cl:menu"))


# ---------------------------------------------------------------------------
# Запрос маршрута через меню (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:sites")
async def cb_cl_sites(cb: CallbackQuery, **kw):
    await cb.answer()
    try:
        await cb.message.edit_text(
            "🌐 <b>Сайты через VPN</b>\n\nЗапросить добавление сайта или посмотреть статус запросов.",
            reply_markup=client_sites_menu(),
            parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            "🌐 <b>Сайты через VPN</b>",
            reply_markup=client_sites_menu(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "cl:request")
async def cb_cl_request(cb: CallbackQuery, **kw):
    await cb.answer()
    await cb.message.answer("Куда направить домен?", reply_markup=client_request_type_kb())


@router.callback_query(F.data.startswith("cl:req:"))
async def cb_cl_request_type(cb: CallbackQuery, state: FSMContext, **kw):
    direction = cb.data[len("cl:req:"):]
    await cb.answer()
    label = "VPN" if direction == "vpn" else "Direct"
    await cb.message.answer(f"Введите домен для маршрута *{label}*\n(например: `example.com`):")
    await state.update_data(_req_direction=direction, _fsm_ts=_now())
    await state.set_state(RequestFSM.domain)


@router.message(RequestFSM.domain)
async def fsm_request_domain(message: Message, state: FSMContext, **kw):
    data = await state.get_data()
    direction = data.get("_req_direction", "vpn")
    domain = message.text.strip().lower().strip(".")
    await state.clear()
    db: Database = kw.get("db")
    bot = kw.get("bot")
    req_id = await db.create_domain_request(str(message.from_user.id), domain, direction)
    await message.answer(
        f"✅ Запрос #{req_id} отправлен.\nДомен: `{domain}` → {direction}",
        reply_markup=client_main_menu(),
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
    try:
        await cb.message.edit_text("🚫 *Исключения из VPN*", reply_markup=client_excludes_menu())
    except Exception:
        await cb.message.answer("🚫 *Исключения из VPN*", reply_markup=client_excludes_menu())


@router.callback_query(F.data == "cl:ex_list")
async def cb_cl_ex_list(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    lines = []
    for d in devices:
        exs = await db.get_excludes(d["id"])
        if exs:
            lines.append(f"*{d['device_name']}:*")
            lines.extend(f"  • `{e['subnet']}`" for e in exs)
    text = "\n".join(lines) if lines else "Исключений нет."
    await cb.message.answer(text, reply_markup=client_excludes_menu())


@router.callback_query(F.data == "cl:ex_add")
async def cb_cl_ex_add(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    if not devices:
        await cb.message.answer("Нет устройств.", reply_markup=client_main_menu())
        return
    await state.update_data(_ex_device_id=devices[0]["id"], _fsm_ts=_now())
    await cb.message.answer(
        f"Введите подсеть для исключения\n(например: `192.168.1.0/24`):"
    )
    await state.set_state(ExcludeFSM.subnet)


@router.message(ExcludeFSM.subnet)
async def fsm_exclude_subnet(message: Message, state: FSMContext, **kw):
    import ipaddress
    subnet = message.text.strip()
    try:
        ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        await message.answer(f"❌ Неверный формат: `{subnet}`\nПример: `192.168.1.0/24`")
        return
    data = await state.get_data()
    device_id = data.get("_ex_device_id")
    await state.clear()
    db: Database = kw.get("db")
    await db.add_exclude(device_id, subnet)
    await message.answer(f"✅ `{subnet}` добавлен в исключения.", reply_markup=client_main_menu())


@router.callback_query(F.data == "cl:ex_remove")
async def cb_cl_ex_remove(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    devices = await db.get_devices(str(cb.from_user.id))
    # Показываем исключения первого устройства с исключениями
    for d in devices:
        exs = await db.get_excludes(d["id"])
        if exs:
            await cb.message.answer(
                f"Исключения устройства *{d['device_name']}*:",
                reply_markup=excludes_inline_kb(exs, d["id"]),
            )
            return
    await cb.message.answer("Исключений нет.", reply_markup=client_excludes_menu())


@router.callback_query(F.data.startswith("cl:ex_del:"))
async def cb_cl_ex_del(cb: CallbackQuery, **kw):
    parts = cb.data[len("cl:ex_del:"):].split(":", 1)
    device_id = int(parts[0])
    subnet = parts[1]
    db: Database = kw.get("db")
    await db.remove_exclude(device_id, subnet)
    await cb.answer(f"Удалено: {subnet}")
    await cb.message.edit_text(f"✅ `{subnet}` удалён из исключений.")


# ---------------------------------------------------------------------------
# Сообщить о проблеме через меню (FSM)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cl:report")
async def cb_cl_report(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    await cb.message.answer("Опишите проблему:")
    await state.update_data(_fsm_ts=_now())
    await state.set_state(ReportFSM.text)


@router.message(ReportFSM.text)
async def fsm_report_text(message: Message, state: FSMContext, **kw):
    text = message.text.strip()
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
    await message.answer("✅ Сообщение отправлено администратору.", reply_markup=client_main_menu())


# ---------------------------------------------------------------------------
# Проверка сайта: почему не работает?
# ---------------------------------------------------------------------------

_MANUAL_VPN    = "/etc/vpn-routes/manual-vpn.txt"
_MANUAL_DIRECT = "/etc/vpn-routes/manual-direct.txt"


@router.callback_query(F.data == "cl:checksite")
async def cb_cl_checksite(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    client = await db.get_client(str(cb.from_user.id))
    if not client:
        await cb.message.answer("Сначала зарегистрируйтесь: /start")
        return
    await cb.message.answer("Введите адрес сайта (например: <code>youtube.com</code>):", parse_mode="HTML")
    await state.update_data(_fsm_ts=_now())
    await state.set_state(CheckSiteFSM.domain)


@router.message(CheckSiteFSM.domain)
async def fsm_checksite_domain(message: Message, state: FSMContext, **kw):
    import os as _os
    domain = message.text.strip().lower().strip(".").replace("https://", "").replace("http://", "").split("/")[0]
    await state.clear()

    try:
        r = await _wc().check_domain(domain)
        verdict   = r.get("verdict", "unknown")
        ips       = r.get("ips", [])
        in_static = r.get("in_blocked_static", False)
        in_dyn    = r.get("in_blocked_dynamic", False)
        in_manual = r.get("in_manual_vpn", False)
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

    await message.answer(text, reply_markup=client_main_menu(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Default handler
# ---------------------------------------------------------------------------
@router.message()
async def default_handler(message: Message, **kw):
    if _is_admin(message):
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
        await cb.message.answer(
            "Устройств нет. Нажмите «Добавить устройство».",
            reply_markup=client_main_menu(),
        )
        return
    # Show device list with buttons to device detail page
    await cb.message.answer(
        "📱 <b>Ваши устройства</b> — выберите для управления:",
        reply_markup=devices_inline_kb(
            devices, "cl:dev:", "cl:menu",
            footer=[InlineKeyboardButton(text="🔄 Обновить все конфиги", callback_data="cl:update")],
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cl:dev:"))
async def cb_cl_device_detail(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:dev:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.", reply_markup=client_main_menu())
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
    icon = "⏳" if device.get("pending_approval") else "✅"
    text = (
        f"{icon} <b>{device['device_name']}</b>\n"
        f"Протокол: <code>{device['protocol'].upper()}</code>\n"
        f"IP: <code>{device.get('ip_address', 'N/A')}</code>\n"
        f"Последний handshake: {hs_str}"
    )
    await cb.message.answer(text, reply_markup=device_detail_kb(device_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("cl:getconf:"))
async def cb_cl_getconf(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:getconf:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.")
        return
    if device.get("pending_approval"):
        await cb.message.answer(
            "⏳ Устройство ещё ожидает одобрения администратора.",
            reply_markup=client_main_menu(),
        )
        return
    await cb.message.answer(
        f"📱 <b>{device['device_name']}</b> — выберите формат конфига:",
        reply_markup=platform_inline_kb(device_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cl:del:"))
async def cb_cl_del_device(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:del:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.")
        return
    await cb.message.edit_text(
        f"🗑 <b>Удалить устройство «{device['device_name']}»?</b>",
        reply_markup=confirm_kb(f"cl:del_ok:{device_id}", f"cl:dev:{device_id}"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cl:del_ok:"))
async def cb_cl_del_device_ok(cb: CallbackQuery, **kw):
    await cb.answer()
    device_id = int(cb.data[len("cl:del_ok:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.edit_text("Устройство не найдено.")
        return
    await _do_remove_device(cb.message, db, device)


@router.callback_query(F.data == "cl:myconfig")
async def cb_cl_myconfig(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    devices = await db.get_devices(chat_id)
    if not devices:
        await cb.message.answer("Нет устройств.", reply_markup=client_main_menu())
        return
    if len(devices) == 1:
        if devices[0].get("pending_approval"):
            await cb.message.answer(
                "⏳ Устройство ещё ожидает одобрения администратора.",
                reply_markup=client_main_menu(),
            )
            return
        await _send_config(cb.message, db, devices[0], kw)
    else:
        await cb.message.answer(
            "Выберите устройство для получения конфига:",
            reply_markup=devices_inline_kb(devices, "cfg:"),
        )


@router.callback_query(F.data == "cl:adddevice")
async def cb_cl_adddevice(cb: CallbackQuery, state: FSMContext, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    client = await db.get_client(chat_id)
    if not client:
        await cb.message.answer("Сначала зарегистрируйтесь: /start")
        return
    count = await db.count_devices(chat_id)
    limit = client.get("device_limit", config.device_limit_per_client)
    if count >= limit:
        await cb.message.answer(
            f"Достигнут лимит устройств: {count}/{limit}.\nОбратитесь к администратору.",
            reply_markup=client_main_menu(),
        )
        return
    await cb.message.answer("Введите *имя нового устройства*:")
    await state.update_data(_fsm_ts=_now())
    await state.set_state(AddDeviceFSM.device_name)


@router.callback_query(F.data == "cl:update")
async def cb_cl_update(cb: CallbackQuery, **kw):
    await cb.answer("Отправляю конфиги...")
    db: Database = kw.get("db")
    chat_id = str(cb.from_user.id)
    client = await db.get_client(chat_id)
    if not client:
        await cb.message.answer("Сначала зарегистрируйтесь: /start")
        return
    devices = await db.get_devices(chat_id)
    active = [d for d in devices if not d.get("pending_approval")]
    if not active:
        await cb.message.answer("Нет активных устройств.", reply_markup=client_main_menu())
        return
    builder = ConfigBuilder()
    updated = 0
    same = 0
    for device in active:
        try:
            excludes_raw = await db.get_excludes(device["id"])
            excludes = [e["subnet"] for e in excludes_raw]
            conf_text, _, version = await builder.build(device, excludes)
            if version == device.get("config_version"):
                same += 1
                continue
            await _send_config(cb.message, db, device, kw)
            updated += 1
        except Exception as exc:
            logger.warning(f"cb_cl_update: {device.get('device_name')}: {exc}")
    if same > 0 and updated == 0:
        await cb.message.answer("✅ Все конфиги актуальны (версия не изменилась).", reply_markup=client_main_menu())
    elif same > 0:
        await cb.message.answer(f"ℹ️ {same} устройств без изменений.")


@router.callback_query(F.data.startswith("cl:upd1:"))
async def cb_cl_upd1_device(cb: CallbackQuery, **kw):
    """Обновить конфиг конкретного устройства (из device detail)."""
    await cb.answer("Проверяю конфиг...")
    device_id = int(cb.data[len("cl:upd1:"):])
    db: Database = kw.get("db")
    device = await db.get_device_by_id(device_id)
    if not device:
        await cb.message.answer("Устройство не найдено.", reply_markup=client_main_menu())
        return
    if device.get("pending_approval"):
        await cb.message.answer(
            "⏳ Устройство ожидает одобрения администратора.",
            reply_markup=device_detail_kb(device_id),
        )
        return
    try:
        builder = ConfigBuilder()
        excludes_raw = await db.get_excludes(device_id)
        excludes = [e["subnet"] for e in excludes_raw]
        _, _, version = await builder.build(device, excludes)
        if version == device.get("config_version"):
            await cb.message.answer(
                "✅ Конфиг актуален, изменений нет.",
                reply_markup=device_detail_kb(device_id),
            )
        else:
            await _send_config(cb.message, db, device, kw)
    except Exception as exc:
        await cb.message.answer(f"❌ Ошибка: {exc}", reply_markup=device_detail_kb(device_id))


@router.callback_query(F.data == "cl:status")
async def cb_cl_status(cb: CallbackQuery, **kw):
    await cb.answer("Загружаю...")
    try:
        s = await _wc().get_status()
        ok    = s.get("status") == "ok"
        stack = s.get("active_stack", "N/A")
        text  = (
            f"{'✅ VPN работает' if ok else '⚠️ VPN деградирован'}\n"
            f"Протокол: `{stack}`"
        )
    except Exception:
        text = "❌ Не удалось получить статус"
    await cb.message.answer(text, reply_markup=client_main_menu())


@router.callback_query(F.data == "cl:myrequests")
async def cb_cl_myrequests(cb: CallbackQuery, **kw):
    await cb.answer()
    db: Database = kw.get("db")
    reqs = await db.get_requests_by_client(str(cb.from_user.id))
    if not reqs:
        await cb.message.answer("У вас нет запросов.", reply_markup=client_sites_menu())
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
    await cb.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [IKB(text="◀️ Назад", callback_data="cl:sites")],
        ]),
    )


@router.callback_query(F.data == "cl:help")
async def cb_cl_help(cb: CallbackQuery, **kw):
    await cb.answer()
    await cb.message.answer(
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
        "/report <текст> — сообщить о проблеме\n"
        "/status — статус VPN\n"
        "/help — эта справка",
        reply_markup=client_main_menu(),
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
    excludes_raw = await db.get_excludes(device["id"])
    excludes = [e["subnet"] for e in excludes_raw]

    had_keys = bool(device.get("private_key"))
    device = await builder.ensure_keys(device)
    if not had_keys and device.get("private_key"):
        from database import Database as _DB
        await db.update_device_keys(device["id"], device["private_key"], device["public_key"])
    conf_text, qr_bytes, version = await builder.build(device, excludes)

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
        # windows / macos / linux — отправить .conf + installer script
        from services.config_builder import build_installer
        installer_bytes = build_installer(device["device_name"], conf_text, platform)
        await cb.message.answer(
            "⚠️ <b>Конфигурация содержит приватный ключ!</b> Не передавайте никому.",
            parse_mode="HTML",
        )
        _dated = f"{device['device_name']}_{date.today()}"
        await cb.message.answer_document(
            BufferedInputFile(conf_text.encode(), filename=f"{_dated}.conf"),
            caption=f"Конфигурация `{device['device_name']}` · {date.today()}",
        )
        if installer_bytes:
            from services.config_builder import PLATFORM_SCRIPTS
            ext = PLATFORM_SCRIPTS[platform]["ext"]
            label = PLATFORM_SCRIPTS[platform]["label"]
            await cb.message.answer_document(
                BufferedInputFile(installer_bytes, filename=f"install-vpn-{device['device_name']}.{ext}"),
                caption=f"Установщик для {label}\nЗапустите от имени администратора.",
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
        f"✅ Устройство `{device['device_name']}` удалено.",
        reply_markup=client_main_menu(),
    )


async def _send_config(message: Message, db: Database, device: dict, kw: dict) -> None:
    """Отправить конфиг одного устройства пользователю."""
    builder = ConfigBuilder()
    excludes_raw = await db.get_excludes(device["id"])
    excludes = [e["subnet"] for e in excludes_raw]

    had_keys = bool(device.get("private_key"))
    device = await builder.ensure_keys(device)
    if not had_keys and device.get("private_key"):
        await db.update_device_keys(device["id"], device["private_key"], device["public_key"])
    conf_text, qr_bytes, version = await builder.build(device, excludes)

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
            "Только заблокированные ресурсы идут через VPN, остальное — напрямую.\n\n"
        )
    await message.answer(
        mode_note +
        "⚠️ *Конфигурация содержит приватный ключ!*\n"
        "Не передавайте никому. Рекомендуется включить 2FA."
    )

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

    await db.update_config_version(device["id"], version)

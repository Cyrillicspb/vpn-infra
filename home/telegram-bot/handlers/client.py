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
import io
import ipaddress
import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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


def _proto_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="AWG (рекомендуется)"),
            KeyboardButton(text="WireGuard"),
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


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
        await message.answer(
            f"Добро пожаловать!\n\n"
            f"Устройств: {len(devices)}\n\n"
            f"/mydevices — список  /myconfig — конфиг\n"
            f"/adddevice — добавить  /help — помощь"
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
    await state.update_data(invite_code=code, _fsm_ts=_now())
    await message.answer(
        "✅ Код принят!\n\nВведите *имя вашего устройства* (iPhone, MacBook, PC…):"
    )
    await state.set_state(RegFSM.device_name)


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
    await message.answer("Выберите *протокол*:", reply_markup=_proto_kb())
    await state.set_state(RegFSM.protocol)


# ---------------------------------------------------------------------------
# Регистрация: протокол → завершение
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

    try:
        await db.register_client(chat_id, username, invite_code)
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    # Добавляем первое устройство
    builder = ConfigBuilder()
    device = await db.add_device(chat_id, device_name, protocol, pending=False)
    device = await builder.ensure_keys(device)

    # Добавляем WG пир
    try:
        await _wc().add_peer(device_name, protocol, device.get("public_key", ""))
    except Exception:
        pass

    await message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"Устройство: `{device_name}`\n"
        f"Протокол: `{protocol.upper()}`\n\n"
        f"Используйте /myconfig для получения конфига.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()

    # Отправляем конфиг сразу
    autodist: "AutoDist" = kw.get("autodist")
    if autodist:
        asyncio.create_task(autodist.send_to_device(chat_id, device, "Регистрация"))


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
    else:
        device = devices[0]

    if device.get("pending_approval"):
        await message.answer("⏳ Устройство ещё ожидает одобрения администратора.")
        return

    await _send_config(message, db, device, kw)


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

    for device in active:
        await _send_config(message, db, device, kw)


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
    await message.answer("Выберите *протокол*:", reply_markup=_proto_kb())
    await state.set_state(AddDeviceFSM.protocol)


@router.message(AddDeviceFSM.protocol)
async def adddev_protocol(message: Message, state: FSMContext, **kw):
    db: Database = kw.get("db")
    protocol = _parse_protocol(message.text)
    data     = await state.get_data()
    chat_id  = str(message.from_user.id)
    await state.clear()

    try:
        device = await db.add_device(
            chat_id, data["device_name"], protocol, pending=True
        )
        await message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Конфиг придёт после одобрения.",
            reply_markup=ReplyKeyboardRemove(),
        )
        # Уведомляем admin
        bot: "Bot" = kw.get("bot")
        if bot:
            asyncio.create_task(
                bot.send_message(
                    config.admin_chat_id,
                    f"📱 Новый запрос на устройство!\n"
                    f"Клиент: `{chat_id}`  Устройство: `{data['device_name']}`\n"
                    f"Протокол: `{protocol.upper()}`\n"
                    f"/requests — для одобрения",
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
    elif len(devices) == 1:
        device = devices[0]
    else:
        names = "\n".join(f"• `{d['device_name']}`" for d in devices)
        await message.answer(
            f"Укажите устройство:\n{names}\n\n"
            f"Пример: `/removedevice iPhone`"
        )
        return

    # Удаляем пир из WireGuard
    if device.get("public_key"):
        try:
            await _wc().remove_peer(device["public_key"])
        except Exception:
            pass
    await db.delete_device(device["id"])
    await message.answer(f"✅ Устройство `{device['device_name']}` удалено.")


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
                lines.extend(f"  • `{e}`" for e in exs)
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
        "/help — эта справка"
    )


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
# Утилиты
# ---------------------------------------------------------------------------
def _now() -> float:
    import time
    return time.time()


async def _send_config(message: Message, db: Database, device: dict, kw: dict) -> None:
    """Отправить конфиг одного устройства пользователю."""
    builder = ConfigBuilder()
    excludes = await db.get_excludes(device["id"])

    device = await builder.ensure_keys(device)
    conf_text, qr_bytes, version = await builder.build(device, excludes)

    # Предупреждение
    await message.answer(
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
    await message.answer_document(
        BufferedInputFile(conf_text.encode(), filename=f"vpn-{device['device_name']}.conf"),
        caption=f"Конфигурация `{device['device_name']}`",
    )

    await db.update_config_version(device["id"], version)

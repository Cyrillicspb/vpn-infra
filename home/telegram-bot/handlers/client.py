"""
handlers/client.py — Команды клиентов (самообслуживание)

FSM для регистрации: /start → invite_code → device_name → protocol
"""
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from config import config
from database import Database
from services.config_builder import ConfigBuilder

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# FSM состояния регистрации
# ---------------------------------------------------------------------------
class RegistrationFSM(StatesGroup):
    waiting_invite_code = State()
    waiting_device_name = State()
    waiting_protocol = State()


class AddDeviceFSM(StatesGroup):
    waiting_device_name = State()
    waiting_protocol = State()


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
async def get_db_and_client(message: Message, **kwargs):
    db: Database = kwargs.get("db")
    client = await db.get_client_by_chat_id(str(message.from_user.id))
    return db, client


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, **kwargs):
    """
    Начало работы.
    - Уже зарегистрирован → показать устройства
    - Новый пользователь → запросить invite код
    """
    db: Database = kwargs.get("db")
    chat_id = str(message.from_user.id)

    # Сбрасываем FSM
    await state.clear()

    client = await db.get_client_by_chat_id(chat_id)

    if client:
        # Уже зарегистрирован
        if client.get("is_disabled"):
            await message.answer("❌ Ваш аккаунт отключён. Обратитесь к администратору.")
            return

        devices = await db.get_devices_by_client(chat_id)
        text = (
            f"Добро пожаловать!\n\n"
            f"Ваш профиль:\n"
            f"Имя: `{client['device_name']}`\n"
            f"Устройств: {len(devices)}\n\n"
            f"Команды:\n"
            f"/mydevices — мои устройства\n"
            f"/myconfig — получить конфиг\n"
            f"/adddevice — добавить устройство\n"
            f"/help — все команды"
        )
        await message.answer(text)
    else:
        # Новый пользователь
        await message.answer(
            "Добро пожаловать в VPN!\n\n"
            "Для регистрации введите *код приглашения*:"
        )
        await state.set_state(RegistrationFSM.waiting_invite_code)


# ---------------------------------------------------------------------------
# Регистрация: invite код
# ---------------------------------------------------------------------------
@router.message(RegistrationFSM.waiting_invite_code)
async def process_invite_code(message: Message, state: FSMContext, **kwargs):
    db: Database = kwargs.get("db")
    code = message.text.strip()

    # Резервируем код
    if not await db.reserve_invite_code(code, str(message.from_user.id)):
        await message.answer("❌ Неверный, использованный или истёкший код.\nПопробуйте другой:")
        return

    await state.update_data(invite_code=code)
    await message.answer(
        "✅ Код принят!\n\n"
        "Введите *имя вашего устройства* (например: iPhone, MacBook, PC):"
    )
    await state.set_state(RegistrationFSM.waiting_device_name)


# ---------------------------------------------------------------------------
# Регистрация: имя устройства
# ---------------------------------------------------------------------------
@router.message(RegistrationFSM.waiting_device_name)
async def process_device_name(message: Message, state: FSMContext, **kwargs):
    name = message.text.strip()

    if len(name) < 2 or len(name) > 30:
        await message.answer("Имя должно быть от 2 до 30 символов. Попробуйте ещё раз:")
        return

    await state.update_data(device_name=name)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="AWG (рекомендуется)"), KeyboardButton(text="WireGuard")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Выберите *протокол*:\n\n"
        "• *AWG* — AmneziaWG, лучше обходит DPI\n"
        "• *WireGuard* — стандартный WireGuard",
        reply_markup=keyboard,
    )
    await state.set_state(RegistrationFSM.waiting_protocol)


# ---------------------------------------------------------------------------
# Регистрация: протокол → завершение
# ---------------------------------------------------------------------------
@router.message(RegistrationFSM.waiting_protocol)
async def process_protocol(message: Message, state: FSMContext, **kwargs):
    db: Database = kwargs.get("db")
    text = message.text.lower()

    if "awg" in text or "amnezia" in text:
        protocol = "awg"
    elif "wireguard" in text or "wg" in text:
        protocol = "wg"
    else:
        await message.answer("Пожалуйста, выберите AWG или WireGuard:")
        return

    data = await state.get_data()
    invite_code = data["invite_code"]
    device_name = data["device_name"]
    chat_id = str(message.from_user.id)

    try:
        await db.register_client(chat_id, device_name, protocol, invite_code)

        await message.answer(
            f"✅ *Регистрация завершена!*\n\n"
            f"Устройство: `{device_name}`\n"
            f"Протокол: `{protocol.upper()}`\n\n"
            f"Используйте /myconfig для получения конфигурации.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except ValueError as e:
        await message.answer(f"❌ Ошибка регистрации: {e}")
    finally:
        await state.clear()


# ---------------------------------------------------------------------------
# /mydevices
# ---------------------------------------------------------------------------
@router.message(Command("mydevices"))
async def cmd_mydevices(message: Message, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    devices = await db.get_devices_by_client(str(message.from_user.id))
    if not devices:
        await message.answer("У вас нет устройств. /adddevice — добавить.")
        return

    lines = ["*Ваши устройства:*\n"]
    for d in devices:
        status = "⏳ Ожидает" if d.get("pending_approval") else "✅"
        lines.append(f"{status} `{d['device_name']}` ({d['protocol'].upper()}) — IP: `{d.get('ip_address', 'N/A')}`")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /myconfig [имя_устройства]
# ---------------------------------------------------------------------------
@router.message(Command("myconfig"))
async def cmd_myconfig(message: Message, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split(maxsplit=1)
    devices = await db.get_devices_by_client(str(message.from_user.id))

    if not devices:
        await message.answer("Нет устройств. /adddevice — добавить.")
        return

    # Выбираем устройство
    if len(args) > 1:
        device_name = args[1]
        device = next((d for d in devices if d["device_name"] == device_name), None)
        if not device:
            await message.answer(f"Устройство `{device_name}` не найдено.")
            return
    else:
        device = devices[0]

    try:
        builder = ConfigBuilder()
        conf_text, qr_image = await builder.build(device)

        # Предупреждение о приватном ключе
        await message.answer(
            "⚠️ *Конфигурация содержит приватный ключ!*\n"
            "Не передавайте его никому. Рекомендуется включить 2FA на устройстве."
        )

        if qr_image:
            await message.answer_photo(qr_image, caption=f"QR-код для `{device['device_name']}`")

        import io
        await message.answer_document(
            document=io.BytesIO(conf_text.encode()),
            filename=f"vpn-{device['device_name']}.conf",
            caption=f"Конфигурация для `{device['device_name']}`"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации конфига: {e}")


# ---------------------------------------------------------------------------
# /adddevice
# ---------------------------------------------------------------------------
@router.message(Command("adddevice"))
async def cmd_adddevice(message: Message, state: FSMContext, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    # Проверяем лимит устройств
    count = await db.count_devices(str(message.from_user.id))
    limit = client.get("device_limit", config.device_limit_per_client)

    if count >= limit:
        await message.answer(f"Достигнут лимит устройств ({limit}). Обратитесь к администратору.")
        return

    await message.answer("Введите имя нового устройства:")
    await state.set_state(AddDeviceFSM.waiting_device_name)


@router.message(AddDeviceFSM.waiting_device_name)
async def process_adddevice_name(message: Message, state: FSMContext, **kwargs):
    name = message.text.strip()
    if len(name) < 2 or len(name) > 30:
        await message.answer("Имя от 2 до 30 символов:")
        return

    await state.update_data(device_name=name)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="AWG"), KeyboardButton(text="WireGuard")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Выберите протокол:", reply_markup=keyboard)
    await state.set_state(AddDeviceFSM.waiting_protocol)


@router.message(AddDeviceFSM.waiting_protocol)
async def process_adddevice_protocol(message: Message, state: FSMContext, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    text = message.text.lower()
    protocol = "awg" if "awg" in text else "wg"
    data = await state.get_data()

    try:
        # Добавляем с модерацией (pending_approval=True)
        await db.add_device(
            chat_id=str(message.from_user.id),
            device_name=data["device_name"],
            protocol=protocol,
            pending=True,
        )
        await message.answer(
            f"✅ Запрос на устройство `{data['device_name']}` отправлен администратору.\n"
            f"Вы получите конфигурацию после одобрения.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=ReplyKeyboardRemove())
    finally:
        await state.clear()


# ---------------------------------------------------------------------------
# /request vpn|direct <домен>
# ---------------------------------------------------------------------------
@router.message(Command("request"))
async def cmd_request(message: Message, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    if not client:
        await message.answer("Сначала зарегистрируйтесь: /start")
        return

    args = message.text.split()
    if len(args) < 3 or args[1] not in ("vpn", "direct"):
        await message.answer(
            "Использование: `/request vpn|direct <домен>`\n\n"
            "Пример: `/request vpn example.com`"
        )
        return

    direction, domain = args[1], args[2].lower().strip(".")
    req_id = await db.create_domain_request(str(message.from_user.id), domain, direction)

    await message.answer(
        f"✅ Запрос #{req_id} отправлен администратору.\n"
        f"Домен: `{domain}`\n"
        f"Направление: {direction}"
    )


# ---------------------------------------------------------------------------
# /status (клиентский)
# ---------------------------------------------------------------------------
@router.message(Command("status"))
async def cmd_status_client(message: Message, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)
    if not client:
        return

    # Только базовый статус туннеля
    from services.watchdog_client import WatchdogClient
    wc = WatchdogClient(config.watchdog_url, config.watchdog_token)
    try:
        status = await wc.get_status()
        ok = status.get("status") == "ok"
        stack = status.get("active_stack", "N/A")
        await message.answer(
            f"{'✅ VPN работает' if ok else '⚠️ VPN деградирован'}\n"
            f"Текущий протокол: `{stack}`"
        )
    except Exception:
        await message.answer("❌ Не удалось получить статус")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
@router.message(Command("help"))
async def cmd_help(message: Message, **kwargs):
    db, client = await get_db_and_client(message, **kwargs)

    if not client:
        await message.answer(
            "Для использования бота нужна регистрация.\n"
            "Запросите код приглашения у администратора: /start"
        )
        return

    await message.answer(
        "*Доступные команды:*\n\n"
        "/start — главная\n"
        "/mydevices — мои устройства\n"
        "/myconfig [имя] — получить конфиг\n"
        "/adddevice — добавить устройство\n"
        "/removedevice — удалить устройство\n"
        "/update — получить обновлённый конфиг\n"
        "/request vpn|direct <домен> — запросить маршрут\n"
        "/myrequests — мои запросы\n"
        "/exclude add|remove|list <подсеть> — исключения\n"
        "/report <текст> — сообщение администратору\n"
        "/status — статус VPN\n"
        "/help — эта справка"
    )


# ---------------------------------------------------------------------------
# Default handler (незарегистрированные — игнор, зарегистрированные — подсказка)
# ---------------------------------------------------------------------------
@router.message()
async def default_handler(message: Message, **kwargs):
    db: Database = kwargs.get("db")
    if not db:
        return

    client = await db.get_client_by_chat_id(str(message.from_user.id))
    if client:
        await message.answer("Неизвестная команда. /help")
    # Незарегистрированным — игнор

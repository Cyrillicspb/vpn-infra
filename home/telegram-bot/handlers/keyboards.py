"""
handlers/keyboards.py — Централизованные определения клавиатур

Admin: /menu → категории → подменю → действия
Client: /start → инлайн-меню; выбор протокола; выбор устройства
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# ── Администратор: главное меню ───────────────────────────────────────────────

def admin_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Мониторинг",   callback_data="adm:monitor"),
            InlineKeyboardButton(text="⚙️ Управление",   callback_data="adm:manage"),
        ],
        [
            InlineKeyboardButton(text="🌐 Маршруты",     callback_data="adm:routes"),
            InlineKeyboardButton(text="👥 Клиенты",      callback_data="adm:clients"),
        ],
        [
            InlineKeyboardButton(text="🖥️ VPS",          callback_data="adm:vps"),
            InlineKeyboardButton(text="🔐 Безопасность", callback_data="adm:security"),
        ],
    ])


# ── Администратор: подменю ────────────────────────────────────────────────────

def admin_monitor_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Статус",   callback_data="adm:status"),
            InlineKeyboardButton(text="🔗 Туннель",  callback_data="adm:tunnel"),
        ],
        [
            InlineKeyboardButton(text="🌍 IP",       callback_data="adm:ip"),
            InlineKeyboardButton(text="🐳 Docker",   callback_data="adm:docker"),
        ],
        [
            InlineKeyboardButton(text="⚡ Метрики",  callback_data="adm:speed"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",    callback_data="adm:menu"),
        ],
    ])


def admin_manage_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Сменить стек",        callback_data="adm:switch_menu"),
            InlineKeyboardButton(text="🔃 Перезапуск",          callback_data="adm:restart_menu"),
        ],
        [
            InlineKeyboardButton(text="⬆️ Обновить",            callback_data="adm:update"),
            InlineKeyboardButton(text="🚀 Deploy",              callback_data="adm:deploy"),
            InlineKeyboardButton(text="⏮️ Откат",               callback_data="adm:rollback"),
        ],
        [
            InlineKeyboardButton(text="🔌 Перезагрузить сервер", callback_data="adm:reboot"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",                callback_data="adm:menu"),
        ],
    ])


def admin_switch_menu() -> InlineKeyboardMarkup:
    stacks = [
        ("☁️ Cloudflare CDN", "cloudflare-cdn"),
        ("🔒 REALITY + gRPC",  "reality-grpc"),
        ("🔒 REALITY",         "reality"),
        ("⚡ Hysteria2",        "hysteria2"),
    ]
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"adm:sw:{key}")]
        for name, key in stacks
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_restart_menu() -> InlineKeyboardMarkup:
    services = [
        ("dnsmasq",   "dnsmasq"),
        ("watchdog",  "watchdog"),
        ("hysteria2", "hysteria2"),
        ("docker",    "docker"),
        ("wg0",       "wg-quick@wg0"),
        ("wg1",       "wg-quick@wg1"),
        ("nftables",  "nftables"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for name, svc in services:
        row.append(InlineKeyboardButton(text=name, callback_data=f"adm:rs:{svc}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_routes_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список VPN",    callback_data="adm:list_vpn"),
            InlineKeyboardButton(text="📋 Список Direct", callback_data="adm:list_direct"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить маршруты", callback_data="adm:routes_update"),
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ Как добавить домен",
                callback_data="adm:routes_info",
            ),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_clients_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎫 Создать инвайт",      callback_data="adm:invite"),
            InlineKeyboardButton(text="👥 Все клиенты",         callback_data="adm:clients_list"),
        ],
        [
            InlineKeyboardButton(text="📋 Запросы на модерацию", callback_data="adm:requests"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_vps_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список VPS",    callback_data="adm:vps_list"),
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ Добавить / удалить VPS",
                callback_data="adm:vps_info",
            ),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_security_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔑 Ротация ключей",   callback_data="adm:rotate_keys"),
            InlineKeyboardButton(text="📜 Сертификат mTLS",  callback_data="adm:renew_cert"),
        ],
        [
            InlineKeyboardButton(text="🏛️ Обновить CA",      callback_data="adm:renew_ca"),
        ],
        [
            InlineKeyboardButton(
                text="🔍 Диагностика устройства",
                callback_data="adm:diagnose_info",
            ),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def back_to_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu"),
    ]])


# ── Клиент: главное меню ──────────────────────────────────────────────────────

def client_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Мои устройства",    callback_data="cl:mydevices"),
            InlineKeyboardButton(text="📋 Получить конфиг",   callback_data="cl:myconfig"),
        ],
        [
            InlineKeyboardButton(text="➕ Добавить устройство", callback_data="cl:adddevice"),
            InlineKeyboardButton(text="🔄 Обновить конфиги",   callback_data="cl:update"),
        ],
        [
            InlineKeyboardButton(text="📊 Статус VPN",   callback_data="cl:status"),
            InlineKeyboardButton(text="📝 Мои запросы",  callback_data="cl:myrequests"),
        ],
        [
            InlineKeyboardButton(text="🆘 Помощь", callback_data="cl:help"),
        ],
    ])


# ── Клиент: выбор протокола (инлайн) ─────────────────────────────────────────

def proto_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛡️ AmneziaWG (рекомендуется)",
            callback_data="proto:awg",
        ),
        InlineKeyboardButton(text="🔒 WireGuard", callback_data="proto:wg"),
    ]])


# ── Клиент: выбор устройства (инлайн) ────────────────────────────────────────

def devices_inline_kb(devices: list, prefix: str) -> InlineKeyboardMarkup:
    """Кнопка на каждое устройство; callback = prefix + device_id."""
    rows = []
    for d in devices:
        icon = "✅" if not d.get("pending_approval") else "⏳"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {d['device_name']} ({d['protocol'].upper()})",
            callback_data=f"{prefix}{d['id']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

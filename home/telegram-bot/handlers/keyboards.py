"""
handlers/keyboards.py — Централизованные определения клавиатур

Admin: /menu → категории → подменю → действия (всё через кнопки)
Client: /start → инлайн-меню; выбор протокола; выбор устройства
Persistent: menu_reply_kb() — постоянная кнопка «📋 Меню» в чате
"""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


# ── Постоянная кнопка «Меню» ─────────────────────────────────────────────────

def menu_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура с одной кнопкой — вызов меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Меню")]],
        resize_keyboard=True,
        persistent=True,
    )


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


# ── Администратор: мониторинг ─────────────────────────────────────────────────

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
            InlineKeyboardButton(text="📉 Графики",  callback_data="adm:graph_menu"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",    callback_data="adm:menu"),
        ],
    ])


def admin_graph_menu() -> InlineKeyboardMarkup:
    panels = [
        ("🔗 Туннель",   "tunnel"),
        ("⚡ Скорость",  "speed"),
        ("👥 Клиенты",   "clients"),
        ("🖥️ Система",   "system"),
    ]
    rows = [[InlineKeyboardButton(text=name, callback_data=f"adm:gr:{key}")]
            for name, key in panels]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:monitor")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: управление ─────────────────────────────────────────────────

def admin_manage_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Сменить стек",    callback_data="adm:switch_menu"),
            InlineKeyboardButton(text="🔃 Перезапуск",      callback_data="adm:restart_menu"),
        ],
        [
            InlineKeyboardButton(text="📋 Логи",            callback_data="adm:logs_menu"),
            InlineKeyboardButton(text="📢 Рассылка",        callback_data="adm:broadcast"),
        ],
        [
            InlineKeyboardButton(text="⬆️ Обновить",        callback_data="adm:update"),
            InlineKeyboardButton(text="🚀 Deploy",          callback_data="adm:deploy"),
            InlineKeyboardButton(text="⏮️ Откат",           callback_data="adm:rollback"),
        ],
        [
            InlineKeyboardButton(text="🔌 Перезагрузить сервер", callback_data="adm:reboot"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
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


def admin_logs_menu() -> InlineKeyboardMarkup:
    services = [
        ("telegram-bot",  "telegram-bot"),
        ("watchdog",      "watchdog"),
        ("dnsmasq",       "dnsmasq"),
        ("hysteria2",     "hysteria2"),
        ("xray-client",   "xray-client"),
        ("xray-client-2", "xray-client-2"),
        ("cloudflared",   "cloudflared"),
        ("node-exporter", "node-exporter"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for name, svc in services:
        row.append(InlineKeyboardButton(text=name, callback_data=f"adm:log:{svc}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: маршруты ───────────────────────────────────────────────────

def admin_routes_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить VPN",    callback_data="adm:vpn_add"),
            InlineKeyboardButton(text="➕ Добавить Direct", callback_data="adm:direct_add"),
        ],
        [
            InlineKeyboardButton(text="➖ Удалить VPN",     callback_data="adm:vpn_remove"),
            InlineKeyboardButton(text="➖ Удалить Direct",  callback_data="adm:direct_remove"),
        ],
        [
            InlineKeyboardButton(text="🔍 Проверить домен", callback_data="adm:check"),
        ],
        [
            InlineKeyboardButton(text="📋 Список VPN",      callback_data="adm:list_vpn"),
            InlineKeyboardButton(text="📋 Список Direct",   callback_data="adm:list_direct"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить маршруты", callback_data="adm:routes_update"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def domains_inline_kb(domains: list[str], prefix: str, back: str) -> InlineKeyboardMarkup:
    """Список доменов как кнопки для удаления."""
    rows = [
        [InlineKeyboardButton(text=f"❌ {d}", callback_data=f"{prefix}{d[:40]}")]
        for d in domains[:30]
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: клиенты ────────────────────────────────────────────────────

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


def admin_clients_list_kb(clients: list[dict]) -> InlineKeyboardMarkup:
    """Список клиентов как кнопки."""
    rows = []
    for c in clients:
        icon = "🚫" if c.get("is_disabled") else ("👑" if c.get("is_admin") else "✅")
        name = c.get("username") or c["chat_id"]
        rows.append([InlineKeyboardButton(
            text=f"{icon} {name}",
            callback_data=f"adm:cl:{c['chat_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:clients")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_client_actions_kb(chat_id: str, is_disabled: bool) -> InlineKeyboardMarkup:
    """Действия с конкретным клиентом."""
    toggle = ("✅ Включить", f"adm:cl_en:{chat_id}") if is_disabled else ("🚫 Отключить", f"adm:cl_dis:{chat_id}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle[0], callback_data=toggle[1])],
        [
            InlineKeyboardButton(text="🔢 Лимит устройств", callback_data=f"adm:cl_lim:{chat_id}"),
            InlineKeyboardButton(text="🦵 Кик",             callback_data=f"adm:cl_kick:{chat_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:clients_list")],
    ])


# ── Администратор: VPS ────────────────────────────────────────────────────────

def admin_vps_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список VPS",  callback_data="adm:vps_list"),
            InlineKeyboardButton(text="➕ Добавить VPS", callback_data="adm:vps_add"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_vps_list_kb(vps_list: list[dict], active_idx: int) -> InlineKeyboardMarkup:
    rows = []
    for i, v in enumerate(vps_list):
        icon = "✅" if i == active_idx else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {v['ip']}:{v.get('ssh_port', 22)}  ❌ удалить",
            callback_data=f"adm:vps_rm:{v['ip']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:vps")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: безопасность ───────────────────────────────────────────────

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
            InlineKeyboardButton(text="🔍 Диагностика",      callback_data="adm:diagnose_menu"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_diagnose_kb(devices: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"🔍 {d['device_name']} ({d['protocol'].upper()})",
            callback_data=f"adm:diag:{d['device_name'][:30]}",
        )]
        for d in devices[:20]
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:security")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            InlineKeyboardButton(text="➕ Добавить устройство",  callback_data="cl:adddevice"),
            InlineKeyboardButton(text="➖ Удалить устройство",   callback_data="cl:removedevice"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить конфиги",    callback_data="cl:update"),
            InlineKeyboardButton(text="📊 Статус VPN",          callback_data="cl:status"),
        ],
        [
            InlineKeyboardButton(text="🌐 Запросить маршрут",   callback_data="cl:request"),
            InlineKeyboardButton(text="📝 Мои запросы",         callback_data="cl:myrequests"),
        ],
        [
            InlineKeyboardButton(text="🚫 Исключения",          callback_data="cl:excludes"),
            InlineKeyboardButton(text="🆘 Сообщить о проблеме", callback_data="cl:report"),
        ],
    ])


# ── Клиент: запрос маршрута ───────────────────────────────────────────────────

def client_request_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Через VPN",    callback_data="cl:req:vpn"),
            InlineKeyboardButton(text="🌐 Напрямую",     callback_data="cl:req:direct"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="cl:menu")],
    ])


# ── Клиент: исключения ────────────────────────────────────────────────────────

def client_excludes_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список",         callback_data="cl:ex_list"),
            InlineKeyboardButton(text="➕ Добавить",        callback_data="cl:ex_add"),
        ],
        [
            InlineKeyboardButton(text="➖ Удалить",         callback_data="cl:ex_remove"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="cl:menu")],
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

def devices_inline_kb(devices: list, prefix: str, back: str = "cl:menu") -> InlineKeyboardMarkup:
    """Кнопка на каждое устройство; callback = prefix + device_id."""
    rows = []
    for d in devices:
        icon = "✅" if not d.get("pending_approval") else "⏳"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {d['device_name']} ({d['protocol'].upper()})",
            callback_data=f"{prefix}{d['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def excludes_inline_kb(excludes: list[dict], device_id: int) -> InlineKeyboardMarkup:
    """Список исключений с кнопками удаления."""
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {e['subnet']}",
            callback_data=f"cl:ex_del:{device_id}:{e['subnet'][:30]}",
        )]
        for e in excludes[:20]
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cl:excludes")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

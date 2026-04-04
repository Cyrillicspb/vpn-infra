"""
handlers/keyboards.py — Централизованные определения клавиатур

Admin: /menu → категории → подменю → действия (всё через кнопки)
Client: /start → инлайн-меню; выбор протокола; выбор устройства
Persistent: menu_reply_kb() — постоянная кнопка «📋 Меню» в чате
"""
import re

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

_RE_VISIBLE = re.compile(r'\w', re.UNICODE)


def _visible(s: str) -> str:
    """Вернуть s если содержит видимые символы (буквы/цифры), иначе ''."""
    v = (s or "").strip()
    return v if _RE_VISIBLE.search(v) else ""


def _nav_row(back_cb: str, home_cb: str = "adm:menu") -> list[InlineKeyboardButton]:
    """Строка навигации для экранов уровня 3+: ◀️ Назад + 🏠 Меню."""
    return [
        InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Меню",  callback_data=home_cb),
    ]


def confirm_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    """Универсальная клавиатура подтверждения деструктивного действия."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да",     callback_data=yes_cb),
        InlineKeyboardButton(text="❌ Отмена", callback_data=no_cb),
    ]])


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
            InlineKeyboardButton(text="🏠 Дашборд", callback_data="adm:dashboard"),
        ],
        [
            InlineKeyboardButton(text="📡 Туннель",      callback_data="adm:tunnel_menu"),
            InlineKeyboardButton(text="👥 Клиенты",      callback_data="adm:clients"),
        ],
        [
            InlineKeyboardButton(text="🌐 Маршруты",     callback_data="adm:routes"),
            InlineKeyboardButton(text="🔧 Система",      callback_data="adm:system"),
        ],
        [
            InlineKeyboardButton(text="📊 Мониторинг",   callback_data="adm:monitor"),
            InlineKeyboardButton(text="👤 Меню пользователя", callback_data="adm:user_menu"),
        ],
        [
            InlineKeyboardButton(text="🏘 Gateway", callback_data="adm:gateway"),
        ],
    ])


# ── Администратор: туннель ────────────────────────────────────────────────────

def admin_tunnel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔗 Стеки",           callback_data="adm:tunnel"),
            InlineKeyboardButton(text="🔄 Сменить стек",    callback_data="adm:switch_menu"),
        ],
        [
            InlineKeyboardButton(text="🔍 Тест стеков",     callback_data="adm:assess"),
            InlineKeyboardButton(text="📋 Журнал ротаций",  callback_data="adm:rotation_log"),
        ],
        [
            InlineKeyboardButton(text="🖥️ Backends",        callback_data="adm:vps"),
            InlineKeyboardButton(text="🧪 DPI experimental", callback_data="adm:dpi"),
        ],
        [
            InlineKeyboardButton(text="🌍 Внешний IP",      callback_data="adm:ip"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",           callback_data="adm:menu"),
        ],
    ])


# ── Администратор: мониторинг ─────────────────────────────────────────────────

def admin_monitor_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📟 Status",         callback_data="adm:status"),
            InlineKeyboardButton(text="❤️ Health",         callback_data="adm:health"),
            InlineKeyboardButton(text="🐳 Docker",          callback_data="adm:docker"),
        ],
        [InlineKeyboardButton(text="🧪 Functional", callback_data="adm:functional")],
        [InlineKeyboardButton(text="📊 Трафик клиентов", callback_data="adm:stats")],
        [
            InlineKeyboardButton(text="⚡ Спидтест",        callback_data="adm:speedtest"),
            InlineKeyboardButton(text="📉 Графики",         callback_data="adm:graph_menu"),
        ],
        [
            InlineKeyboardButton(text="📋 Логи",            callback_data="adm:logs_monitor"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",           callback_data="adm:menu"),
        ],
    ])


def admin_functional_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Active", callback_data="adm:functional:mode:active"),
            InlineKeyboardButton(text="🟡 Staged", callback_data="adm:functional:mode:staged"),
            InlineKeyboardButton(text="⚫ Off", callback_data="adm:functional:mode:off"),
        ],
        [
            InlineKeyboardButton(text="🔄 Quick", callback_data="adm:functional:run:quick"),
            InlineKeyboardButton(text="🔄 Standard", callback_data="adm:functional:run:standard"),
            InlineKeyboardButton(text="🔄 Deep", callback_data="adm:functional:run:deep"),
        ],
        [
            InlineKeyboardButton(text="↻ Обновить", callback_data="adm:functional"),
        ],
        _nav_row("adm:monitor"),
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
    rows.append(_nav_row("adm:monitor"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: система (объединяет управление + безопасность) ─────────────

def admin_system_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Применить апдейт", callback_data="adm:deploy"),
            InlineKeyboardButton(text="⏮️ Откат",           callback_data="adm:rollback"),
        ],
        [
            InlineKeyboardButton(text="💾 Бэкап",           callback_data="adm:backup"),
            InlineKeyboardButton(text="🗂 Полный экспорт",  callback_data="adm:backup_export"),
        ],
        [
            InlineKeyboardButton(text="⬆️ Обновить Docker", callback_data="adm:update"),
        ],
        [
            InlineKeyboardButton(text="🔃 Перезапуск",      callback_data="adm:restart_menu"),
            InlineKeyboardButton(text="📋 Логи",            callback_data="adm:logs_system"),
        ],
        [
            InlineKeyboardButton(text="📜 Сертификат mTLS", callback_data="adm:renew_cert"),
        ],
        [
            InlineKeyboardButton(text="🏛️ Обновить CA",     callback_data="adm:renew_ca"),
        ],
        [
            InlineKeyboardButton(text="🛡️ Fail2ban",        callback_data="adm:fail2ban"),
        ],
        [
            InlineKeyboardButton(text="⚠️ Перезагрузить сервер", callback_data="adm:reboot"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_manage_menu() -> InlineKeyboardMarkup:
    """Обратная совместимость — возвращает admin_system_menu()."""
    return admin_system_menu()


def admin_switch_menu(active_stack: str = "") -> InlineKeyboardMarkup:
    stacks = [
        ("🔐 Trojan (exp)", "trojan"),
        ("☁️ Cloudflare CDN", "cloudflare-cdn"),
        ("🛡️ REALITY + Vision", "vless-reality-vision"),
        ("🚀 TUIC (exp)", "tuic"),
        ("🧪 REALITY + XHTTP (exp)", "reality-xhttp"),
        ("⚡ Hysteria2",         "hysteria2"),
    ]
    rows = []
    for name, key in stacks:
        prefix = "✅ " if active_stack and key == active_stack else ""
        rows.append([InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"adm:sw:{key}")])
    rows.append(_nav_row("adm:tunnel_menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_restart_menu() -> InlineKeyboardMarkup:
    services = [
        ("dnsmasq",   "dnsmasq"),
        ("watchdog",  "watchdog"),
        ("hysteria2", "hysteria2"),
        ("docker",    "docker"),
        ("wg0",       "awg-quick@wg0"),
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
    rows.append(_nav_row("adm:system"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_logs_menu(
    back_cb: str = "adm:system",
    prefix: str = "adm:log:system:",
) -> InlineKeyboardMarkup:
    services = [
        ("telegram-bot",  "telegram-bot"),
        ("watchdog",      "watchdog"),
        ("dnsmasq",       "dnsmasq"),
        ("hysteria2",     "hysteria2"),
        ("sing-box-tuic", "sing-box-tuic-client"),
        ("sing-box-trojan", "sing-box-trojan-client"),
        ("xray-client-vision", "xray-client-vision"),
        ("xray-client-xhttp", "xray-client-xhttp"),
        ("cloudflared",   "cloudflared"),
        ("node-exporter", "node-exporter"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for name, svc in services:
        row.append(InlineKeyboardButton(text=name, callback_data=f"{prefix}{svc}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_nav_row(back_cb))
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
            InlineKeyboardButton(text="📄 Список VPN",      callback_data="adm:list_vpn"),
            InlineKeyboardButton(text="📄 Список Direct",   callback_data="adm:list_direct"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить маршруты", callback_data="adm:routes_update"),
        ],
        [
            InlineKeyboardButton(text="🧠 Latency state", callback_data="adm:latency_learning"),
        ],
        [
            InlineKeyboardButton(text="🧪 DPI experimental", callback_data="adm:dpi"),
            InlineKeyboardButton(text="📊 Наборы IP",       callback_data="adm:nft_stats"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


# ── Администратор: DPI bypass (zapret lane) ───────────────────────────────────

def admin_dpi_menu(enabled: bool, services: list[dict]) -> InlineKeyboardMarkup:
    """Динамическое меню DPI bypass с текущим статусом."""
    rows: list[list[InlineKeyboardButton]] = []

    # Глобальный вкл/выкл
    if enabled:
        rows.append([InlineKeyboardButton(text="❌ Выключить DPI experimental", callback_data="adm:dpi_off")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Включить DPI experimental",  callback_data="adm:dpi_on")])

    # Пресеты (добавить если нет)
    preset_names = {s["name"] for s in services}
    presets = [("🎬 YouTube", "youtube")]
    add_row = []
    for label, name in presets:
        if name not in preset_names:
            add_row.append(InlineKeyboardButton(
                text=f"➕ {label}", callback_data=f"adm:dpi_add:{name}",
            ))
    if add_row:
        rows.append(add_row)

    # Переключатели для существующих сервисов
    for svc in services[:10]:
        icon = "✅" if svc.get("enabled", True) else "❌"
        display = svc.get("display") or svc["name"]
        rows.append([InlineKeyboardButton(
            text=f"{icon} {display}",
            callback_data=f"adm:dpi_tog:{svc['name'][:20]}",
        )])

    rows.append([InlineKeyboardButton(text="🧪 Тест DPI", callback_data="adm:dpi_test")])
    rows.append([
        InlineKeyboardButton(text="🔄 Пересобрать пресет", callback_data="adm:dpi_recheck"),
        InlineKeyboardButton(text="📋 История",            callback_data="adm:dpi_history"),
    ])
    rows.append(_nav_row("adm:routes"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def domains_inline_kb(domains: list[str], prefix: str, back: str) -> InlineKeyboardMarkup:
    """Список доменов как кнопки для удаления."""
    rows = [
        [InlineKeyboardButton(text=f"❌ {d}", callback_data=f"{prefix}{d[:40]}")]
        for d in domains[:30]
    ]
    rows.append(_nav_row(back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Администратор: клиенты ────────────────────────────────────────────────────

def admin_clients_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Все клиенты",         callback_data="adm:clients_list"),
            InlineKeyboardButton(text="🎫 Создать инвайт",      callback_data="adm:invite"),
        ],
        [
            InlineKeyboardButton(text="📨 Запросы",              callback_data="adm:requests"),
            InlineKeyboardButton(text="📤 Разослать конфиги",    callback_data="adm:broadcast_configs"),
        ],
        [
            InlineKeyboardButton(text="🩺 Диагностика",          callback_data="adm:diagnose_menu"),
        ],
        [
            InlineKeyboardButton(text="📣 Рассылка",            callback_data="adm:broadcast"),
        ],
        [
            InlineKeyboardButton(text="👥 Администраторы",       callback_data="adm:admin_list"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="adm:menu"),
        ],
    ])


def admin_admins_menu(admins: list[dict], is_root: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_root:
        rows.append([InlineKeyboardButton(text="➕ Создать admin-инвайт", callback_data="adm:admin_invite")])
    for admin in admins:
        name = _visible(admin.get("first_name", "")) or _visible(admin.get("username", "")) or str(admin["chat_id"])
        label = f"👤 {name}"
        if admin.get("username"):
            label = f"👤 @{admin['username']}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm:admin:{admin['chat_id']}")])
    rows.append(_nav_row("adm:clients"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_admin_actions_kb(chat_id: str, can_demote: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔢 Лимит устройств", callback_data=f"adm:cl_lim:{chat_id}"),
            InlineKeyboardButton(text="🔄 Реконнект",       callback_data=f"adm:cl_reconnect:{chat_id}"),
        ],
    ]
    if can_demote:
        rows.append([InlineKeyboardButton(text="➖ Снять права администратора", callback_data=f"adm:admin_rm:{chat_id}")])
    rows.append(_nav_row("adm:admin_list", home_cb="adm:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_clients_list_kb(clients: list[dict]) -> InlineKeyboardMarkup:
    """Список клиентов как кнопки."""
    rows = []
    for c in clients:
        icon = "🚫" if c.get("is_disabled") else ("👑" if c.get("is_admin") else "✅")
        name = _visible(c.get("first_name", "")) or _visible(c.get("username", "")) or c["chat_id"]
        rows.append([InlineKeyboardButton(
            text=f"{icon} {name}",
            callback_data=f"adm:cl:{c['chat_id']}",
        )])
    rows.append(_nav_row("adm:clients"))
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
        [InlineKeyboardButton(text="🔄 Реконнект", callback_data=f"adm:cl_reconnect:{chat_id}")],
        _nav_row("adm:clients_list"),
    ])


# ── Администратор: VPS ────────────────────────────────────────────────────────

def admin_vps_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Backend pool", callback_data="adm:vps_list"),
            InlineKeyboardButton(text="➕ Добавить backend", callback_data="adm:vps_add"),
        ],
        [
            InlineKeyboardButton(text="⚖️ Balancer", callback_data="adm:balancer"),
            InlineKeyboardButton(text="🧠 Auto-select", callback_data="adm:backend_auto"),
        ],
        [
            InlineKeyboardButton(text="🧭 Client backend prefs", callback_data="adm:backend_prefs"),
        ],
        _nav_row("adm:tunnel_menu"),
    ])


def admin_backend_prefs_kb(prefs: list[dict] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Add client pref", callback_data="adm:backend_pref_add")],
    ]
    for pref in (prefs or [])[:12]:
        rows.append([
            InlineKeyboardButton(
                text=f"❌ #{pref.get('id')} {pref.get('chat_id')} {pref.get('match_type')}={pref.get('match_value')}",
                callback_data=f"adm:backend_pref_rm:{pref.get('id')}",
            )
        ])
    rows.append(_nav_row("adm:vps"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_gateway_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖧 LAN clients", callback_data="adm:lan_clients")],
        [InlineKeyboardButton(text="🧭 LAN backend prefs", callback_data="adm:lan_prefs")],
        _nav_row("adm:menu"),
    ])


def admin_vps_list_kb(vps_list: list[dict], active_idx: int) -> InlineKeyboardMarkup:
    rows = []
    for i, v in enumerate(vps_list):
        if v.get("drain"):
            icon = "🟡"
        elif v.get("status") == "healthy":
            icon = "🟢"
        elif v.get("status") == "degraded":
            icon = "🟠"
        elif v.get("status") == "down":
            icon = "🔴"
        else:
            icon = "✅" if i == active_idx else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {v['ip']}:{v.get('ssh_port', 22)}",
            callback_data=f"adm:vps_detail:{v['ip']}",
        )])
    rows.append(_nav_row("adm:vps"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_vps_actions_kb(ip: str, ssh_port: int = 22, backend_id: str = "", active: bool = False, drain: bool = False) -> InlineKeyboardMarkup:
    """Действия с конкретным VPS."""
    switch_label = "✅ Активный backend" if active else "🎯 Сделать активным"
    drain_label = "🟢 Снять drain" if drain else "🟡 Drain"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Тест соединения",     callback_data=f"adm:vps_test:{ip}")],
        [InlineKeyboardButton(text=switch_label,              callback_data=f"adm:vps_set_active:{backend_id or ip}")],
        [InlineKeyboardButton(text=drain_label,               callback_data=f"adm:vps_drain_toggle:{backend_id or ip}")],
        [InlineKeyboardButton(text="🔄 Мигрировать на этот", callback_data=f"adm:vps_migrate:{ip}")],
        [InlineKeyboardButton(text="❌ Удалить backend",      callback_data=f"adm:vps_rm:{ip}")],
        _nav_row("adm:vps_list"),
    ])


# ── Администратор: безопасность ───────────────────────────────────────────────

def admin_security_menu() -> InlineKeyboardMarkup:
    """Обратная совместимость — возвращает admin_system_menu()."""
    return admin_system_menu()


def admin_diagnose_kb(devices: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"🔍 {d.get('owner_name', '?')} / {d['device_name']} ({d['protocol'].upper()})",
            callback_data=f"adm:diag:{d['device_name'][:30]}",
        )]
        for d in devices[:20]
    ]
    rows.append(_nav_row("adm:clients"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ В меню", callback_data="adm:menu"),
    ]])


# ── Клиент: главное меню ──────────────────────────────────────────────────────

def client_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Устройства",          callback_data="cl:mydevices"),
            InlineKeyboardButton(text="➕ Добавить устройство",  callback_data="cl:adddevice"),
        ],
        [
            InlineKeyboardButton(text="📥 Получить конфиг",     callback_data="cl:myconfig"),
            InlineKeyboardButton(text="🔄 Обновить конфиги",    callback_data="cl:update"),
        ],
        [
            InlineKeyboardButton(text="🌐 Сайты через VPN",     callback_data="cl:sites"),
        ],
        [
            InlineKeyboardButton(text="🔍 Не работает сайт?",   callback_data="cl:checksite"),
            InlineKeyboardButton(text="🚫 Исключения",           callback_data="cl:excludes"),
        ],
        [
            InlineKeyboardButton(text="📍 Через сервер",         callback_data="cl:sroutes"),
        ],
        [
            InlineKeyboardButton(text="📶 Статус VPN",          callback_data="cl:status"),
            InlineKeyboardButton(text="ℹ️ Помощь",              callback_data="cl:help"),
        ],
        [
            InlineKeyboardButton(text="🆘 Сообщить о проблеме", callback_data="cl:report"),
        ],
    ])


# ── Клиент: сайты через VPN ───────────────────────────────────────────────────

def client_sites_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Запросить сайт через VPN", callback_data="cl:request")],
        [InlineKeyboardButton(text="📋 Мои запросы",              callback_data="cl:myrequests")],
        [InlineKeyboardButton(text="◀️ Назад",                    callback_data="cl:menu")],
    ])


# ── Клиент: запрос маршрута ───────────────────────────────────────────────────

def client_request_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Через VPN",    callback_data="cl:req:vpn"),
            InlineKeyboardButton(text="🌐 Напрямую",     callback_data="cl:req:direct"),
        ],
        _nav_row("cl:sites", home_cb="cl:menu"),
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


def client_server_routes_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список",         callback_data="cl:sr_list"),
            InlineKeyboardButton(text="➕ Добавить",        callback_data="cl:sr_add"),
        ],
        [
            InlineKeyboardButton(text="➖ Удалить",         callback_data="cl:sr_remove"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="cl:menu")],
    ])


# ── Клиент: выбор протокола (инлайн) ─────────────────────────────────────────

def proto_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛡️ AmneziaWG — телефон/ноутбук",
            callback_data="proto:awg",
        )],
        [InlineKeyboardButton(
            text="🔒 WireGuard — телефон/ноутбук",
            callback_data="proto:wg",
        )],
        [InlineKeyboardButton(
            text="🖥️ WireGuard — роутер",
            callback_data="proto:wg_router",
        )],
    ])


# ── Клиент: выбор устройства (инлайн) ────────────────────────────────────────

def devices_inline_kb(
    devices: list,
    prefix: str,
    back: str = "cl:menu",
    footer: list | None = None,
) -> InlineKeyboardMarkup:
    """Кнопка на каждое устройство; callback = prefix + device_id."""
    rows = []
    for d in devices:
        icon = "✅" if not d.get("pending_approval") else "⏳"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {d['device_name']} ({d['protocol'].upper()})",
            callback_data=f"{prefix}{d['id']}",
        )])
    if footer:
        rows.append(footer)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_detail_kb(device_id: int) -> InlineKeyboardMarkup:
    """Действия с конкретным устройством клиента."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Получить конфиг",    callback_data=f"cl:getconf:{device_id}")],
        [InlineKeyboardButton(text="🔄 Обновить конфиг",    callback_data=f"cl:upd1:{device_id}")],
        [InlineKeyboardButton(text="🚫 Исключения",         callback_data=f"cl:devex:{device_id}")],
        [InlineKeyboardButton(text="📍 Маршруты через сервер", callback_data=f"cl:devsr:{device_id}")],
        [InlineKeyboardButton(text="🗑 Удалить устройство", callback_data=f"cl:del:{device_id}")],
        _nav_row("cl:mydevices", home_cb="cl:menu"),
    ])


def platform_inline_kb(device_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора платформы при получении конфига."""
    platforms = [
        ("📱 iOS / Android", "ios"),
        ("💻 Стандартный .conf", "conf"),
        ("🪟 Windows (.ps1)", "windows"),
        ("🍎 macOS (.command)", "macos"),
        ("🐧 Linux (.sh)", "linux"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"cfgp:{device_id}:{platform}")]
        for label, platform in platforms
    ]
    rows.append(_nav_row(f"cl:dev:{device_id}", home_cb="cl:menu"))
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
    rows.append(_nav_row("cl:excludes", home_cb="cl:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_excludes_menu(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список", callback_data=f"cl:devex_list:{device_id}"),
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"cl:devex_add:{device_id}"),
        ],
        [
            InlineKeyboardButton(text="➖ Удалить", callback_data=f"cl:devex_remove:{device_id}"),
        ],
        _nav_row(f"cl:dev:{device_id}", home_cb="cl:menu"),
    ])


def device_excludes_inline_kb(excludes: list[dict], device_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {item['subnet']}",
            callback_data=f"cl:devex_del:{device_id}:{item['subnet'][:30]}",
        )]
        for item in excludes[:20]
    ]
    rows.append(_nav_row(f"cl:devex:{device_id}", home_cb=f"cl:dev:{device_id}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def server_routes_inline_kb(routes: list[dict], device_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {route['subnet']}",
            callback_data=f"cl:sr_del:{device_id}:{route['subnet'][:30]}",
        )]
        for route in routes[:20]
    ]
    rows.append(_nav_row("cl:sroutes", home_cb="cl:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_server_routes_menu(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список", callback_data=f"cl:devsr_list:{device_id}"),
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"cl:devsr_add:{device_id}"),
        ],
        [
            InlineKeyboardButton(text="➖ Удалить", callback_data=f"cl:devsr_remove:{device_id}"),
        ],
        _nav_row(f"cl:dev:{device_id}", home_cb="cl:menu"),
    ])


def device_server_routes_inline_kb(routes: list[dict], device_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {route['subnet']}",
            callback_data=f"cl:devsr_del:{device_id}:{route['subnet'][:30]}",
        )]
        for route in routes[:20]
    ]
    rows.append(_nav_row(f"cl:devsr:{device_id}", home_cb=f"cl:dev:{device_id}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)

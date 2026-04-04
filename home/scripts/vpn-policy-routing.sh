#!/bin/bash
# vpn-policy-routing.sh — Policy routing для гибрида B+
#
# Создаёт две routing tables и ip rules:
#   table 200 (marked): fwmark 0x1 → заблокированный трафик → tun (VPS)
#   table 100 (vpn):    VPN-клиенты без fwmark → незаблокированное → eth0 (прямой)
#
# Запускается как vpn-routes.service (After=awg-quick@wg0 wg-quick@wg1)
# При смене активного tun: watchdog вызывает этот скрипт повторно с аргументом TUN_IFACE
#
# Использование:
#   vpn-policy-routing.sh [up|down] [tun-интерфейс]
#   По умолчанию: up, интерфейс из /run/vpn-active-tun

set -euo pipefail

# ── Конфигурация ────────────────────────────────────────────────────────────
TABLE_MARKED=200          # fwmark 0x1 → заблокированное → VPS
TABLE_VPN=100             # VPN-клиенты → незаблокированное → eth0
TABLE_DPI=201             # fwmark 0x2 → DPI-throttled → eth0 + zapret
FWMARK=0x1
FWMARK_DPI=0x2

AWG_SUBNET="10.177.1.0/24"
WG_SUBNET="10.177.3.0/24"
TIER2_SUBNET="10.177.2.0/30"
FUNCTIONAL_NS_SUBNET="${FUNCTIONAL_NS_SUBNET:-172.21.0.0/24}"

STATE_FILE="/run/vpn-active-tun"

# Файл с переменными сети (заполняется setup.sh)
ENV_FILE="/opt/vpn/.env"

# ── Загрузка переменных ──────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
fi

# Определяем интерфейс и gateway
ETH_IFACE="${NET_INTERFACE:-$(ip route show default | awk '/default/ {print $5}' | head -1)}"
GATEWAY="${GATEWAY_IP:-$(ip route show default dev "$ETH_IFACE" | awk '/default/ {print $3}' | head -1)}"

ACTION="${1:-up}"
TUN_IFACE=""

# Определяем активный tun-интерфейс
if [[ -n "${2:-}" ]]; then
    TUN_IFACE="$2"
elif [[ -f "$STATE_FILE" ]]; then
    TUN_IFACE="$(cat "$STATE_FILE")"
else
    # Автоопределение: первый активный tun
    TUN_IFACE="$(ip link show | awk '/tun[0-9]/{gsub(":",""); print $2}' | head -1)"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] vpn-policy-routing: $*"
}

# ── Вспомогательные функции ──────────────────────────────────────────────────

rule_exists() {
    ip rule show | grep -q "$1"
}

route_exists() {
    local table="$1"; shift
    ip route show table "$table" | grep -q "$*"
}

# Явные маршруты VPS через eth0 — предотвращают routing loop.
# hysteria2/маркировка могут уводить трафик к VPS не только через main,
# но и через table 200/201, поэтому закрепляем host route во всех управляющих
# таблицах. Иначе SSH control-plane может уйти в tun и повиснуть на banner exchange.
ensure_vps_routes() {
    for vps_ip in ${VPS_IP:-} ${VPS_IP2:-} ${VPS_IP3:-}; do
        [[ -z "$vps_ip" ]] && continue
        ip route replace "$vps_ip" via "$GATEWAY" dev "$ETH_IFACE" metric 1
        ip route replace "$vps_ip" via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_MARKED
        ip route replace "$vps_ip" via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_DPI
        log "VPS route: $vps_ip via $GATEWAY dev $ETH_IFACE (main + table $TABLE_MARKED/$TABLE_DPI, anti-loop)"
    done
}

# ── UP: настройка routing ────────────────────────────────────────────────────
setup_routing() {
    log "Настройка policy routing (tun=$TUN_IFACE, eth=$ETH_IFACE, gw=$GATEWAY)"

    # Проверяем что tun-интерфейс существует
    if [[ -n "$TUN_IFACE" ]] && ! ip link show "$TUN_IFACE" &>/dev/null; then
        log "WARN: tun-интерфейс $TUN_IFACE не существует, routing table 200 без маршрута"
        TUN_IFACE=""
    fi

    # --- Table 200: заблокированный трафик (fwmark 0x1) → tun ---

    # Очищаем старые маршруты в table 200
    ip route flush table $TABLE_MARKED 2>/dev/null || true

    if [[ -n "$TUN_IFACE" ]]; then
        # Маршрут по умолчанию через tun
        ip route replace default dev "$TUN_IFACE" table $TABLE_MARKED
        log "Table $TABLE_MARKED: default dev $TUN_IFACE"
    else
        # Нет активного tun → UNREACHABLE (kill switch через routing)
        ip route replace unreachable default table $TABLE_MARKED
        log "Table $TABLE_MARKED: unreachable (тun не определён)"
    fi

    # --- Table 100: VPN-клиенты (незаблокированное) → eth0 ---

    # Очищаем старые маршруты в table 100
    ip route flush table $TABLE_VPN 2>/dev/null || true

    # Маршрут по умолчанию через основной gateway
    ip route replace default via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_VPN
    log "Table $TABLE_VPN: default via $GATEWAY dev $ETH_IFACE"

    # Локальная сеть — напрямую (replace вместо add: идемпотентно при перезапуске)
    LOCAL_SUBNET="${HOME_SUBNET:-$(ip -4 addr show dev "$ETH_IFACE" | awk '/inet / {print $2}' | head -1 | cut -d'/' -f1 | sed 's/\.[0-9]*$/\.0/')/24}"
    ip route replace "$LOCAL_SUBNET" dev "$ETH_IFACE" table $TABLE_VPN 2>/dev/null || true
    log "Table $TABLE_VPN: $LOCAL_SUBNET dev $ETH_IFACE (прямой)"

    # Functional namespaces живут на локальном bridge br-fh. Без явного маршрута
    # ответы host->namespace по source-based rule from 172.21.0.0/24 уходят через
    # eth0, и DNS внутри vpn-fh-* перестаёт работать.
    if ip link show br-fh &>/dev/null; then
        ip route replace "$FUNCTIONAL_NS_SUBNET" dev br-fh table $TABLE_VPN 2>/dev/null || true
        log "Table $TABLE_VPN: $FUNCTIONAL_NS_SUBNET dev br-fh (functional namespaces)"
    fi

    # --- Table 201: DPI-bypass (fwmark 0x2) → eth0 + zapret ---

    ip route flush table $TABLE_DPI 2>/dev/null || true
    ip route replace default via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_DPI
    log "Table $TABLE_DPI: default via $GATEWAY dev $ETH_IFACE (DPI bypass)"

    # --- ip rules ---

    # Priority 90: fwmark 0x2 → table 201 (DPI bypass → eth0 + zapret)
    if ! rule_exists "fwmark $FWMARK_DPI lookup $TABLE_DPI"; then
        ip rule add fwmark $FWMARK_DPI lookup $TABLE_DPI priority 90
        log "Rule: fwmark $FWMARK_DPI → table $TABLE_DPI (priority 90)"
    fi

    # Priority 100: fwmark 0x1 → table 200 (заблокированное → VPN)
    if ! rule_exists "fwmark $FWMARK lookup $TABLE_MARKED"; then
        ip rule add fwmark $FWMARK lookup $TABLE_MARKED priority 100
        log "Rule: fwmark $FWMARK → table $TABLE_MARKED (priority 100)"
    fi

    # Priority 150: трафик К VPN-подсетям → main (ответы сервера идут через wg0/wg1)
    if ! rule_exists "to $AWG_SUBNET lookup main"; then
        ip rule add to $AWG_SUBNET lookup main priority 150
        log "Rule: to $AWG_SUBNET → main (priority 150)"
    fi
    if ! rule_exists "to $WG_SUBNET lookup main"; then
        ip rule add to $WG_SUBNET lookup main priority 150
        log "Rule: to $WG_SUBNET → main (priority 150)"
    fi

    # Priority 200: AWG-клиенты (незаблокированное) → table 100
    if ! rule_exists "from $AWG_SUBNET lookup $TABLE_VPN"; then
        ip rule add from $AWG_SUBNET lookup $TABLE_VPN priority 200
        log "Rule: from $AWG_SUBNET → table $TABLE_VPN (priority 200)"
    fi

    # Priority 200: WG-клиенты (незаблокированное) → table 100
    if ! rule_exists "from $WG_SUBNET lookup $TABLE_VPN"; then
        ip rule add from $WG_SUBNET lookup $TABLE_VPN priority 200
        log "Rule: from $WG_SUBNET → table $TABLE_VPN (priority 200)"
    fi

    # Gateway Mode: LAN-трафик через таблицу 100 (прямой выход, без VPN)
    # Прямой маршрут до LAN обязателен в table vpn — иначе ответы идут через роутер
    # (hairpin), который их дропает, и входящий SSH/ping пропадает.
    if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${LAN_SUBNET:-}" ]]; then
        # Нормализуем LAN_SUBNET до сетевого адреса (на случай если задан хост-IP)
        LAN_NET=$(python3 -c "import ipaddress; print(str(ipaddress.ip_network('$LAN_SUBNET', strict=False)))" 2>/dev/null || echo "$LAN_SUBNET")
        # Добавляем прямой маршрут до LAN в table vpn (без этого — hairpin через роутер)
        ip route replace "$LAN_NET" dev "$ETH_IFACE" table $TABLE_VPN 2>/dev/null || true
        log "Table $TABLE_VPN: $LAN_NET dev $ETH_IFACE (gateway mode LAN, прямой)"
        if ! rule_exists "from $LAN_NET lookup $TABLE_VPN"; then
            ip rule add from "$LAN_NET" lookup $TABLE_VPN priority 195
            log "Rule: from $LAN_NET → table $TABLE_VPN (priority 195, gateway mode)"
        fi
    fi

    # Synthetic functional-health namespaces → table 100
    if ! rule_exists "from $FUNCTIONAL_NS_SUBNET lookup $TABLE_VPN"; then
        ip rule add from "$FUNCTIONAL_NS_SUBNET" lookup $TABLE_VPN priority 196
        log "Rule: from $FUNCTIONAL_NS_SUBNET → table $TABLE_VPN (priority 196, functional namespaces)"
    fi

    # Защита от routing loop: VPS IP всегда через eth0
    ensure_vps_routes

    # Сохраняем активный tun в state file
    echo "${TUN_IFACE:-}" > "$STATE_FILE"

    log "Policy routing настроен"
}

# ── UPDATE: смена активного tun (вызывается watchdog при failover/rotation) ──
update_tun() {
    local new_tun="${2:-$TUN_IFACE}"

    if [[ -z "$new_tun" ]]; then
        log "ERROR: не указан новый tun-интерфейс"
        exit 1
    fi

    if ! ip link show "$new_tun" &>/dev/null; then
        log "ERROR: интерфейс $new_tun не существует"
        exit 1
    fi

    log "Смена активного tun: $(cat "$STATE_FILE" 2>/dev/null || echo 'none') → $new_tun"

    # Атомарная смена маршрута в table 200
    # ip route replace = replace or add (без окна без маршрута)
    ip route replace default dev "$new_tun" table $TABLE_MARKED
    echo "$new_tun" > "$STATE_FILE"

    # После смены tun повторно закрепляем VPS IP через eth0 —
    # hysteria2 мог добавить host route в main table при старте
    ensure_vps_routes

    log "Table $TABLE_MARKED: default → $new_tun"
}

# ── DOWN: очистка ────────────────────────────────────────────────────────────
teardown_routing() {
    log "Удаление policy routing"

    # Удаляем ip rules
    ip rule del fwmark $FWMARK_DPI lookup $TABLE_DPI 2>/dev/null || true
    ip rule del fwmark $FWMARK lookup $TABLE_MARKED 2>/dev/null || true
    ip rule del to $AWG_SUBNET lookup main 2>/dev/null || true
    ip rule del to $WG_SUBNET lookup main 2>/dev/null || true
    ip rule del from $AWG_SUBNET lookup $TABLE_VPN 2>/dev/null || true
    ip rule del from $WG_SUBNET lookup $TABLE_VPN 2>/dev/null || true

    # Gateway Mode: удаляем LAN-правило
    if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${LAN_SUBNET:-}" ]]; then
        LAN_NET=$(python3 -c "import ipaddress; print(str(ipaddress.ip_network('$LAN_SUBNET', strict=False)))" 2>/dev/null || echo "$LAN_SUBNET")
        ip rule del from "$LAN_NET" lookup $TABLE_VPN priority 195 2>/dev/null || true
        log "Rule: from $LAN_NET → table $TABLE_VPN (priority 195) удалено"
    fi
    ip rule del from "$FUNCTIONAL_NS_SUBNET" lookup $TABLE_VPN priority 196 2>/dev/null || true

    # Очищаем routing tables
    ip route flush table $TABLE_DPI    2>/dev/null || true
    ip route flush table $TABLE_MARKED 2>/dev/null || true
    ip route flush table $TABLE_VPN    2>/dev/null || true

    rm -f "$STATE_FILE"

    log "Policy routing удалён"
}

# ── Main ─────────────────────────────────────────────────────────────────────
case "$ACTION" in
    up)
        setup_routing
        ;;
    update)
        update_tun "$@"
        ;;
    down)
        teardown_routing
        ;;
    status)
        echo "=== ip rules ==="
        ip rule show | grep -E "($TABLE_MARKED|$TABLE_VPN|$FWMARK)"
        echo ""
        echo "=== table $TABLE_DPI (dpi-bypass → eth0) ==="
        ip route show table $TABLE_DPI
        echo ""
        echo "=== table $TABLE_MARKED (blocked → tun) ==="
        ip route show table $TABLE_MARKED
        echo ""
        echo "=== table $TABLE_VPN (vpn → eth0) ==="
        ip route show table $TABLE_VPN
        echo ""
        echo "=== active tun ==="
        cat "$STATE_FILE" 2>/dev/null || echo "(none)"
        ;;
    *)
        echo "Usage: $0 {up|down|update|status} [tun-interface]"
        exit 1
        ;;
esac

#!/bin/bash
# vpn-policy-routing.sh — Policy routing для гибрида B+
#
# Создаёт две routing tables и ip rules:
#   table 200 (marked): fwmark 0x1 → заблокированный трафик → tun (VPS)
#   table 100 (vpn):    VPN-клиенты без fwmark → незаблокированное → eth0 (прямой)
#
# Запускается как vpn-routes.service (After=wg-quick@wg0 wg-quick@wg1)
# При смене активного tun: watchdog вызывает этот скрипт повторно с аргументом TUN_IFACE
#
# Использование:
#   vpn-policy-routing.sh [up|down] [tun-интерфейс]
#   По умолчанию: up, интерфейс из /run/vpn-active-tun

set -euo pipefail

# ── Конфигурация ────────────────────────────────────────────────────────────
TABLE_MARKED=200          # fwmark 0x1 → заблокированное → VPS
TABLE_VPN=100             # VPN-клиенты → незаблокированное → eth0
FWMARK=0x1

AWG_SUBNET="10.177.1.0/24"
WG_SUBNET="10.177.3.0/24"
TIER2_SUBNET="10.177.2.0/30"

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
        ip route add default dev "$TUN_IFACE" table $TABLE_MARKED
        log "Table $TABLE_MARKED: default dev $TUN_IFACE"
    else
        # Нет активного tun → UNREACHABLE (kill switch через routing)
        ip route add unreachable default table $TABLE_MARKED
        log "Table $TABLE_MARKED: unreachable (tun не определён)"
    fi

    # --- Table 100: VPN-клиенты (незаблокированное) → eth0 ---

    # Очищаем старые маршруты в table 100
    ip route flush table $TABLE_VPN 2>/dev/null || true

    # Маршрут по умолчанию через основной gateway
    ip route add default via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_VPN
    log "Table $TABLE_VPN: default via $GATEWAY dev $ETH_IFACE"

    # Локальная сеть — напрямую
    LOCAL_SUBNET="${HOME_SUBNET:-$(ip -4 addr show dev "$ETH_IFACE" | awk '/inet / {print $2}' | head -1 | cut -d'/' -f1 | sed 's/\.[0-9]*$/\.0/')/24}"
    ip route add "$LOCAL_SUBNET" dev "$ETH_IFACE" table $TABLE_VPN 2>/dev/null || true

    # --- ip rules ---

    # Priority 100: fwmark 0x1 → table 200 (заблокированное → VPN)
    if ! rule_exists "fwmark $FWMARK lookup $TABLE_MARKED"; then
        ip rule add fwmark $FWMARK lookup $TABLE_MARKED priority 100
        log "Rule: fwmark $FWMARK → table $TABLE_MARKED (priority 100)"
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

    log "Table $TABLE_MARKED: default → $new_tun"
}

# ── DOWN: очистка ────────────────────────────────────────────────────────────
teardown_routing() {
    log "Удаление policy routing"

    # Удаляем ip rules
    ip rule del fwmark $FWMARK lookup $TABLE_MARKED 2>/dev/null || true
    ip rule del from $AWG_SUBNET lookup $TABLE_VPN 2>/dev/null || true
    ip rule del from $WG_SUBNET lookup $TABLE_VPN 2>/dev/null || true

    # Очищаем routing tables
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

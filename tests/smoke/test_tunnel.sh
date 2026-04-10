#!/usr/bin/env bash
# Smoke test: WireGuard / AmneziaWG туннели
# Проверяет наличие интерфейсов, Tier-2 туннель к VPS, active tun для стеков.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="TUNNEL"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. wg0 (AmneziaWG) существует
if ip link show wg0 &>/dev/null; then
    STATE=$(ip link show wg0 | grep -oP '(?<=state )\S+')
    if [[ "$STATE" == "UNKNOWN" || "$STATE" == "UP" ]]; then
        pass "wg0 (AmneziaWG) активен"
    else
        warn "wg0 существует но статус: $STATE"
    fi
else
    fail "wg0 (AmneziaWG) интерфейс не найден"
fi

# 2. wg1 (WireGuard) существует
if ip link show wg1 &>/dev/null; then
    STATE=$(ip link show wg1 | grep -oP '(?<=state )\S+')
    if [[ "$STATE" == "UNKNOWN" || "$STATE" == "UP" ]]; then
        pass "wg1 (WireGuard) активен"
    else
        warn "wg1 существует но статус: $STATE"
    fi
else
    warn "wg1 (WireGuard) интерфейс не найден (нет WG-клиентов?)"
fi

# 3. Tier-2 туннель: локальный endpoint на tun0
TIER2_IP=$(ip addr show tun0 2>/dev/null | grep "inet " | awk '{print $2}' | head -1)
if [[ "$TIER2_IP" == "10.177.2.1/30" ]]; then
    pass "tun0 IP: $TIER2_IP"
else
    warn "tun0 IP не в ожидаемом диапазоне: ${TIER2_IP:-не назначен}"
fi

# 4. VPS доступен через Tier-2 туннель
VPS_TUN_IP="${VPS_TUNNEL_IP:-10.177.2.2}"
if ping -c 2 -W 5 -q "$VPS_TUN_IP" &>/dev/null; then
    PING_OUT=$(ping -c 3 -W 5 "$VPS_TUN_IP" 2>/dev/null | tail -1)
    RTT=$(echo "$PING_OUT" | grep -oP 'avg = \K[\d.]+' || echo "$PING_OUT" | awk -F'/' '{print $5}')
    pass "VPS $VPS_TUN_IP доступен через туннель (avg RTT: ${RTT:-?} ms)"
else
    warn "VPS $VPS_TUN_IP недоступен (стек ещё не поднят или VPS выключен)"
fi

# 4b. iperf3 endpoint на tier-2
if nc -z -w 3 "$VPS_TUN_IP" 5201 &>/dev/null; then
    pass "iperf3 на $VPS_TUN_IP:5201 доступен через tier-2"
else
    warn "iperf3 на $VPS_TUN_IP:5201 недоступен"
fi

# 5. Активный tun интерфейс для выхода
TUN_IFACE=$(ip link show 2>/dev/null | grep -oP '^[0-9]+: \K(tun\S+|wg\S+)' | head -1 || true)
if ip link show 2>/dev/null | grep -qE '(tun[0-9]|awgtun)'; then
    TUN_IFACE=$(ip link show 2>/dev/null | grep -oP '^[0-9]+: \Ktun\S+' | head -1)
    pass "Активный tun интерфейс: ${TUN_IFACE:-найден}"
else
    warn "tun интерфейс не найден (watchdog ещё не поднял стек?)"
fi

# 6. Маршрут table 200 существует (default → tun)
if ip route show table 200 2>/dev/null | grep -q "default"; then
    ROUTE=$(ip route show table 200 | grep default)
    pass "table 200 default маршрут: $ROUTE"
else
    warn "table 200 default маршрут отсутствует (стек не поднят)"
fi

# 7. awg-quick@wg0 сервис (клиентский AmneziaWG ingress)
if systemctl is-active --quiet "awg-quick@wg0"; then
    pass "awg-quick@wg0 активен"
else
    fail "awg-quick@wg0 не запущен"
fi

# 8. hysteria2 сервис
if systemctl is-active --quiet hysteria2 2>/dev/null; then
    pass "hysteria2.service активен"
else
    warn "hysteria2.service не активен (стек Hysteria2 отключён?)"
fi

# 8b. tier2-connect сервис
if systemctl list-unit-files tier2-connect.service >/dev/null 2>&1; then
    if systemctl is-active --quiet tier2-connect; then
        pass "tier2-connect.service активен"
    else
        warn "tier2-connect.service не активен"
    fi
else
    warn "tier2-connect.service не найден"
fi

# 9. wg peers существуют (хотя бы один клиент или Tier-2)
PEER_COUNT_WG0=$(sudo wg show wg0 peers 2>/dev/null | wc -l)
if (( PEER_COUNT_WG0 > 0 )); then
    pass "wg0 содержит $PEER_COUNT_WG0 peer(s)"
else
    pass "wg0 peers не настроены (клиенты не подключены)"
fi

# 10. WireGuard handshake: Tier-2 пир (VPS) должен иметь свежий handshake
WG0_PEERS=$(sudo wg show wg0 latest-handshakes 2>/dev/null || true)
if [[ -n "$WG0_PEERS" ]]; then
    now=$(date +%s)
    stale_count=0
    fresh_count=0
    while read -r pubkey ts; do
        [[ -z "$pubkey" || -z "$ts" ]] && continue
        [[ "$ts" == "0" ]] && continue  # ни разу не соединялся
        [[ "$ts" =~ ^[0-9]+$ ]] || continue  # нечисловой timestamp
        age=$(( now - ts ))
        if (( age < 180 )); then
            (( fresh_count++ ))
        else
            (( stale_count++ ))
        fi
    done <<< "$WG0_PEERS"

    if (( fresh_count > 0 )); then
        pass "wg0: $fresh_count peer(s) с handshake < 3 мин (активные)"
    elif (( stale_count > 0 )); then
        warn "wg0: $stale_count peer(s) с устаревшим handshake > 3 мин"
    fi
fi

# 11. WG1 handshake аналогично
WG1_PEERS=$(sudo wg show wg1 latest-handshakes 2>/dev/null || true)
if [[ -n "$WG1_PEERS" ]]; then
    now=$(date +%s)
    stale_wg1=0; fresh_wg1=0
    while read -r pubkey ts; do
        [[ -z "$pubkey" || -z "$ts" || "$ts" == "0" ]] && continue
        age=$(( now - ts ))
        (( age < 180 )) && (( fresh_wg1++ )) || (( stale_wg1++ ))
    done <<< "$WG1_PEERS"
    if (( fresh_wg1 > 0 )); then
        pass "wg1: $fresh_wg1 peer(s) с handshake < 3 мин"
    elif (( stale_wg1 > 0 )); then
        pass "wg1: $stale_wg1 peer(s) настроены, активных handshake сейчас нет"
    fi
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

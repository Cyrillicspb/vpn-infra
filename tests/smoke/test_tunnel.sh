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

# 3. Tier-2 туннель: IP адреса
AWG_IP=$(ip addr show wg0 2>/dev/null | grep "inet " | awk '{print $2}' | head -1)
if [[ "$AWG_IP" == "10.177.2.1/30"* || "$AWG_IP" == 10.177.* ]]; then
    pass "wg0 IP: $AWG_IP"
else
    warn "wg0 IP не в ожидаемом диапазоне: ${AWG_IP:-не назначен}"
fi

# 4. VPS доступен через Tier-2 туннель
VPS_TUN_IP="${VPS_TUNNEL_IP:-10.177.2.2}"
if ping -c 2 -W 5 -q "$VPS_TUN_IP" &>/dev/null; then
    RTT=$(ping -c 3 -W 5 "$VPS_TUN_IP" 2>/dev/null | tail -1 | grep -oP 'avg = \K[\d.]+' || \
          ping -c 3 -W 5 "$VPS_TUN_IP" 2>/dev/null | tail -1 | awk -F'/' '{print $5}')
    pass "VPS $VPS_TUN_IP доступен через туннель (avg RTT: ${RTT:-?} ms)"
else
    warn "VPS $VPS_TUN_IP недоступен (стек ещё не поднят или VPS выключен)"
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

# 7. awg-quick@wg0 сервис (AmneziaWG Tier-2 туннель)
if systemctl is-active --quiet "awg-quick@wg0"; then
    pass "awg-quick@wg0 активен"
elif systemctl is-active --quiet "wg-quick@wg0"; then
    pass "wg-quick@wg0 активен"
else
    fail "awg-quick@wg0 не запущен"
fi

# 8. hysteria2 сервис
if systemctl is-active --quiet hysteria2 2>/dev/null; then
    pass "hysteria2.service активен"
else
    warn "hysteria2.service не активен (стек Hysteria2 отключён?)"
fi

# 9. wg peers существуют (хотя бы один клиент или Tier-2)
PEER_COUNT_WG0=$(sudo wg show wg0 peers 2>/dev/null | wc -l)
if (( PEER_COUNT_WG0 > 0 )); then
    pass "wg0 содержит $PEER_COUNT_WG0 peer(s)"
else
    warn "wg0 не имеет peers (нет клиентов?)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

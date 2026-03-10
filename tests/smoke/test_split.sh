#!/bin/bash
# Тест: Split tunneling — nft sets (table inet vpn) и policy routing
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true
FAIL=0

ok()   { echo "  [OK]   $1"; }
fail() { echo "  [FAIL] $1"; ((FAIL++)); }
warn() { echo "  [WARN] $1"; }

# blocked_static — table inet vpn (не inet filter!)
nft list set inet vpn blocked_static > /dev/null 2>&1 \
    && ok "blocked_static (table inet vpn)" \
    || fail "nft set blocked_static не найден в table inet vpn"

# blocked_dynamic — table inet vpn
nft list set inet vpn blocked_dynamic > /dev/null 2>&1 \
    && ok "blocked_dynamic (table inet vpn)" \
    || fail "nft set blocked_dynamic не найден в table inet vpn"

# fwmark 0x1 → lookup 200
ip rule show | grep -q "fwmark 0x1 lookup 200" \
    && ok "ip rule fwmark 0x1 → table 200" \
    || fail "ip rule fwmark 0x1 lookup 200 не найден"

# AWG subnet → table 100
ip rule show | grep -q "10.177.1.0/24" \
    && ok "ip rule AWG 10.177.1.0/24 → table 100" \
    || fail "ip rule from 10.177.1.0/24 не найден"

# WG subnet → table 100
ip rule show | grep -q "10.177.3.0/24" \
    && ok "ip rule WG 10.177.3.0/24 → table 100" \
    || fail "ip rule from 10.177.3.0/24 не найден"

# table 100 default
ip route show table 100 2>/dev/null | grep -q "default" \
    && ok "table 100 default маршрут" \
    || warn "нет default в table 100 (VPN ещё не поднят?)"

echo ""
[[ $FAIL -eq 0 ]] && { echo "Split tunneling: OK"; exit 0; } \
    || { echo "Split tunneling: FAIL ($FAIL)"; exit 1; }

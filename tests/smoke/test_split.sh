#!/usr/bin/env bash
# Smoke test: Split tunneling — nft sets (table inet vpn) и policy routing
# Проверяет что оба уровня split tunneling настроены корректно.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="SPLIT_TUNNELING"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# === УРОВЕНЬ 1: nft sets (table inet vpn) ===

# 1. table inet vpn существует
if nft list table inet vpn &>/dev/null; then
    pass "nft table inet vpn существует"
else
    fail "nft table inet vpn не найдена"
fi

# 2. blocked_static set
if nft list set inet vpn blocked_static &>/dev/null; then
    COUNT=$(nft list set inet vpn blocked_static 2>/dev/null | grep -c 'elements =' || \
            nft list set inet vpn blocked_static 2>/dev/null | grep -oP '\d+ elements' | grep -oP '\d+' || \
            echo "?")
    ELEM_COUNT=$(nft list set inet vpn blocked_static 2>/dev/null | \
                 grep -oP '(?<=\{)[^}]+' | tr ',' '\n' | wc -l 2>/dev/null || echo 0)
    pass "nft set blocked_static существует (элементов: ~${ELEM_COUNT})"
    if (( ELEM_COUNT > 100 )); then
        pass "blocked_static содержит >100 записей (базы РКН загружены)"
    elif (( ELEM_COUNT > 0 )); then
        warn "blocked_static содержит только $ELEM_COUNT записей (базы РКН не обновлены?)"
    else
        warn "blocked_static пуст (выполните обновление маршрутов)"
    fi
else
    fail "nft set blocked_static не найден в table inet vpn"
fi

# 3. blocked_dynamic set с timeout
if nft list set inet vpn blocked_dynamic &>/dev/null; then
    pass "nft set blocked_dynamic существует"
    if nft list set inet vpn blocked_dynamic 2>/dev/null | grep -q "timeout"; then
        pass "blocked_dynamic имеет timeout (self-cleaning)"
    else
        warn "blocked_dynamic не имеет timeout"
    fi
else
    fail "nft set blocked_dynamic не найден в table inet vpn"
fi

# === УРОВЕНЬ 2: Policy routing ===

# 4. ip rule fwmark 0x1 → table 200
if ip rule show 2>/dev/null | grep -q "fwmark 0x1.*lookup 200"; then
    pass "ip rule: fwmark 0x1 → table 200 (заблокированное → VPN)"
else
    fail "ip rule: fwmark 0x1 → table 200 не найден"
fi

# 5. ip rule: DNS через VPN (1.1.1.1)
if ip rule show 2>/dev/null | grep -q "1.1.1.1.*lookup 200"; then
    pass "ip rule: DNS 1.1.1.1 → table 200"
elif ip rule show 2>/dev/null | grep -q "8.8.8.8.*lookup 200"; then
    pass "ip rule: DNS 8.8.8.8 → table 200"
else
    warn "ip rule для DNS через VPN не найден"
fi

# 6. AWG subnet → table 100 (прямой интернет для незаблокированных)
if ip rule show 2>/dev/null | grep -q "10.177.1.0/24.*lookup 100"; then
    pass "ip rule: AWG 10.177.1.0/24 → table 100 (прямой интернет)"
else
    fail "ip rule: AWG 10.177.1.0/24 → table 100 не найден"
fi

# 7. WG subnet → table 100
if ip rule show 2>/dev/null | grep -q "10.177.3.0/24.*lookup 100"; then
    pass "ip rule: WG 10.177.3.0/24 → table 100 (прямой интернет)"
else
    fail "ip rule: WG 10.177.3.0/24 → table 100 не найден"
fi

# 8. table 100 default route (прямой интернет)
if ip route show table 100 2>/dev/null | grep -q "^default"; then
    GW=$(ip route show table 100 | grep "^default" | awk '{print $3}')
    pass "table 100 default via $GW (прямой интернет)"
else
    warn "table 100 default отсутствует (стек ещё не поднят?)"
fi

# 9. table 200 default route (через VPN tun)
if ip route show table 200 2>/dev/null | grep -q "^default"; then
    ROUTE=$(ip route show table 200 | grep "^default")
    pass "table 200 default: $ROUTE"
else
    warn "table 200 default отсутствует (tun ещё не поднят?)"
fi

# 10. /etc/vpn-routes/combined.cidr существует и непустой
CIDR_FILE="/etc/vpn-routes/combined.cidr"
if [[ -f "$CIDR_FILE" ]]; then
    CIDR_COUNT=$(wc -l < "$CIDR_FILE" 2>/dev/null || echo 0)
    if (( CIDR_COUNT > 100 )); then
        pass "combined.cidr содержит $CIDR_COUNT записей"
    else
        warn "combined.cidr содержит только $CIDR_COUNT записей"
    fi
    if (( CIDR_COUNT <= 500 )); then
        pass "combined.cidr ≤500 записей (в пределах лимита WireGuard)"
    else
        warn "combined.cidr содержит $CIDR_COUNT > 500 записей (возможны проблемы с QR)"
    fi
else
    warn "combined.cidr не найден (маршруты не обновлялись)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

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
    ELEM_COUNT=$(nft -j list set inet vpn blocked_static 2>/dev/null | \
        python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('nftables',[{}])[-1].get('set',{}).get('elem',[])))" 2>/dev/null || \
        nft list set inet vpn blocked_static 2>/dev/null | grep -cE '^\s+[0-9a-f.:]+' || echo 0)
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
    BLOCKED_DYNAMIC_TEXT="$(nft list set inet vpn blocked_dynamic 2>/dev/null || true)"
    if [[ "$BLOCKED_DYNAMIC_TEXT" == *"timeout"* || "$BLOCKED_DYNAMIC_TEXT" == *"expires"* ]]; then
        pass "blocked_dynamic имеет timeout (self-cleaning)"
    else
        warn "blocked_dynamic не имеет timeout"
    fi
else
    fail "nft set blocked_dynamic не найден в table inet vpn"
fi

# === УРОВЕНЬ 2: Policy routing ===

# 4. ip rule fwmark 0x1 → table 200 (может называться "marked")
if ip rule show 2>/dev/null | grep -qE "fwmark 0x1.*(lookup 200|lookup marked)"; then
    pass "ip rule: fwmark 0x1 → table 200/marked (заблокированное → VPN)"
else
    fail "ip rule: fwmark 0x1 → table 200 не найден"
fi

# 5. ip rule: DNS upstream НЕ должны быть в table 200 — ломает dnsmasq upstream
# Текущие upstream: 77.88.8.8/77.88.8.1 (Яндекс DNS). 1.1.1.1/8.8.8.8 заблокированы ISP.
if ! ip rule show 2>/dev/null | grep -qE "(77\.88\.8\.[18]|1\.1\.1\.1|8\.8\.8\.8).*(lookup 200|lookup marked)"; then
    pass "ip rule: DNS upstream (Yandex/CF/Google) NOT in table 200 (dnsmasq safe)"
else
    fail "ip rule: DNS upstream servers in table 200 — breaks dnsmasq upstream"
fi

# 6. AWG subnet → table 100 (может называться "vpn")
if ip rule show 2>/dev/null | grep -qE "10.177.1.0/24.*(lookup 100|lookup vpn)"; then
    pass "ip rule: AWG 10.177.1.0/24 → table 100/vpn (прямой интернет)"
else
    fail "ip rule: AWG 10.177.1.0/24 → table 100 не найден"
fi

# 7. WG subnet → table 100 (может называться "vpn")
if ip rule show 2>/dev/null | grep -qE "10.177.3.0/24.*(lookup 100|lookup vpn)"; then
    pass "ip rule: WG 10.177.3.0/24 → table 100/vpn (прямой интернет)"
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
    CIDR_COUNT=$(grep -vc '^\s*#' "$CIDR_FILE" 2>/dev/null || echo 0)
    if (( CIDR_COUNT > 100 )); then
        pass "combined.cidr содержит $CIDR_COUNT записей"
    else
        warn "combined.cidr содержит только $CIDR_COUNT записей"
    fi
    if (( CIDR_COUNT <= 12000 )); then
        pass "combined.cidr ≤12000 записей (temporary correctness-first limit)"
    else
        fail "combined.cidr содержит $CIDR_COUNT > 12000 записей"
    fi

    WIDE_78=$(awk -F/ 'NF==2 && ($2==7 || $2==8) {c++} END{print c+0}' "$CIDR_FILE")
    WIDE_910=$(awk -F/ 'NF==2 && ($2==9 || $2==10) {c++} END{print c+0}' "$CIDR_FILE")
    if (( WIDE_78 == 0 )); then
        pass "combined.cidr не содержит /7 и /8"
    else
        fail "combined.cidr содержит /7 или /8: $WIDE_78"
    fi
    if (( WIDE_910 == 0 )); then
        pass "combined.cidr не содержит /9 и /10"
    else
        pass "combined.cidr содержит контролируемые /9 или /10 агрегаты: $WIDE_910"
    fi
else
    warn "combined.cidr не найден (маршруты не обновлялись)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

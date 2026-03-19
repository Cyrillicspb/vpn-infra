#!/usr/bin/env bash
# Smoke test: Kill switch — nftables правила защиты от утечек
# Проверяет что заблокированный трафик не может утечь через eth0 при падении tun.
set -uo pipefail

PASS=0; FAIL=0; WARN=0
TEST_NAME="KILL_SWITCH"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. table inet vpn существует
if nft list table inet vpn &>/dev/null; then
    pass "nft table inet vpn существует"
else
    fail "nft table inet vpn не найдена — kill switch не работает"
    echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
    exit 1
fi

# 2. forward chain существует
FORWARD_CHAIN=$(nft list chain inet vpn forward 2>/dev/null || true)
if [[ -n "$FORWARD_CHAIN" ]]; then
    pass "Цепочка inet vpn forward существует"
else
    fail "Цепочка inet vpn forward не найдена"
fi

# 3. Kill switch: правило DROP для заблокированного через не-tun
if echo "$FORWARD_CHAIN" | grep -qiE "drop"; then
    pass "DROP правило в forward chain"
else
    fail "DROP правило не найдено в forward chain"
fi

# 4. blocked_static в forward chain
if echo "$FORWARD_CHAIN" | grep -q "blocked_static"; then
    pass "blocked_static проверяется в forward chain"
else
    fail "blocked_static НЕ проверяется в forward chain (утечка данных!)"
fi

# 5. blocked_dynamic в forward chain
if echo "$FORWARD_CHAIN" | grep -q "blocked_dynamic"; then
    pass "blocked_dynamic проверяется в forward chain"
else
    fail "blocked_dynamic НЕ проверяется в forward chain"
fi

# 6. Kill switch проверяется ПЕРЕД ct state established для VPN-трафика
# Правильный порядок: kill switch → ct state established/related
CHAIN_TEXT="$FORWARD_CHAIN"
KS_LINE=$(echo "$CHAIN_TEXT" | grep -n "blocked_static\|blocked_dynamic" | head -1 | cut -d: -f1)
CT_LINE=$(echo "$CHAIN_TEXT" | grep -n "ct state established" | head -1 | cut -d: -f1)
if [[ -n "$KS_LINE" && -n "$CT_LINE" ]]; then
    if (( KS_LINE < CT_LINE )); then
        pass "Kill switch (строка $KS_LINE) до ct state established (строка $CT_LINE)"
    else
        warn "Kill switch порядок: kill switch после ct state established (потенциальная утечка)"
    fi
fi

# 7. prerouting chain с fwmark
PREROUTING=$(nft list chain inet vpn prerouting 2>/dev/null || true)
if [[ -n "$PREROUTING" ]]; then
    pass "Цепочка inet vpn prerouting существует"
    if echo "$PREROUTING" | grep -qE "(meta )?mark set 0x1"; then
        pass "fwmark 0x1 устанавливается в prerouting"
    else
        fail "mark set 0x1 не найден в prerouting"
    fi
else
    fail "Цепочка inet vpn prerouting не найдена"
fi

# 8. ip rule: fwmark → table 200 с UNREACHABLE при падении tun
if ip rule show 2>/dev/null | grep -qE "fwmark 0x1.*(lookup 200|lookup marked)"; then
    pass "ip rule: fwmark 0x1 → table 200/marked"
else
    fail "ip rule: fwmark 0x1 → table 200 не найден (kill switch через policy routing не работает)"
fi

# 9. nftables правила для rate limiting (защита от UDP flood)
INPUT_CHAIN=$(nft list chain inet vpn input 2>/dev/null || \
              nft list chain inet vpn INPUT 2>/dev/null || true)
if echo "$INPUT_CHAIN" | grep -qiE "limit rate|udp dport 5182[01]"; then
    pass "Rate limiting для UDP портов WireGuard в input chain"
else
    warn "Rate limiting для UDP 51820/51821 не найден в input chain"
fi

# 10. IPv6 отключён (защита от утечки через IPv6)
IPV6_STATUS=$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo "?")
if [[ "$IPV6_STATUS" == "1" ]]; then
    pass "IPv6 отключён системно (нет утечки через IPv6)"
else
    warn "IPv6 включён — возможна утечка через IPv6 (disable_ipv6=$IPV6_STATUS)"
fi

# 11. Проверка masquerade для VPN-трафика
POSTROUTING=$(nft list chain inet vpn postrouting 2>/dev/null || \
              nft list chain inet vpn POSTROUTING 2>/dev/null || true)
if echo "$POSTROUTING" | grep -qi "masquerade"; then
    pass "MASQUERADE для VPN трафика настроен"
else
    warn "MASQUERADE не найден (VPN-трафик может не выходить в интернет)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

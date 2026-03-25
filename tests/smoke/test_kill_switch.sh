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

# 5a. Level 2: nftables forward DROP для blocked_static
if nft list chain inet vpn forward 2>/dev/null | grep -qE 'ip daddr @blocked_static.*drop'; then
    pass "nftables forward: kill switch для blocked_static configured"
else
    fail "nftables forward: kill switch для blocked_static MISSING"
fi

# 5b. Level 2: nftables forward DROP для blocked_dynamic
if nft list chain inet vpn forward 2>/dev/null | grep -qE 'ip daddr @blocked_dynamic.*drop'; then
    pass "nftables forward: kill switch для blocked_dynamic configured"
else
    fail "nftables forward: kill switch для blocked_dynamic MISSING"
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
    if echo "$PREROUTING" | grep -qE "(meta )?mark set 0x0*1"; then
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

# 12. Поведенческий тест: заблокированный трафик идёт через tun, не через eth0
# Берём первый IP из blocked_static — он должен маршрутизироваться через table 200
BLOCKED_IP=$(nft list set inet vpn blocked_static 2>/dev/null \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [[ -n "$BLOCKED_IP" ]]; then
    route_out=$(ip route get "$BLOCKED_IP" mark 0x1 2>/dev/null || true)
    if echo "$route_out" | grep -qE "(tun[0-9]|awgtun|UNREACHABLE)"; then
        if echo "$route_out" | grep -q "UNREACHABLE"; then
            pass "kill switch ACTIVE: $BLOCKED_IP → UNREACHABLE (tun упал, трафик заблокирован)"
        else
            tun_dev=$(echo "$route_out" | grep -oP 'dev \K\S+' | head -1)
            pass "kill switch routing OK: $BLOCKED_IP → tun ($tun_dev)"
        fi
    elif echo "$route_out" | grep -qE "dev (eth|ens|enp)"; then
        fail "УТЕЧКА: $BLOCKED_IP (fwmark 0x1) маршрутизируется через eth0!"
        echo "       ip route get: $route_out"
    else
        warn "kill switch: неожиданный маршрут для $BLOCKED_IP: $route_out"
    fi
else
    warn "blocked_static пуст — нет IP для поведенческого теста"
fi

# 13. Незаблокированный трафик идёт через eth0 (split tunneling работает)
# Берём IP из table 100 (должен идти напрямую)
ETH_IFACE=$(ip route show default table main 2>/dev/null | grep -oP 'dev \K\S+' | head -1)
if [[ -n "$ETH_IFACE" ]]; then
    # 77.88.8.8 — Яндекс DNS, не в CDN_SUBNETS/blocked_static → идёт напрямую
    TEST_IP="77.88.8.8"
    route_direct=$(ip route get "$TEST_IP" from 10.177.1.1 2>/dev/null || true)
    if echo "$route_direct" | grep -q "dev $ETH_IFACE"; then
        pass "split tunneling: незаблокированный $TEST_IP → $ETH_IFACE (прямой выход)"
    elif echo "$route_direct" | grep -qE "tun|UNREACHABLE"; then
        warn "split tunneling: $TEST_IP идёт через tun или UNREACHABLE (всё через VPN?)"
    else
        warn "split tunneling: неожиданный маршрут для $TEST_IP: $route_direct"
    fi
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

#!/bin/bash
# Тест: Kill switch — nftables правила (table inet vpn, не inet filter/mangle)
set -euo pipefail

FAIL=0
ok()   { echo "  [OK]   $1"; }
fail() { echo "  [FAIL] $1"; ((FAIL++)); }
warn() { echo "  [WARN] $1"; }

# forward chain в table inet vpn
FORWARD=$(nft list chain inet vpn forward 2>/dev/null) || {
    fail "chain inet vpn forward не найдена"
    echo "Kill switch: FAIL"; exit 1
}

# Kill switch: blocked sets проверяются в forward
echo "$FORWARD" | grep -q "blocked_static" \
    && ok "kill switch blocked_static в forward" \
    || fail "blocked_static не найден в forward chain"

echo "$FORWARD" | grep -q "blocked_dynamic" \
    && ok "kill switch blocked_dynamic в forward" \
    || fail "blocked_dynamic не найден в forward chain"

# DROP правило
echo "$FORWARD" | grep -qi "drop" \
    && ok "DROP правило в forward" \
    || fail "DROP не найден в forward chain"

# prerouting в table inet vpn (fwmark — в нашей таблице, не в mangle)
PREROUTING=$(nft list chain inet vpn prerouting 2>/dev/null) || {
    fail "chain inet vpn prerouting не найдена"
    echo "Kill switch: FAIL"; exit 1
}

echo "$PREROUTING" | grep -q "mark set 0x1" \
    && ok "fwmark 0x1 в prerouting" \
    || fail "mark set 0x1 не найден в prerouting"

# ip rule: fwmark → table 200
ip rule show | grep -q "fwmark 0x1 lookup 200" \
    && ok "ip rule fwmark 0x1 → table 200" \
    || fail "ip rule fwmark 0x1 lookup 200 не настроен"

echo ""
[[ $FAIL -eq 0 ]] && { echo "Kill switch: OK"; exit 0; } \
    || { echo "Kill switch: FAIL ($FAIL)"; exit 1; }

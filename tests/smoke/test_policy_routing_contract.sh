#!/usr/bin/env bash
# Smoke test: policy-routing contract for blocked/DPI traffic.
set -uo pipefail

PASS=0; FAIL=0; WARN=0
TEST_NAME="POLICY_ROUTING_CONTRACT"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

if ip rule show | grep -qE 'fwmark 0x2 lookup 201'; then
    pass "fwmark 0x2 -> table 201 присутствует"
else
    fail "fwmark 0x2 -> table 201 отсутствует"
fi

if ip rule show | grep -qE 'fwmark 0x1 lookup (200|marked)'; then
    pass "fwmark 0x1 -> table 200 присутствует"
else
    fail "fwmark 0x1 -> table 200 отсутствует"
fi

if ip route show table 200 2>/dev/null | grep -q '^default '; then
    pass "table 200 содержит default route"
else
    fail "table 200 не содержит default route"
fi

if ip route show table 201 2>/dev/null | grep -q '^default '; then
    pass "table 201 содержит default route"
else
    fail "table 201 не содержит default route"
fi

if ip rule show | grep -qE 'from 172\.21\.0\.0/24 lookup (100|vpn)'; then
    pass "functional namespaces -> table 100 присутствует"
else
    warn "functional namespaces -> table 100 отсутствует"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

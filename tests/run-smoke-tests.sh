#!/bin/bash
# Запуск всех smoke-тестов
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

run_test() {
    local name="$1"
    local script="$2"
    echo -n "  [$name] ... "
    if bash "$script" > /tmp/test-output.log 2>&1; then
        echo "PASS"
        ((PASS++))
    else
        echo "FAIL"
        cat /tmp/test-output.log
        ((FAIL++))
    fi
}

echo "=== VPN Infrastructure Smoke Tests ==="
echo ""

run_test "DNS"         "$SCRIPT_DIR/smoke/test_dns.sh"
run_test "Split"       "$SCRIPT_DIR/smoke/test_split.sh"
run_test "Tunnel"      "$SCRIPT_DIR/smoke/test_tunnel.sh"
run_test "Watchdog"    "$SCRIPT_DIR/smoke/test_watchdog.sh"
run_test "Bot"         "$SCRIPT_DIR/smoke/test_bot.sh"
run_test "Kill-Switch" "$SCRIPT_DIR/smoke/test_kill_switch.sh"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

#!/bin/bash
# Тест: Watchdog HTTP API
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true
WATCHDOG_URL="${WATCHDOG_URL:-http://localhost:8080}"
TOKEN="${WATCHDOG_API_TOKEN:-}"

# Сервис запущен?
systemctl is-active --quiet watchdog || { echo "watchdog сервис не запущен"; exit 1; }

# API отвечает?
HEADERS=""
[[ -n "$TOKEN" ]] && HEADERS="-H Authorization: Bearer $TOKEN"

status=$(curl -s --max-time 10 ${HEADERS:+-H "$HEADERS"} "$WATCHDOG_URL/status" 2>/dev/null)
[[ -n "$status" ]] || { echo "Watchdog API не отвечает на $WATCHDOG_URL/status"; exit 1; }

# Проверяем JSON
echo "$status" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'active_stack={d[\"active_stack\"]}')" \
    || { echo "Неверный JSON от watchdog"; exit 1; }

echo "Watchdog: OK ($WATCHDOG_URL/status → $status)"
exit 0

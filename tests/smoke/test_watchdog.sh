#!/bin/bash
# Тест: Watchdog HTTP API
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true
WATCHDOG_URL="${WATCHDOG_URL:-http://localhost:8080}"
TOKEN="${WATCHDOG_API_TOKEN:-}"

systemctl is-active --quiet watchdog \
    || { echo "watchdog сервис не запущен"; exit 1; }

if [[ -n "$TOKEN" ]]; then
    status=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${WATCHDOG_URL}/status" 2>/dev/null)
else
    status=$(curl -sf --max-time 10 "${WATCHDOG_URL}/status" 2>/dev/null)
fi

[[ -n "$status" ]] \
    || { echo "Watchdog API не отвечает на ${WATCHDOG_URL}/status"; exit 1; }

# Проверяем JSON без проблем с кавычками
active_stack=$(echo "$status" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('active_stack','?'))" \
    2>/dev/null) || { echo "Некорректный JSON от watchdog"; exit 1; }

echo "Watchdog: OK (active_stack=${active_stack})"
exit 0

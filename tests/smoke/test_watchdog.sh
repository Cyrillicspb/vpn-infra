#!/usr/bin/env bash
# Smoke test: Watchdog HTTP API
# Проверяет systemd статус, API доступность, JSON ответ, активный стек.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="WATCHDOG"
WATCHDOG_URL="${WATCHDOG_URL:-http://localhost:8080}"
TOKEN="${WATCHDOG_API_TOKEN:-}"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. Сервис запущен
if systemctl is-active --quiet watchdog; then
    pass "watchdog.service активен"
else
    fail "watchdog.service не запущен"
fi

# 2. Сервис не в failed состоянии
if ! systemctl is-failed --quiet watchdog; then
    pass "watchdog.service не в failed состоянии"
else
    fail "watchdog.service в состоянии failed"
    systemctl status watchdog --no-pager -l 2>/dev/null | tail -10 >&2
fi

# 3. Порт 8080 слушает
if ss -tlnp 2>/dev/null | grep -q ':8080 '; then
    pass "watchdog слушает на порту 8080"
else
    fail "Порт 8080 не занят (watchdog API недоступен)"
fi

# 4. GET /status отвечает
AUTH_HEADER=""
[[ -n "$TOKEN" ]] && AUTH_HEADER="-H Authorization: Bearer ${TOKEN}"

if [[ -n "$TOKEN" ]]; then
    STATUS_JSON=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${WATCHDOG_URL}/status" 2>/dev/null || true)
else
    STATUS_JSON=$(curl -sf --max-time 10 \
        "${WATCHDOG_URL}/status" 2>/dev/null || true)
fi

if [[ -n "$STATUS_JSON" ]]; then
    pass "GET /status отвечает"
else
    fail "GET /status не отвечает на ${WATCHDOG_URL}/status"
fi

# 5. Ответ — валидный JSON
if [[ -n "$STATUS_JSON" ]]; then
    if echo "$STATUS_JSON" | python3 -m json.tool &>/dev/null; then
        pass "Ответ /status — валидный JSON"
    else
        fail "Ответ /status — невалидный JSON: ${STATUS_JSON:0:100}"
    fi
fi

# 6. Активный стек указан
if [[ -n "$STATUS_JSON" ]]; then
    ACTIVE_STACK=$(echo "$STATUS_JSON" | python3 -c \
        "import json,sys; d=json.load(sys.stdin); print(d.get('active_stack',''))" 2>/dev/null || true)
    if [[ -n "$ACTIVE_STACK" && "$ACTIVE_STACK" != "null" ]]; then
        pass "Активный стек: $ACTIVE_STACK"
    else
        warn "active_stack не установлен (туннель ещё не поднят?)"
    fi

    # 7. Статус туннеля
    TUNNEL_UP=$(echo "$STATUS_JSON" | python3 -c \
        "import json,sys; d=json.load(sys.stdin); print(d.get('tunnel_up', d.get('status','?')))" 2>/dev/null || true)
    if [[ "$TUNNEL_UP" == "True" || "$TUNNEL_UP" == "true" || "$TUNNEL_UP" == "ok" ]]; then
        pass "Туннель UP"
    else
        warn "Туннель статус: $TUNNEL_UP"
    fi
fi

# 8. GET /metrics отвечает (Prometheus)
if [[ -n "$TOKEN" ]]; then
    METRICS=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${WATCHDOG_URL}/metrics" 2>/dev/null || true)
else
    METRICS=$(curl -sf --max-time 10 "${WATCHDOG_URL}/metrics" 2>/dev/null || true)
fi

if echo "$METRICS" | grep -q "vpn_"; then
    METRIC_COUNT=$(echo "$METRICS" | grep -c "^vpn_" 2>/dev/null || echo 0)
    pass "GET /metrics отвечает ($METRIC_COUNT vpn_ метрик)"
else
    warn "GET /metrics не содержит vpn_ метрик"
fi

# 9. Нет OOM в логах watchdog
OOM=$(journalctl -u watchdog --since "1 hour ago" --no-pager 2>/dev/null | grep -c "MemoryError\|OOM" || echo 0)
if (( OOM == 0 )); then
    pass "OOM ошибок в логах нет"
else
    warn "Найдено $OOM OOM ошибок в логах watchdog"
fi

# 10. Bearer token установлен
if [[ -n "$TOKEN" ]]; then
    pass "WATCHDOG_API_TOKEN установлен"
else
    warn "WATCHDOG_API_TOKEN не установлен в .env"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

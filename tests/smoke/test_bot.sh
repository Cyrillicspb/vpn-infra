#!/usr/bin/env bash
# Smoke test: Telegram-бот
# Проверяет что контейнер запущен, API отвечает, БД доступна, watchdog связь работает.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="BOT"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. Контейнер telegram-bot запущен
RUNNING=$(docker inspect --format '{{.State.Running}}' telegram-bot 2>/dev/null || echo "false")
if [[ "$RUNNING" == "true" ]]; then
    pass "telegram-bot контейнер запущен"
else
    fail "telegram-bot контейнер не запущен"
fi

# 2. Контейнер не в состоянии restarting
RESTARTING=$(docker inspect --format '{{.State.Restarting}}' telegram-bot 2>/dev/null || echo "true")
if [[ "$RESTARTING" == "false" ]]; then
    pass "telegram-bot не перезапускается"
else
    warn "telegram-bot в состоянии Restarting (crash loop?)"
fi

# 3. Нет критических ошибок в логах (последние 50 строк)
ERRORS=$(docker logs --tail 50 telegram-bot 2>&1 | grep -iE "Traceback|CriticalError|CRITICAL" | wc -l)
if (( ERRORS == 0 )); then
    pass "Нет критических ошибок в логах бота"
else
    fail "Найдено $ERRORS критических ошибок в логах бота"
    docker logs --tail 50 telegram-bot 2>&1 | grep -iE "Traceback|CriticalError|CRITICAL" | head -5 >&2
fi

# 4. Telegram API доступен и бот зарегистрирован
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    TGME=$(curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true)
    if [[ -n "$TGME" ]]; then
        BOT_NAME=$(echo "$TGME" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print(d['result']['username'])" 2>/dev/null || echo "?")
        if echo "$TGME" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); exit(0 if d['ok'] else 1)" 2>/dev/null; then
            pass "Telegram API отвечает: @$BOT_NAME"
        else
            fail "Telegram API вернул ok=false"
        fi
    else
        warn "Telegram API недоступен (нет интернета?)"
    fi
else
    warn "TELEGRAM_BOT_TOKEN не установлен в .env"
fi

# 5. SQLite БД существует и читается
BOT_DB="/opt/vpn/telegram-bot/data/vpn_bot.db"
if [[ -f "$BOT_DB" ]]; then
    pass "SQLite БД бота существует"
    TABLES=$(sqlite3 "$BOT_DB" ".tables" 2>/dev/null || true)
    if echo "$TABLES" | grep -q "clients"; then
        pass "Таблица clients в БД"
        CLIENT_COUNT=$(sqlite3 "$BOT_DB" "SELECT COUNT(*) FROM clients" 2>/dev/null || echo "?")
        pass "Клиентов в БД: $CLIENT_COUNT"
    else
        warn "Таблица clients не найдена (бот ещё не инициализирован?)"
    fi
else
    warn "SQLite БД не найдена: $BOT_DB (бот ещё не запускался?)"
fi

# 6. Бот может подключиться к watchdog API
WATCHDOG_URL="http://localhost:8080"
TOKEN="${WATCHDOG_API_TOKEN:-}"
if [[ -n "$TOKEN" ]]; then
    WD_STATUS=$(curl -sf --max-time 5 \
        -H "Authorization: Bearer ${TOKEN}" \
        "${WATCHDOG_URL}/status" 2>/dev/null || true)
    if [[ -n "$WD_STATUS" ]]; then
        pass "Бот → Watchdog API связь работает"
    else
        warn "Watchdog API недоступен на $WATCHDOG_URL (watchdog не запущен?)"
    fi
else
    warn "WATCHDOG_API_TOKEN не установлен, пропуск проверки связи бот→watchdog"
fi

# 7. docker-compose.yml бота существует
if [[ -f "/opt/vpn/telegram-bot/Dockerfile" ]]; then
    pass "Dockerfile бота существует"
else
    warn "Dockerfile бота не найден"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

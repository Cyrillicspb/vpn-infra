#!/bin/bash
# watchdog-failsafe.sh — cron failsafe для watchdog
# Запускается каждые 5 минут через /etc/cron.d/vpn-watchdog-failsafe
#
# Если watchdog не активен → прямой curl к Telegram API (обходит watchdog).
# Не зависит от watchdog — использует TOKEN и CHAT_ID из .env напрямую.

set -euo pipefail

ENV_FILE="/opt/vpn/.env"
LOCK_FILE="/run/vpn-failsafe.lock"
STATE_FILE="/run/vpn-failsafe-alerted"

# ── Загрузка переменных ──────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
else
    exit 0   # Нет .env — установка ещё не завершена
fi

# Проверяем что токен настроен
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
    exit 0
fi

# ── Один экземпляр ────────────────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    exit 0   # Уже запущен
fi

# ── Отправка в Telegram ───────────────────────────────────────────────────────
send_alert() {
    local msg="$1"
    curl -sf \
        --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        -d "text=${msg}" \
        -d "parse_mode=Markdown" \
        > /dev/null || true
}

# ── Проверка watchdog ─────────────────────────────────────────────────────────
if systemctl is-active --quiet watchdog; then
    # Watchdog активен — снимаем флаг алерта если он был
    if [[ -f "$STATE_FILE" ]]; then
        rm -f "$STATE_FILE"
        send_alert "✅ *Watchdog восстановлен* — сервис снова активен."
    fi
    exit 0
fi

# ── Watchdog мёртв ────────────────────────────────────────────────────────────
# Не спамим повторными алертами — один раз в 30 минут
LAST_ALERT=0
if [[ -f "$STATE_FILE" ]]; then
    LAST_ALERT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fi

NOW=$(date +%s)
INTERVAL=1800  # 30 минут

if (( NOW - LAST_ALERT < INTERVAL )); then
    exit 0   # Недавно уже отправляли
fi

# Сохраняем время алерта
echo "$NOW" > "$STATE_FILE"

HOSTNAME=$(hostname)
STATUS=$(systemctl status watchdog --no-pager -l 2>&1 | tail -5 || echo "недоступен")

send_alert "🚨 *WATCHDOG МЁРТВ* (${HOSTNAME})

Сервис watchdog не активен!
Последнее состояние: \`$(systemctl is-active watchdog 2>/dev/null || echo 'failed')\`

Попытка автоматического перезапуска..."

# Пробуем перезапустить
if systemctl start watchdog 2>/dev/null; then
    sleep 10
    if systemctl is-active --quiet watchdog; then
        rm -f "$STATE_FILE"
        send_alert "✅ *Watchdog перезапущен* автоматически failsafe-скриптом."
    fi
fi

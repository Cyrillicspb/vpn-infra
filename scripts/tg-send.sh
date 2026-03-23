#!/bin/bash
# tg-send.sh — Отправка сообщения в Telegram без TOKEN в cmdline
# Использование: tg-send.sh <chat_id> <text>
# TOKEN читается из .env или переменной окружения TELEGRAM_BOT_TOKEN

ENV_FILE="${VPN_ENV_FILE:-/opt/vpn/.env}"
[[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

CHAT_ID="${1:?chat_id required}"
TEXT="${2:?text required}"
TOKEN="${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set}"

curl -sf --max-time 15 \
    --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"' "$TOKEN") \
    -d "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${TEXT}" \
    >/dev/null 2>&1 || true

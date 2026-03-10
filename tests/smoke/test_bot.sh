#!/bin/bash
# Тест: Telegram-бот запущен
set -euo pipefail

# Контейнер запущен?
running=$(docker inspect --format '{{.State.Running}}' telegram-bot 2>/dev/null)
[[ "$running" == "true" ]] || { echo "telegram-bot контейнер не запущен"; exit 1; }

# Нет ошибок в логах?
errors=$(docker logs --tail 20 telegram-bot 2>&1 | grep -i "error\|exception\|traceback" | head -5)
if [[ -n "$errors" ]]; then
    echo "WARN: Ошибки в логах бота:"
    echo "$errors"
fi

# Проверяем через Telegram API
source /opt/vpn/.env 2>/dev/null || true
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    result=$(curl -s --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null)
    echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Bot: @{d[\"result\"][\"username\"]}')" \
        2>/dev/null || echo "WARN: Не удалось проверить бота через Telegram API"
fi

echo "Bot: OK"
exit 0

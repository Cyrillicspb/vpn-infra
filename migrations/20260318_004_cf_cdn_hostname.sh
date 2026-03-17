#!/bin/bash
# Миграция: CF_WORKER_HOSTNAME → CF_CDN_HOSTNAME в .env
# v0.2.0: переменная переименована; если в .env есть старое имя — добавляем новое.

ENV_FILE="/opt/vpn/.env"
[[ -f "$ENV_FILE" ]] || exit 0

# Уже есть CF_CDN_HOSTNAME с непустым значением — ничего делать не нужно
if grep -qE '^CF_CDN_HOSTNAME=.+' "$ENV_FILE"; then
    echo "[migration 004] CF_CDN_HOSTNAME уже задан — пропуск"
    exit 0
fi

# Извлекаем значение из старой переменной
OLD_VAL=$(grep -E '^CF_WORKER_HOSTNAME=' "$ENV_FILE" | cut -d'=' -f2- | tr -d '\r')

if [[ -z "$OLD_VAL" ]]; then
    echo "[migration 004] CF_WORKER_HOSTNAME не задан — пропуск"
    exit 0
fi

# Добавляем новую переменную
echo "CF_CDN_HOSTNAME=${OLD_VAL}" >> "$ENV_FILE"
echo "[migration 004] CF_CDN_HOSTNAME=${OLD_VAL} добавлен в .env"

#!/bin/bash
# Миграция: добавить XRAY_SERVER и XHTTP_*_PASSWORD в .env если отсутствуют
# v0.2.0: эти переменные были добавлены в шаблоны конфигов но могли не попасть в .env

ENV_FILE="/opt/vpn/.env"
[[ -f "$ENV_FILE" ]] || exit 0

changed=0

# XRAY_SERVER — IP VPS, нужен для address в config-grpc.json и config-reality.json
if ! grep -qE '^XRAY_SERVER=.+' "$ENV_FILE"; then
    VPS_IP=$(grep -E '^VPS_IP=' "$ENV_FILE" | cut -d'=' -f2- | tr -d '\r')
    if [[ -n "$VPS_IP" ]]; then
        echo "XRAY_SERVER=${VPS_IP}" >> "$ENV_FILE"
        echo "[migration 005] XRAY_SERVER=${VPS_IP} добавлен в .env"
        changed=1
    else
        echo "[migration 005] VPS_IP не задан, XRAY_SERVER пропущен"
    fi
else
    echo "[migration 005] XRAY_SERVER уже задан — пропуск"
fi

# XHTTP_MS_PASSWORD и XHTTP_CDN_PASSWORD — пароли xHTTP инбаундов в 3x-ui
# Если не заданы — пытаемся вытащить из 3x-ui sqlite напрямую
X_UI_DB="/opt/vpn/3x-ui/db/x-ui.db"
if [[ -f "$X_UI_DB" ]]; then
    if ! grep -qE '^XHTTP_MS_PASSWORD=.+' "$ENV_FILE"; then
        MS_PASS=$(sqlite3 "$X_UI_DB" \
            "SELECT json_extract(stream_settings, '$.splithttpSettings.password') FROM inbounds WHERE port=2087 LIMIT 1" 2>/dev/null)
        if [[ -n "$MS_PASS" ]]; then
            echo "XHTTP_MS_PASSWORD=${MS_PASS}" >> "$ENV_FILE"
            echo "[migration 005] XHTTP_MS_PASSWORD добавлен из 3x-ui"
            changed=1
        fi
    fi

    if ! grep -qE '^XHTTP_CDN_PASSWORD=.+' "$ENV_FILE"; then
        CDN_PASS=$(sqlite3 "$X_UI_DB" \
            "SELECT json_extract(stream_settings, '$.splithttpSettings.password') FROM inbounds WHERE port=2083 LIMIT 1" 2>/dev/null)
        if [[ -n "$CDN_PASS" ]]; then
            echo "XHTTP_CDN_PASSWORD=${CDN_PASS}" >> "$ENV_FILE"
            echo "[migration 005] XHTTP_CDN_PASSWORD добавлен из 3x-ui"
            changed=1
        fi
    fi
fi

# Пересобрать xray конфиги если что-то изменилось
if [[ $changed -eq 1 ]]; then
    set -a && source "$ENV_FILE" && set +a
    for tmpl in /opt/vpn/home/xray/*.json; do
        [[ -f "$tmpl" ]] || continue
        name=$(basename "$tmpl")
        envsubst < "$tmpl" > "/opt/vpn/xray/$name"
    done
    echo "[migration 005] xray конфиги пересобраны"
fi

#!/bin/bash
# ssh-proxy.sh — ProxyCommand для адаптивного SSH-туннеля к VPS.
#
# Читает текущий SOCKS5-порт из /var/run/vpn-active-socks-port (watchdog пишет
# при каждом переключении стека). Если файл отсутствует или пустой — прямое
# подключение (bootstrap, watchdog не запущен).
#
# Использование в ~/.ssh/config:
#   ProxyCommand /opt/vpn/scripts/ssh-proxy.sh %h %p

HOST="$1"
PORT="$2"

SOCKS_PORT_FILE="/var/run/vpn-active-socks-port"

SOCKS_PORT=""
if [[ -f "$SOCKS_PORT_FILE" ]]; then
    SOCKS_PORT=$(tr -d '[:space:]' < "$SOCKS_PORT_FILE" 2>/dev/null)
fi

EMERGENCY_PORT="8022"

if [[ -n "$SOCKS_PORT" && "$SOCKS_PORT" =~ ^[0-9]+$ ]]; then
    # Через активный SOCKS5-прокси стека
    exec nc -X 5 -x "127.0.0.1:${SOCKS_PORT}" "$HOST" "$PORT"
else
    # Прямое подключение — watchdog не запущен или стеки недоступны.
    # Пробуем основной порт; если недоступен (DPI блокирует) — аварийный 8022.
    if nc -z -w 5 "$HOST" "$PORT" 2>/dev/null; then
        exec nc "$HOST" "$PORT"
    else
        exec nc "$HOST" "$EMERGENCY_PORT"
    fi
fi

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

SOCKS_PORT_FILE="${SOCKS_PORT_FILE:-/var/run/vpn-active-socks-port}"
AUTOSSH_VPN_SOCKS_PORT="${AUTOSSH_VPN_SOCKS_PORT:-1183}"

if [[ -f /opt/vpn/.env ]]; then
    # shellcheck disable=SC1091
    source /opt/vpn/.env
fi

port_ready() {
    local candidate="$1"
    [[ "$candidate" =~ ^[0-9]+$ ]] || return 1
    nc -z -w 2 127.0.0.1 "$candidate" >/dev/null 2>&1
}

SOCKS_PORT=""
if [[ -f "$SOCKS_PORT_FILE" ]]; then
    SOCKS_PORT=$(tr -d '[:space:]' < "$SOCKS_PORT_FILE" 2>/dev/null)
fi

EMERGENCY_PORT="8022"

if port_ready "$SOCKS_PORT"; then
    # Через активный SOCKS5-прокси стека
    exec nc -X 5 -x "127.0.0.1:${SOCKS_PORT}" "$HOST" "$PORT"
elif port_ready "$AUTOSSH_VPN_SOCKS_PORT"; then
    # Резервный management SOCKS через autossh-vpn.
    exec nc -X 5 -x "127.0.0.1:${AUTOSSH_VPN_SOCKS_PORT}" "$HOST" "$PORT"
else
    # Прямое подключение — watchdog не запущен или стеки недоступны.
    # Пробуем основной порт; если недоступен (DPI блокирует) — аварийный 8022.
    if nc -z -w 5 "$HOST" "$PORT" 2>/dev/null; then
        exec nc "$HOST" "$PORT"
    else
        exec nc "$HOST" "$EMERGENCY_PORT"
    fi
fi

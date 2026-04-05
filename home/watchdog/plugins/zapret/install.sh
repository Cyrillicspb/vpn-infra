#!/bin/bash
# install.sh — установка zapret (nfqws) на домашний сервер Ubuntu 24.04
# Запускать: bash install.sh
set -euo pipefail

INSTALL_BIN="/usr/local/bin/nfqws"
ARCH="$(uname -m)"
# Бандленные бинарники (рядом со скриптом в репо)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLED_BIN_DIR="$SCRIPT_DIR/bin"

log() { echo "[zapret-install] $*"; }
err() { echo "[zapret-install] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Определить архитектуру
# ---------------------------------------------------------------------------
case "$ARCH" in
    x86_64)  BINARY_ARCH="x86_64" ;;
    aarch64) BINARY_ARCH="aarch64" ;;
    *)       err "Неподдерживаемая архитектура: $ARCH" ;;
esac

# ---------------------------------------------------------------------------
# 2. Проверить зависимости
# ---------------------------------------------------------------------------
log "Проверка зависимостей..."
apt-get update -qq
apt-get install -y -qq curl wget nftables

# ---------------------------------------------------------------------------
# 3. Установить nfqws бинарник
# ---------------------------------------------------------------------------
# Clean install должен использовать bundled binary из release bundle.

NFQWS_INSTALLED=0

# 3.1 Бандленный бинарник (bin/nfqws-ARCH в директории плагина)
BUNDLED_BIN="$BUNDLED_BIN_DIR/nfqws-${BINARY_ARCH}"
if [[ -f "$BUNDLED_BIN" ]]; then
    BUNDLED_VER="$(cat "$BUNDLED_BIN_DIR/VERSION" 2>/dev/null || echo "unknown")"
    log "Используем бандленный бинарник ${BUNDLED_VER} (${BINARY_ARCH})"
    cp "$BUNDLED_BIN" "$INSTALL_BIN"
    chmod +x "$INSTALL_BIN"
    NFQWS_INSTALLED=1
fi

if [[ $NFQWS_INSTALLED -eq 0 ]]; then
    err "Bundled nfqws (${BINARY_ARCH}) отсутствует. Clean install требует полный release bundle."
fi

chmod +x "$INSTALL_BIN"
log "nfqws установлен: $INSTALL_BIN"
"$INSTALL_BIN" --version 2>&1 | head -1 || true

# ---------------------------------------------------------------------------
# 4. Загрузить ядерный модуль
# ---------------------------------------------------------------------------
log "Загрузка nfnetlink_queue..."
modprobe nfnetlink_queue
echo "nfnetlink_queue" >> /etc/modules-load.d/zapret.conf || true

# ---------------------------------------------------------------------------
# 5. Установить зависимости Python для probe.py
# ---------------------------------------------------------------------------
log "Проверка Python зависимостей..."
python3 -c "import asyncio, json, math, random, sqlite3" 2>/dev/null || \
    apt-get install -y -qq python3

# ---------------------------------------------------------------------------
# 6. Инициализация probe state
# ---------------------------------------------------------------------------
log "Инициализация probe..."
STATE_DIR="/opt/vpn/watchdog/plugins/zapret"
mkdir -p "$STATE_DIR"

# Запустить начальный quick probe (если watchdog запущен)
if systemctl is-active --quiet watchdog 2>/dev/null; then
    log "Watchdog запущен, запускаем начальный probe в фоне..."
    nohup python3 "$STATE_DIR/probe.py" quick > /var/log/zapret-probe.log 2>&1 &
    log "Probe запущен в фоне (PID $!). Лог: /var/log/zapret-probe.log"
fi

# ---------------------------------------------------------------------------
# 7. Добавить ночной cron для full probe (02:30)
# ---------------------------------------------------------------------------
CRON_FILE="/etc/cron.d/zapret-probe"
cat > "$CRON_FILE" << 'CRON_EOF'
# zapret adaptive probe — полный re-probe каждую ночь в 02:30
# Обновляет Thompson Sampling параметры под текущий DPI провайдера
# source .env чтобы подхватить NET_INTERFACE и другие переменные окружения
30 2 * * * root bash -c 'set -a; [ -f /opt/vpn/.env ] && . /opt/vpn/.env; set +a; exec /opt/vpn/watchdog/venv/bin/python3 /opt/vpn/watchdog/plugins/zapret/probe.py full' >> /var/log/zapret-probe.log 2>&1
CRON_EOF
chmod 644 "$CRON_FILE"
log "Ночной cron добавлен: $CRON_FILE"

# ---------------------------------------------------------------------------
# 8. Добавить zapret стек в watchdog (если ещё не добавлен)
# ---------------------------------------------------------------------------
log ""
log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "✓ Установка zapret завершена!"
log ""
log "Следующий шаг:"
log "  1. Перезапустить watchdog: systemctl restart watchdog"
log "  2. Проверить probe: python3 /opt/vpn/watchdog/plugins/zapret/probe.py status"
log "  3. Запустить full probe вручную (опционально):"
log "     python3 /opt/vpn/watchdog/plugins/zapret/probe.py full"
log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

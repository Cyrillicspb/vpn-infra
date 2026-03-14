#!/bin/bash
# install.sh — установка zapret (nfqws) на домашний сервер Ubuntu 24.04
# Запускать: bash install.sh
set -euo pipefail

NFQWS_VERSION="67"   # Последняя стабильная версия zapret
INSTALL_BIN="/usr/local/bin/nfqws"
ARCH="$(uname -m)"

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
# 3. Загрузить zapret binary
# ---------------------------------------------------------------------------
ZAPRET_URL="https://github.com/bol-van/zapret/releases/latest/download/zapret-${BINARY_ARCH}.tar.gz"
TMP_DIR="$(mktemp -d)"
trap "rm -rf $TMP_DIR" EXIT

log "Загрузка zapret ${BINARY_ARCH}..."

# Попытка скачать через VPS tunnel (GitHub может быть заблокирован)
if curl -sSfL --connect-timeout 15 -o "$TMP_DIR/zapret.tar.gz" "$ZAPRET_URL" 2>/dev/null; then
    log "Загружено напрямую"
elif curl -sSfL --connect-timeout 15 \
     --interface "$(ip route show table 200 | grep default | awk '{print $5}')" \
     -o "$TMP_DIR/zapret.tar.gz" "$ZAPRET_URL" 2>/dev/null; then
    log "Загружено через туннель"
else
    # Fallback: собрать из исходников
    log "Прямая загрузка недоступна, сборка из исходников..."
    apt-get install -y -qq build-essential libnetfilter-queue-dev libmnl-dev git
    git clone --depth=1 https://github.com/bol-van/zapret.git "$TMP_DIR/zapret-src" 2>/dev/null || \
    git clone --depth=1 http://github.com/bol-van/zapret.git "$TMP_DIR/zapret-src"
    cd "$TMP_DIR/zapret-src/nfq"
    make -j"$(nproc)"
    cp nfqws "$INSTALL_BIN"
    cd /
fi

# Если скачали архив — распаковать
if [ -f "$TMP_DIR/zapret.tar.gz" ]; then
    tar -xzf "$TMP_DIR/zapret.tar.gz" -C "$TMP_DIR"
    NFQWS_BINARY="$(find "$TMP_DIR" -name "nfqws" -type f | head -1)"
    if [ -z "$NFQWS_BINARY" ]; then
        err "nfqws бинарник не найден в архиве"
    fi
    cp "$NFQWS_BINARY" "$INSTALL_BIN"
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
30 2 * * * root /opt/vpn/watchdog/venv/bin/python3 /opt/vpn/watchdog/plugins/zapret/probe.py full >> /var/log/zapret-probe.log 2>&1
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

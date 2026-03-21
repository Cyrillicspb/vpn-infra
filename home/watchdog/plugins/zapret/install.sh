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
# Приоритет: 1) бандленный из репо  →  2) GitHub Release  →  3) сборка из исходников
# Бандленные бинарники включены в репо чтобы не зависеть от доступности GitHub (заблокирован в РФ).

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

# 3.2 Fallback: скачать с GitHub (если доступен)
if [[ $NFQWS_INSTALLED -eq 0 ]]; then
    TMP_DIR="$(mktemp -d)"
    trap "rm -rf $TMP_DIR" EXIT

    log "Бандленный бинарник не найден, пробуем GitHub..."

    _curl_maybe_tunnel() {
        local url="$1" out="$2"
        local tun_iface
        if curl -sSfL --connect-timeout 15 -o "$out" "$url" 2>/dev/null; then
            return 0
        fi
        tun_iface="$(ip route show table 200 2>/dev/null | grep default | awk '{print $5}' | head -1)"
        if [[ -n "$tun_iface" ]] && curl -sSfL --connect-timeout 15 \
             --interface "$tun_iface" -o "$out" "$url" 2>/dev/null; then
            return 0
        fi
        return 1
    }

    RELEASE_URL=""
    if _curl_maybe_tunnel "https://api.github.com/repos/bol-van/zapret/releases/latest" \
            "$TMP_DIR/release.json" 2>/dev/null; then
        RELEASE_URL=$(python3 -c "
import json
d = json.load(open('$TMP_DIR/release.json'))
for a in d.get('assets', []):
    if a['name'].endswith('.tar.gz') and 'openwrt' not in a['name']:
        print(a['browser_download_url'])
        break
" 2>/dev/null || true)
    fi

    [[ -z "$RELEASE_URL" ]] && RELEASE_URL="https://github.com/bol-van/zapret/archive/refs/heads/master.tar.gz"

    log "Загрузка zapret (${BINARY_ARCH}) с GitHub..."
    if _curl_maybe_tunnel "$RELEASE_URL" "$TMP_DIR/zapret.tar.gz"; then
        tar -xzf "$TMP_DIR/zapret.tar.gz" -C "$TMP_DIR"
        NFQWS_BINARY="$(find "$TMP_DIR" -path "*/binaries/linux-${BINARY_ARCH}/nfqws" -type f 2>/dev/null | head -1)"
        [[ -z "$NFQWS_BINARY" ]] && NFQWS_BINARY="$(find "$TMP_DIR" -name "nfqws" -type f 2>/dev/null | head -1)"
        if [[ -n "$NFQWS_BINARY" ]]; then
            cp "$NFQWS_BINARY" "$INSTALL_BIN"
            chmod +x "$INSTALL_BIN"
            NFQWS_INSTALLED=1
            log "Бинарник установлен из GitHub"
        fi
    fi
fi

# 3.3 Fallback: сборка из исходников
if [[ $NFQWS_INSTALLED -eq 0 ]]; then
    log "GitHub недоступен, собираем из исходников..."
    apt-get install -y -qq build-essential libnetfilter-queue-dev libmnl-dev git
    TMP_SRC="$(mktemp -d)"
    trap "rm -rf $TMP_SRC" EXIT
    git clone --depth=1 "https://github.com/bol-van/zapret.git" "$TMP_SRC/zapret-src" || \
        err "Не удалось скачать исходники zapret (нет доступа к GitHub)"
    cd "$TMP_SRC/zapret-src/nfq"
    make -j"$(nproc)"
    cp nfqws "$INSTALL_BIN"
    chmod +x "$INSTALL_BIN"
    cd /
    log "Собрано из исходников"
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

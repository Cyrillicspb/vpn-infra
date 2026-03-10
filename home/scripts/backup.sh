#!/bin/bash
# =============================================================================
# backup.sh — Резервное копирование VPN Infrastructure
# Cron: 0 4 * * * root bash /opt/vpn/scripts/backup.sh
# =============================================================================
set -euo pipefail

BACKUP_DIR="/opt/vpn/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="vpn-backup-${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
MAX_SIZE_MB=50  # Лимит Telegram
RETENTION_DAYS=30

source /opt/vpn/.env 2>/dev/null || true

log() { echo "[$(date '+%H:%M:%S')] $*"; }

mkdir -p "$BACKUP_DIR"
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

log "Начало бэкапа..."

# ---------------------------------------------------------------------------
# 1. WireGuard ключи и конфиги
# ---------------------------------------------------------------------------
log "WireGuard ключи..."
mkdir -p "$TMP_DIR/wireguard"
cp -r /etc/wireguard/. "$TMP_DIR/wireguard/" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. .env
# ---------------------------------------------------------------------------
log ".env файлы..."
cp /opt/vpn/.env "$TMP_DIR/.env" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. SQLite база данных бота (консистентная копия)
# ---------------------------------------------------------------------------
log "База данных..."
if [[ -f /opt/vpn/telegram-bot/data/vpn_bot.db ]]; then
    sqlite3 /opt/vpn/telegram-bot/data/vpn_bot.db ".backup $TMP_DIR/vpn_bot.db"
fi

# ---------------------------------------------------------------------------
# 4. nftables конфиги
# ---------------------------------------------------------------------------
log "nftables..."
cp /etc/nftables.conf "$TMP_DIR/" 2>/dev/null || true
cp /etc/nftables-blocked-static.conf "$TMP_DIR/" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Hysteria конфиг
# ---------------------------------------------------------------------------
log "Hysteria2..."
mkdir -p "$TMP_DIR/hysteria"
cp /etc/hysteria/config.yaml "$TMP_DIR/hysteria/" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 6. Маршруты
# ---------------------------------------------------------------------------
log "Маршруты..."
mkdir -p "$TMP_DIR/vpn-routes"
cp -r /etc/vpn-routes/. "$TMP_DIR/vpn-routes/" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Архивируем
# ---------------------------------------------------------------------------
log "Архивирование..."
tar -czf "${BACKUP_PATH}.tar.gz" -C "$TMP_DIR" .

# Проверяем размер
SIZE_MB=$(du -m "${BACKUP_PATH}.tar.gz" | cut -f1)
if [[ $SIZE_MB -gt $MAX_SIZE_MB ]]; then
    log "WARN: Бэкап ${SIZE_MB}MB > ${MAX_SIZE_MB}MB (лимит Telegram)"
fi

# ---------------------------------------------------------------------------
# GPG шифрование
# ---------------------------------------------------------------------------
log "Шифрование..."
if [[ -n "${BACKUP_GPG_PASSPHRASE:-}" ]]; then
    gpg --batch --yes --passphrase "$BACKUP_GPG_PASSPHRASE" \
        --symmetric --cipher-algo AES256 \
        --output "${BACKUP_PATH}.tar.gz.gpg" \
        "${BACKUP_PATH}.tar.gz"
    rm -f "${BACKUP_PATH}.tar.gz"
    FINAL_FILE="${BACKUP_PATH}.tar.gz.gpg"
else
    log "WARN: GPG пароль не задан, бэкап не зашифрован"
    FINAL_FILE="${BACKUP_PATH}.tar.gz"
fi

log "Бэкап создан: ${FINAL_FILE}"

# ---------------------------------------------------------------------------
# Отправка на VPS
# ---------------------------------------------------------------------------
if [[ -n "${BACKUP_VPS_HOST:-}" ]]; then
    log "Отправка на VPS..."
    scp -o StrictHostKeyChecking=no \
        "$FINAL_FILE" \
        "${BACKUP_VPS_USER:-sysadmin}@${BACKUP_VPS_HOST}:/opt/vpn/backups/" \
        && log "OK: Отправлено на VPS" \
        || log "WARN: Не удалось отправить на VPS"
fi

# ---------------------------------------------------------------------------
# Отправка в Telegram
# ---------------------------------------------------------------------------
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
    log "Отправка в Telegram..."
    FINAL_SIZE=$(du -k "$FINAL_FILE" | cut -f1)
    if [[ $FINAL_SIZE -lt $((MAX_SIZE_MB * 1024)) ]]; then
        curl -s -F "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
            -F "document=@${FINAL_FILE}" \
            -F "caption=🔒 VPN Backup ${TIMESTAMP}" \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
            > /dev/null && log "OK: Отправлено в Telegram" \
            || log "WARN: Не удалось отправить в Telegram"
    else
        log "WARN: Файл слишком большой для Telegram (${FINAL_SIZE}KB)"
    fi
fi

# ---------------------------------------------------------------------------
# Ротация старых бэкапов
# ---------------------------------------------------------------------------
log "Ротация бэкапов (> ${RETENTION_DAYS} дней)..."
find "$BACKUP_DIR" -name "vpn-backup-*" -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true

log "Бэкап завершён: ${FINAL_FILE}"

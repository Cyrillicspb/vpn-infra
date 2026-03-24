#!/bin/bash
# =============================================================================
# backup.sh — Резервное копирование VPN Infrastructure
#
# Использование:
#   bash backup.sh              — полный бэкап
#   bash backup.sh --dry-run    — показать что войдёт в бэкап, не создавать
#   bash backup.sh --no-upload  — только локальный бэкап, не отправлять
#
# Cron: 0 4 * * * root bash /opt/vpn/scripts/backup.sh >> /var/log/vpn-backup.log 2>&1
#
# Состав бэкапа:
#   /etc/wireguard/               — WireGuard ключи и серверные конфиги
#   /opt/vpn/.env                 — все секреты (chmod 600)
#   vpn_bot.db (sqlite3 .backup)  — консистентная копия SQLite
#   /etc/nftables*.conf           — правила и blocked_static set
#   /etc/hysteria/config.yaml     — Hysteria2 клиентский конфиг
#   /opt/vpn/home/xray/*.json     — Xray клиентские конфиги
#   /opt/vpn/home/dnsmasq/        — dnsmasq конфиги с nftset= директивами
#   /etc/vpn-routes/manual-*.txt  — ручные маршруты (VPN и direct)
#   /opt/vpn/watchdog/plugins/    — плагины стеков
#   metadata.json                 — метаданные бэкапа (время, версия, sha256)
#
# Назначения:
#   1. Локально: /opt/vpn/backups/ (30 дней ротация)
#   2. VPS: scp → /opt/vpn/backups/ (ротация 30 дней на VPS стороне)
#   3. Telegram: sendDocument (лимит 50 МБ)
#
# Гарантия размера ≤ 50 MB (ключи + конфиги, без данных Grafana/Prometheus)
# =============================================================================
set -euo pipefail

# ── Константы ─────────────────────────────────────────────────────────────────
BACKUP_DIR="/opt/vpn/backups"
MAX_SIZE_MB=45          # Лимит < 50 МБ (Telegram)
RETENTION_DAYS=30
VPS_BACKUP_DIR="/opt/vpn/backups"
SSH_KEY="/root/.ssh/vpn_id_ed25519"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
if [[ "$FULL_EXPORT" == "true" ]]; then
    BACKUP_NAME="vpn-export-${TIMESTAMP}"
else
    BACKUP_NAME="vpn-backup-${TIMESTAMP}"
fi

# ── Флаги ─────────────────────────────────────────────────────────────────────
DRY_RUN=false
NO_UPLOAD=false
FULL_EXPORT=false
HAS_MTLS=false
for arg in "$@"; do
    case "$arg" in
        --dry-run)     DRY_RUN=true ;;
        --no-upload)   NO_UPLOAD=true ;;
        --full-export) FULL_EXPORT=true ;;
    esac
done

# ── Логирование ───────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] BACKUP: $*"; }
log_ok()   { echo "[$(date '+%H:%M:%S')] BACKUP: ✓ $*"; }
log_warn() { echo "[$(date '+%H:%M:%S')] BACKUP: ! $*" >&2; }

# ── Загрузка .env ─────────────────────────────────────────────────────────────
ENV_FILE="/opt/vpn/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
fi

# ── Telegram уведомление ──────────────────────────────────────────────────────
notify() {
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        --data-urlencode "text=$1" \
        -d "parse_mode=Markdown" \
        > /dev/null 2>&1 || true
}

notify_file() {
    local file="$1" caption="$2"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    curl -sf --max-time 60 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
        -F "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        -F "document=@${file}" \
        -F "caption=${caption}" \
        > /dev/null 2>&1 || true
}

# ── Dry-run ───────────────────────────────────────────────────────────────────
if $DRY_RUN; then
    echo "═══ Dry-run: состав бэкапа ═══"
    for item in \
        "/etc/wireguard" \
        "$ENV_FILE" \
        "/opt/vpn/telegram-bot/data/vpn_bot.db" \
        "/etc/nftables.conf" \
        "/etc/nftables-blocked-static.conf" \
        "/etc/hysteria/config.yaml" \
        "/opt/vpn/home/xray" \
        "/opt/vpn/home/dnsmasq" \
        "/etc/vpn-routes" \
        "/opt/vpn/watchdog/plugins"
    do
        if [[ -e "$item" ]]; then
            size=$(du -sh "$item" 2>/dev/null | cut -f1)
            echo "  [${size}] $item"
        else
            echo "  [нет]   $item"
        fi
    done
    echo "══════════════════════════════"
    exit 0
fi

# ── Подготовка ────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log "Начало бэкапа (${TIMESTAMP})"

# ── 1. WireGuard ключи и серверные конфиги ────────────────────────────────────
log "WireGuard..."
if [[ -d /etc/wireguard ]]; then
    mkdir -p "$TMP_DIR/wireguard"
    cp -r /etc/wireguard/. "$TMP_DIR/wireguard/"
    # Права на приватные ключи
    chmod 600 "$TMP_DIR/wireguard/"*.key 2>/dev/null || true
    log_ok "WireGuard: $(ls "$TMP_DIR/wireguard/" | wc -l) файлов"
else
    log_warn "WireGuard: /etc/wireguard не найден"
fi

# ── 2. .env ───────────────────────────────────────────────────────────────────
log ".env..."
if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$TMP_DIR/.env"
    chmod 600 "$TMP_DIR/.env"
    log_ok ".env скопирован"
fi

# ── 3. SQLite БД бота (sqlite3 .backup — консистентная копия) ────────────────
log "SQLite БД..."
DB_PATH="/opt/vpn/telegram-bot/data/vpn_bot.db"
if [[ -f "$DB_PATH" ]]; then
    # sqlite3 .backup использует SQLite backup API — безопасно при WAL-режиме
    sqlite3 "$DB_PATH" ".backup $TMP_DIR/vpn_bot.db"
    log_ok "SQLite: $(du -k "$TMP_DIR/vpn_bot.db" | cut -f1) КБ"
else
    log_warn "SQLite: $DB_PATH не найден"
fi

# ── 4. nftables правила ───────────────────────────────────────────────────────
log "nftables..."
cp /etc/nftables.conf "$TMP_DIR/nftables.conf" 2>/dev/null && log_ok "nftables.conf" || true
cp /etc/nftables-blocked-static.conf "$TMP_DIR/nftables-blocked-static.conf" 2>/dev/null \
    && log_ok "nftables-blocked-static.conf" || true

# ── 5. Hysteria2 клиентский конфиг ───────────────────────────────────────────
log "Hysteria2..."
if [[ -f /etc/hysteria/config.yaml ]]; then
    mkdir -p "$TMP_DIR/hysteria"
    cp /etc/hysteria/config.yaml "$TMP_DIR/hysteria/config.yaml"
    log_ok "hysteria config.yaml"
fi

# ── 6. Xray клиентские конфиги ────────────────────────────────────────────────
log "Xray..."
if [[ -d /opt/vpn/home/xray ]]; then
    mkdir -p "$TMP_DIR/xray"
    cp /opt/vpn/home/xray/*.json "$TMP_DIR/xray/" 2>/dev/null && \
        log_ok "Xray: $(ls "$TMP_DIR/xray/" 2>/dev/null | wc -l) файлов" || true
fi

# ── 7. dnsmasq конфиги (включая nftset= директивы) ───────────────────────────
log "dnsmasq..."
if [[ -d /opt/vpn/home/dnsmasq ]]; then
    mkdir -p "$TMP_DIR/dnsmasq"
    cp -r /opt/vpn/home/dnsmasq/. "$TMP_DIR/dnsmasq/"
    log_ok "dnsmasq конфиги"
fi

# ── 8. Ручные маршруты VPN/direct ────────────────────────────────────────────
log "vpn-routes..."
if [[ -d /etc/vpn-routes ]]; then
    mkdir -p "$TMP_DIR/vpn-routes"
    cp -r /etc/vpn-routes/. "$TMP_DIR/vpn-routes/"
    log_ok "vpn-routes: $(ls "$TMP_DIR/vpn-routes/" | wc -l) файлов"
fi

# ── 9. Watchdog плагины ───────────────────────────────────────────────────────
log "Watchdog plugins..."
if [[ -d /opt/vpn/watchdog/plugins ]]; then
    mkdir -p "$TMP_DIR/watchdog-plugins"
    cp -r /opt/vpn/watchdog/plugins/. "$TMP_DIR/watchdog-plugins/"
    log_ok "watchdog plugins"
fi

# ── 9а. Watchdog state.json ───────────────────────────────────────────────────
if [[ -f "/opt/vpn/watchdog/state.json" ]]; then
    cp "/opt/vpn/watchdog/state.json" "$TMP_DIR/watchdog-state.json" 2>/dev/null || true
    log_ok "watchdog state.json"
fi

# ── Full-export дополнительные данные ─────────────────────────────────────────
if [[ "$FULL_EXPORT" == "true" ]]; then
    log "Full export: дополнительные данные..."

    # mTLS CA с VPS (graceful degradation)
    if [[ -n "${VPS_IP:-}" && -n "${SSH_KEY:-}" ]]; then
        log "Копирование mTLS CA с VPS ${VPS_IP}..."
        mkdir -p "$TMP_DIR/mtls"
        if scp -P "${VPS_SSH_PORT:-22}" \
            -i "$SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=10 \
            -o BatchMode=yes \
            "sysadmin@${VPS_IP}:/opt/vpn/nginx/mtls/ca.key" \
            "sysadmin@${VPS_IP}:/opt/vpn/nginx/mtls/ca.crt" \
            "$TMP_DIR/mtls/" 2>/dev/null; then
            HAS_MTLS=true
            log_ok "mTLS CA скопирован"
        else
            log_warn "Не удалось скопировать mTLS CA с VPS (VPS недоступен?) — продолжаем без него"
            rmdir "$TMP_DIR/mtls" 2>/dev/null || true
        fi
    else
        log_warn "VPS_IP или SSH_KEY не заданы — mTLS CA пропущен"
    fi

    # DPI presets
    if [[ -f "/etc/vpn/dpi-presets.json" ]]; then
        cp "/etc/vpn/dpi-presets.json" "$TMP_DIR/dpi-presets.json" 2>/dev/null || true
        log_ok "DPI presets скопированы"
    fi

    # Cloudflared credentials
    if [[ -d "$HOME/.cloudflared" ]] && ls "$HOME/.cloudflared/"*.json &>/dev/null 2>&1; then
        mkdir -p "$TMP_DIR/cloudflared"
        cp "$HOME/.cloudflared/"*.json "$TMP_DIR/cloudflared/" 2>/dev/null || true
        log_ok "Cloudflared credentials скопированы"
    fi
fi

# ── 10. Метаданные бэкапа ─────────────────────────────────────────────────────
log "Метаданные..."
VPN_VERSION="$(cat /opt/vpn/version 2>/dev/null || echo 'unknown')"
ACTIVE_TUN="$(cat /run/vpn-active-tun 2>/dev/null || echo 'none')"
WG0_PEERS="$(wg show wg0 peers 2>/dev/null | wc -l || echo 0)"
WG1_PEERS="$(wg show wg1 peers 2>/dev/null | wc -l || echo 0)"
CLIENT_COUNT=0
if [[ -f "$DB_PATH" ]]; then
    CLIENT_COUNT="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM clients" 2>/dev/null || echo 0)"
fi
EXPORT_TYPE="backup"
[[ "$FULL_EXPORT" == "true" ]] && EXPORT_TYPE="full-export"
HAS_MTLS_JSON="false"
[[ "$HAS_MTLS" == "true" ]] && HAS_MTLS_JSON="true"

cat > "$TMP_DIR/metadata.json" << EOF
{
  "timestamp":      "${TIMESTAMP}",
  "hostname":       "$(hostname)",
  "vpn_version":    "${VPN_VERSION}",
  "kernel":         "$(uname -r)",
  "active_tun":     "${ACTIVE_TUN}",
  "wg0_peers":      ${WG0_PEERS},
  "wg1_peers":      ${WG1_PEERS},
  "export_type":    "${EXPORT_TYPE}",
  "client_count":   ${CLIENT_COUNT},
  "has_mtls":       ${HAS_MTLS_JSON},
  "home_server_ip": "${EXTERNAL_IP:-}",
  "created_at":     "$(date -Iseconds)"
}
EOF
log_ok "metadata.json"

# ── Архивируем ────────────────────────────────────────────────────────────────
log "Архивирование..."
ARCHIVE_PATH="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
tar -czf "$ARCHIVE_PATH" -C "$TMP_DIR" . 2>/dev/null
ARCHIVE_SIZE_MB="$(du -m "$ARCHIVE_PATH" | cut -f1)"
log_ok "Архив: ${ARCHIVE_SIZE_MB} МБ"

if (( ARCHIVE_SIZE_MB > MAX_SIZE_MB )); then
    log_warn "Архив ${ARCHIVE_SIZE_MB}МБ > ${MAX_SIZE_MB}МБ (лимит Telegram)"
    notify "⚠️ *Backup* слишком большой: ${ARCHIVE_SIZE_MB}МБ > ${MAX_SIZE_MB}МБ\nТelegram-отправка пропущена"
fi

# ── sha256 контрольная сумма ──────────────────────────────────────────────────
sha256sum "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"
log_ok "sha256: $(cut -c1-16 "${ARCHIVE_PATH}.sha256")..."

# ── GPG шифрование ───────────────────────────────────────────────────────────
FINAL_FILE="$ARCHIVE_PATH"
if [[ -n "${BACKUP_GPG_PASSPHRASE:-}" ]]; then
    log "GPG шифрование (AES256)..."
    ENCRYPTED_PATH="${ARCHIVE_PATH}.gpg"
    # --passphrase-fd: пароль через pipe (не через cmdline — не виден в ps)
    echo "$BACKUP_GPG_PASSPHRASE" | gpg --batch --yes \
        --passphrase-fd 0 \
        --symmetric \
        --cipher-algo AES256 \
        --s2k-digest-algo SHA512 \
        --s2k-count 65011712 \
        --output "$ENCRYPTED_PATH" \
        "$ARCHIVE_PATH" 2>/dev/null
    rm -f "$ARCHIVE_PATH"
    # sha256 зашифрованного файла
    sha256sum "$ENCRYPTED_PATH" > "${ENCRYPTED_PATH}.sha256"
    rm -f "${ARCHIVE_PATH}.sha256"
    FINAL_FILE="$ENCRYPTED_PATH"
    log_ok "GPG: зашифровано → $(basename "$FINAL_FILE")"
else
    log_warn "BACKUP_GPG_PASSPHRASE не задан — бэкап НЕ зашифрован!"
    notify "⚠️ *Backup* без шифрования — установите BACKUP_GPG_PASSPHRASE в .env"
fi

log_ok "Финальный файл: $(basename "$FINAL_FILE") ($(du -m "$FINAL_FILE" | cut -f1) МБ)"

# ── Отправка на VPS ──────────────────────────────────────────────────────────
if [[ "$NO_UPLOAD" == "false" && -n "${VPS_IP:-}" ]]; then
    log "Отправка на VPS ${VPS_IP}..."
    SSH_PORT="${VPS_SSH_PORT:-22}"
    SSH_PROXY_CMD="/opt/vpn/scripts/ssh-proxy.sh"
    SSH_PROXY_OPTS=()
    [[ -x "$SSH_PROXY_CMD" ]] && SSH_PROXY_OPTS=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")

    _backup_scp=false
    for _retry in 1 2 3; do
        if scp -P "$SSH_PORT" -i "$SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=30 \
            "${SSH_PROXY_OPTS[@]}" \
            "$FINAL_FILE" "${FINAL_FILE}.sha256" \
            "sysadmin@${VPS_IP}:${VPS_BACKUP_DIR}/" 2>/dev/null; then
            _backup_scp=true; break
        fi
        [[ $_retry -lt 3 ]] && { log_warn "scp retry ${_retry}/3..."; sleep 10; }
    done

    if [[ "$_backup_scp" == "true" ]]; then
        log_ok "Отправлено на VPS"
        # Ротация на VPS (оставить последние 30 дней)
        ssh -p "$SSH_PORT" -i "$SSH_KEY" \
            -o StrictHostKeyChecking=no -o BatchMode=yes \
            "${SSH_PROXY_OPTS[@]}" \
            "sysadmin@${VPS_IP}" \
            "find ${VPS_BACKUP_DIR} -name 'vpn-backup-*' -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true" \
            2>/dev/null || true
    else
        log_warn "Не удалось отправить на VPS"
        notify "⚠️ *Backup* — не удалось отправить на VPS ${VPS_IP}"
    fi
fi

# ── Отправка в Telegram ───────────────────────────────────────────────────────
if [[ "$NO_UPLOAD" == "false" && -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    FINAL_SIZE_MB="$(du -m "$FINAL_FILE" | cut -f1)"
    if (( FINAL_SIZE_MB <= MAX_SIZE_MB )); then
        log "Отправка в Telegram..."
        CAPTION="🔒 VPN Backup ${TIMESTAMP}
📦 Размер: ${FINAL_SIZE_MB} МБ
🖥 Хост: $(hostname)
📌 Версия: ${VPN_VERSION}
👥 AWG пиров: ${WG0_PEERS} | WG пиров: ${WG1_PEERS}"
        if notify_file "$FINAL_FILE" "$CAPTION"; then
            log_ok "Отправлено в Telegram"
        else
            log_warn "Не удалось отправить в Telegram"
        fi
    else
        log_warn "Файл ${FINAL_SIZE_MB}МБ — слишком большой для Telegram (лимит 50МБ)"
    fi
fi

# ── Ротация локальных бэкапов ─────────────────────────────────────────────────
log "Ротация (> ${RETENTION_DAYS} дней)..."
find "$BACKUP_DIR" -name "vpn-backup-*" -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true

# ── Итог ──────────────────────────────────────────────────────────────────────
log_ok "Бэкап завершён: $(basename "$FINAL_FILE")"

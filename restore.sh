#!/bin/bash
# =============================================================================
# restore.sh — Восстановление из бэкапа
# Использование: bash restore.sh <backup_file.tar.gz.gpg>
# =============================================================================
set -euo pipefail

BACKUP_FILE="${1:-}"
RESTORE_DIR="/tmp/vpn-restore-$$"
REPO_DIR="/opt/vpn"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $*"; }

[[ -z "$BACKUP_FILE" ]] && { log_error "Укажите файл бэкапа: bash restore.sh backup.tar.gz.gpg"; exit 1; }
[[ -f "$BACKUP_FILE" ]] || { log_error "Файл не найден: $BACKUP_FILE"; exit 1; }
[[ "$EUID" -eq 0 ]] || { log_error "Запустите с правами root: sudo bash restore.sh ..."; exit 1; }

# ---------------------------------------------------------------------------
# Шаг 1: Расшифровка
# ---------------------------------------------------------------------------
log_info "Расшифровка бэкапа..."
read -sp "GPG пароль: " GPG_PASS; echo ""

mkdir -p "$RESTORE_DIR"
gpg --batch --yes --passphrase "$GPG_PASS" \
    --output "$RESTORE_DIR/backup.tar.gz" \
    --decrypt "$BACKUP_FILE" || { log_error "Расшифровка не удалась — неверный пароль?"; rm -rf "$RESTORE_DIR"; exit 1; }

# ---------------------------------------------------------------------------
# Шаг 2: Распаковка
# ---------------------------------------------------------------------------
log_info "Распаковка..."
tar -xzf "$RESTORE_DIR/backup.tar.gz" -C "$RESTORE_DIR"
ls "$RESTORE_DIR"

# ---------------------------------------------------------------------------
# Шаг 3: Установка базовых компонентов (если не установлены)
# ---------------------------------------------------------------------------
log_info "Проверка установки..."
if [[ ! -d "$REPO_DIR" ]]; then
    log_info "Запуск установки..."
    # Клонируем репозиторий или используем файлы из бэкапа
    if [[ -d "$RESTORE_DIR/repo" ]]; then
        cp -r "$RESTORE_DIR/repo" "$REPO_DIR"
    else
        log_warn "Репозиторий не найден в бэкапе. Клонируйте вручную в /opt/vpn"
        exit 1
    fi
    bash "$REPO_DIR/install-home.sh"
fi

# ---------------------------------------------------------------------------
# Шаг 4: Восстановление конфигов
# ---------------------------------------------------------------------------
log_info "Восстановление конфигурации..."

# .env
[[ -f "$RESTORE_DIR/.env" ]] && {
    cp "$RESTORE_DIR/.env" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    log_ok ".env восстановлен"
}

# WireGuard ключи
[[ -d "$RESTORE_DIR/wireguard" ]] && {
    cp -r "$RESTORE_DIR/wireguard/." /etc/wireguard/
    chmod 700 /etc/wireguard
    chmod 600 /etc/wireguard/*.key 2>/dev/null || true
    log_ok "WireGuard ключи восстановлены"
}

# nftables
[[ -f "$RESTORE_DIR/nftables-blocked-static.conf" ]] && {
    cp "$RESTORE_DIR/nftables-blocked-static.conf" /etc/nftables-blocked-static.conf
    log_ok "nftables blocked_static восстановлен"
}

# SQLite
[[ -f "$RESTORE_DIR/vpn_bot.db" ]] && {
    mkdir -p "$REPO_DIR/telegram-bot/data"
    cp "$RESTORE_DIR/vpn_bot.db" "$REPO_DIR/telegram-bot/data/vpn_bot.db"
    log_ok "База данных бота восстановлена"
}

# Hysteria конфиг
[[ -f "$RESTORE_DIR/hysteria-config.yaml" ]] && {
    mkdir -p /etc/hysteria
    cp "$RESTORE_DIR/hysteria-config.yaml" /etc/hysteria/config.yaml
    log_ok "Hysteria2 конфиг восстановлен"
}

# ---------------------------------------------------------------------------
# Шаг 5: Перезапуск сервисов
# ---------------------------------------------------------------------------
log_info "Перезапуск сервисов..."
systemctl restart nftables 2>/dev/null || true
nft -f /etc/nftables-blocked-static.conf 2>/dev/null || true
systemctl restart dnsmasq 2>/dev/null || true
systemctl restart watchdog 2>/dev/null || true
cd "$REPO_DIR" && docker compose up -d --remove-orphans 2>/dev/null || true

# ---------------------------------------------------------------------------
# Очистка
# ---------------------------------------------------------------------------
rm -rf "$RESTORE_DIR"

log_ok "Восстановление завершено!"
log_info "Проверьте работоспособность: bash /opt/vpn/tests/run-smoke-tests.sh"

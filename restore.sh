#!/bin/bash
# =============================================================================
# restore.sh — Восстановление VPN Infrastructure из бэкапа
#
# Использование:
#   sudo bash restore.sh <backup.tar.gz.gpg>   — восстановление из бэкапа
#   sudo bash restore.sh <backup.tar.gz>        — без шифрования
#   sudo bash restore.sh --migrate-vps <IP>     — восстановить VPS из бэкапа
#   sudo bash restore.sh --list                 — список локальных бэкапов
#
# Алгоритм:
#   1. Расшифровать GPG (passphrase со stdin через --passphrase-fd)
#   2. Проверить sha256 (если .sha256 файл рядом)
#   3. Если /opt/vpn не установлен → клонировать репо + запустить install-home.sh
#   4. Overwrite конфигов в правильном порядке:
#      .env → WireGuard ключи → nftables → Hysteria2 → Xray → dnsmasq → БД → маршруты
#   5. Перезапустить сервисы в порядке загрузки
#   6. Smoke-тесты
# =============================================================================
set -euo pipefail

# ── Константы ─────────────────────────────────────────────────────────────────
REPO_DIR="/opt/vpn"
ENV_FILE="$REPO_DIR/.env"
LOG_FILE="/var/log/vpn-restore.log"
RESTORE_TMP="$(mktemp -d)"
GITHUB_REPO="https://github.com/Cyrillicspb/vpn-infra.git"
SSH_KEY="/root/.ssh/vpn_id_ed25519"

# ── Очистка при выходе ────────────────────────────────────────────────────────
cleanup() { rm -rf "$RESTORE_TMP"; }
trap cleanup EXIT

# ── Логирование ───────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

_log() { echo -e "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
log_info()  { _log "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { _log "${GREEN}[✓]${NC}    $*"; }
log_warn()  { _log "${YELLOW}[!]${NC}    $*"; }
log_error() { _log "${RED}[✗]${NC}    $*"; }
log_step()  { _log "${CYAN}${BOLD}━━━ $* ━━━${NC}"; }

die() {
    log_error "$*"
    echo ""
    echo "  Восстановление прервано. Проверьте /var/log/vpn-restore.log"
    exit 1
}

# =============================================================================
# Список бэкапов
# =============================================================================
list_backups() {
    echo ""
    echo "── Локальные бэкапы (/opt/vpn/backups/) ──────────────────────"
    if [[ -d /opt/vpn/backups ]]; then
        ls -lhtr /opt/vpn/backups/vpn-backup-*.gpg 2>/dev/null || \
        ls -lhtr /opt/vpn/backups/vpn-backup-*.tar.gz 2>/dev/null || \
        echo "  (бэкапов нет)"
    else
        echo "  /opt/vpn/backups/ не существует"
    fi
    echo "──────────────────────────────────────────────────────────────"
}

# =============================================================================
# Миграция на новый VPS
# =============================================================================
migrate_vps() {
    local new_vps_ip="$1"
    log_step "Миграция на новый VPS ($new_vps_ip)"

    [[ -f "$ENV_FILE" ]] || die ".env не найден — восстановите домашний сервер сначала"
    # shellcheck disable=SC1090
    source "$ENV_FILE"

    local ssh_port="${VPS_SSH_PORT:-22}"

    vps_exec() {
        ssh -p "$ssh_port" -i "$SSH_KEY" \
            -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes \
            "sysadmin@${new_vps_ip}" "$@"
    }
    vps_copy() {
        scp -P "$ssh_port" -i "$SSH_KEY" \
            -o StrictHostKeyChecking=no -o ConnectTimeout=30 \
            "$@"
    }

    # Проверяем доступ
    vps_exec "echo ok" &>/dev/null || die "SSH к $new_vps_ip недоступен (порт $ssh_port)"
    log_ok "SSH к $new_vps_ip OK"

    # Копируем vps/ конфиги
    vps_exec "mkdir -p /opt/vpn"
    vps_copy -r "$REPO_DIR/vps/." "sysadmin@${new_vps_ip}:/opt/vpn/"

    # Формируем VPS .env
    local tmp_env; tmp_env="$(mktemp)"
    cat > "$tmp_env" << EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ADMIN_CHAT_ID=${TELEGRAM_ADMIN_CHAT_ID:-}
GRAFANA_PASSWORD=${GRAFANA_PASSWORD:-changeme}
XRAY_PANEL_PASSWORD=${XRAY_PANEL_PASSWORD:-}
CF_TUNNEL_TOKEN=${CF_TUNNEL_TOKEN:-}
CF_WORKER_HOSTNAME=${CF_WORKER_HOSTNAME:-}
CF_CDN_UUID=${CF_CDN_UUID:-}
XRAY_UUID=${XRAY_UUID:-}
XRAY_GRPC_UUID=${XRAY_GRPC_UUID:-}
XRAY_PRIVATE_KEY=${XRAY_PRIVATE_KEY:-}
XRAY_GRPC_PRIVATE_KEY=${XRAY_GRPC_PRIVATE_KEY:-}
XRAY_PUBLIC_KEY=${XRAY_PUBLIC_KEY:-}
XRAY_GRPC_PUBLIC_KEY=${XRAY_GRPC_PUBLIC_KEY:-}
XHTTP_MS_PASSWORD=${XHTTP_MS_PASSWORD:-}
XHTTP_CDN_PASSWORD=${XHTTP_CDN_PASSWORD:-}
HYSTERIA2_AUTH=${HYSTERIA2_AUTH:-}
HYSTERIA2_OBFS_PASSWORD=${HYSTERIA2_OBFS_PASSWORD:-}
WATCHDOG_API_TOKEN=${WATCHDOG_API_TOKEN:-}
VPS_IP=${new_vps_ip}
VPS_TUNNEL_IP=${VPS_TUNNEL_IP:-10.177.2.2}
HOME_TUNNEL_IP=${HOME_TUNNEL_IP:-10.177.2.1}
HOME_SERVER_IP=${HOME_SERVER_IP:-}
SSH_ADDITIONAL_PORT=443
EOF
    vps_copy "$tmp_env" "sysadmin@${new_vps_ip}:/opt/vpn/.env"
    vps_exec "chmod 600 /opt/vpn/.env"
    rm -f "$tmp_env"

    # Генерируем mTLS CA на новом VPS если нет
    vps_exec "mkdir -p /opt/vpn/nginx/mtls /opt/vpn/nginx/ssl && \
        [ -f /opt/vpn/nginx/mtls/ca.crt ] || ( \
        openssl genrsa -out /opt/vpn/nginx/mtls/ca.key 4096 2>/dev/null && \
        openssl req -new -x509 -days 3650 \
            -key /opt/vpn/nginx/mtls/ca.key \
            -out /opt/vpn/nginx/mtls/ca.crt \
            -subj '/CN=VPN-CA/O=VPNInfra' 2>/dev/null && \
        chmod 600 /opt/vpn/nginx/mtls/ca.key )"

    # Запускаем Docker Compose
    vps_exec "cd /opt/vpn && docker compose pull --quiet 2>/dev/null || true"
    vps_exec "cd /opt/vpn && docker compose up -d --remove-orphans"

    # Обновляем VPS_IP в .env домашнего сервера
    sed -i "s|^VPS_IP=.*|VPS_IP=${new_vps_ip}|" "$ENV_FILE"
    log_ok "VPS_IP обновлён → $new_vps_ip"

    log_ok "Миграция на $new_vps_ip завершена"
    log_info "Следующий шаг: настройте Tier-2 туннель с новым VPS"
    log_info "  bash /opt/vpn/scripts/setup-tier2.sh $new_vps_ip"
}

# =============================================================================
# Расшифровка бэкапа
# =============================================================================
decrypt_backup() {
    local file="$1"

    if [[ "$file" == *.gpg ]]; then
        log_step "Расшифровка GPG"
        local pass

        # Пробуем взять из .env
        if [[ -n "${BACKUP_GPG_PASSPHRASE:-}" ]]; then
            pass="$BACKUP_GPG_PASSPHRASE"
            log_info "GPG пароль из .env"
        else
            read -rsp "GPG пароль для расшифровки: " pass; echo
        fi

        local decrypted="$RESTORE_TMP/backup.tar.gz"
        # --passphrase-fd: пароль через pipe (не через cmdline)
        echo "$pass" | gpg --batch --yes \
            --passphrase-fd 0 \
            --output "$decrypted" \
            --decrypt "$file" 2>/dev/null \
            || die "Расшифровка не удалась — неверный пароль?"
        log_ok "Расшифровано → backup.tar.gz"
        echo "$decrypted"
    else
        echo "$file"
    fi
}

# =============================================================================
# Проверка sha256
# =============================================================================
verify_checksum() {
    local file="$1"
    local sha_file="${file}.sha256"

    if [[ -f "$sha_file" ]]; then
        log_info "Проверка sha256..."
        if sha256sum --check "$sha_file" --status 2>/dev/null; then
            log_ok "sha256 OK"
        else
            log_warn "sha256 не совпадает — бэкап может быть повреждён"
            read -rp "Продолжить несмотря на ошибку контрольной суммы? (y/N): " yn
            [[ "$yn" == "y" ]] || die "Восстановление отменено из-за ошибки контрольной суммы"
        fi
    else
        log_warn "Файл .sha256 не найден — пропускаем проверку"
    fi
}

# =============================================================================
# Полная установка (если /opt/vpn не существует)
# =============================================================================
fresh_install_if_needed() {
    if [[ ! -f "$REPO_DIR/install-home.sh" ]]; then
        log_step "Первичная установка (репозиторий не найден)"
        log_info "Клонирование репозитория..."

        # Пробуем клонировать из GitHub
        if git clone "$GITHUB_REPO" "$REPO_DIR" 2>/dev/null; then
            log_ok "Репозиторий склонирован"
        else
            log_warn "GitHub недоступен — копируем из бэкапа"
            # repo/ может быть включён в бэкап (опционально)
            if [[ -d "$RESTORE_TMP/repo" ]]; then
                cp -r "$RESTORE_TMP/repo/." "$REPO_DIR/"
                log_ok "Репозиторий восстановлен из бэкапа"
            else
                die "Не удалось получить репозиторий. Склонируйте вручную:\n  git clone $GITHUB_REPO $REPO_DIR"
            fi
        fi

        # Устанавливаем .env из бэкапа ДО install-home.sh
        if [[ -f "$RESTORE_TMP/.env" ]]; then
            cp "$RESTORE_TMP/.env" "$ENV_FILE"
            chmod 600 "$ENV_FILE"
            log_ok ".env установлен из бэкапа"
        fi

        log_info "Запуск install-home.sh..."
        bash "$REPO_DIR/install-home.sh" || die "install-home.sh завершился с ошибкой"
    fi
}

# =============================================================================
# Восстановление конфигов из бэкапа
# =============================================================================
restore_configs() {
    log_step "Восстановление конфигурации"
    local src="$RESTORE_TMP"

    # ── .env (первым — содержит секреты для следующих шагов) ──────────────────
    if [[ -f "$src/.env" ]]; then
        cp "$src/.env" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        log_ok ".env"
        # shellcheck disable=SC1090
        source "$ENV_FILE"
    fi

    # ── WireGuard ключи и конфиги ─────────────────────────────────────────────
    if [[ -d "$src/wireguard" ]]; then
        mkdir -p /etc/wireguard
        chmod 700 /etc/wireguard
        cp -r "$src/wireguard/." /etc/wireguard/
        chmod 600 /etc/wireguard/*.key 2>/dev/null || true
        # Убеждаемся что конфиги защищены
        chmod 600 /etc/wireguard/*.conf 2>/dev/null || true
        log_ok "WireGuard ключи и конфиги"
    fi

    # ── nftables правила ──────────────────────────────────────────────────────
    if [[ -f "$src/nftables.conf" ]]; then
        cp "$src/nftables.conf" /etc/nftables.conf
        log_ok "nftables.conf"
    fi
    if [[ -f "$src/nftables-blocked-static.conf" ]]; then
        cp "$src/nftables-blocked-static.conf" /etc/nftables-blocked-static.conf
        log_ok "nftables-blocked-static.conf"
    fi

    # ── Hysteria2 клиентский конфиг ───────────────────────────────────────────
    if [[ -f "$src/hysteria/config.yaml" ]]; then
        mkdir -p /etc/hysteria
        cp "$src/hysteria/config.yaml" /etc/hysteria/config.yaml
        chmod 600 /etc/hysteria/config.yaml
        log_ok "hysteria config.yaml"
    fi

    # ── Xray клиентские конфиги ───────────────────────────────────────────────
    if [[ -d "$src/xray" ]]; then
        mkdir -p "$REPO_DIR/home/xray"
        cp -r "$src/xray/." "$REPO_DIR/home/xray/"
        log_ok "Xray конфиги"
    fi

    # ── dnsmasq конфиги ───────────────────────────────────────────────────────
    if [[ -d "$src/dnsmasq" ]]; then
        mkdir -p /etc/dnsmasq.d
        cp -r "$src/dnsmasq/." "$REPO_DIR/home/dnsmasq/"
        # Обновляем /etc/dnsmasq.d
        [[ -f "$REPO_DIR/home/dnsmasq/dnsmasq.conf" ]] && \
            cp "$REPO_DIR/home/dnsmasq/dnsmasq.conf" /etc/dnsmasq.conf
        find "$REPO_DIR/home/dnsmasq/dnsmasq.d/" -name "*.conf" -exec \
            cp {} /etc/dnsmasq.d/ \; 2>/dev/null || true
        log_ok "dnsmasq конфиги"
    fi

    # ── SQLite БД бота ────────────────────────────────────────────────────────
    if [[ -f "$src/vpn_bot.db" ]]; then
        local db_dir="$REPO_DIR/telegram-bot/data"
        mkdir -p "$db_dir"
        # Если БД уже существует — создаём резерв
        [[ -f "$db_dir/vpn_bot.db" ]] && \
            cp "$db_dir/vpn_bot.db" "$db_dir/vpn_bot.db.pre-restore"
        cp "$src/vpn_bot.db" "$db_dir/vpn_bot.db"
        log_ok "SQLite БД бота"
    fi

    # ── Ручные маршруты ───────────────────────────────────────────────────────
    if [[ -d "$src/vpn-routes" ]]; then
        mkdir -p /etc/vpn-routes
        cp -r "$src/vpn-routes/." /etc/vpn-routes/
        log_ok "vpn-routes (manual-vpn.txt, manual-direct.txt)"
    fi

    # ── Watchdog плагины ──────────────────────────────────────────────────────
    if [[ -d "$src/watchdog-plugins" ]]; then
        mkdir -p "$REPO_DIR/watchdog/plugins"
        cp -r "$src/watchdog-plugins/." "$REPO_DIR/watchdog/plugins/"
        log_ok "watchdog plugins"
    fi
}

# =============================================================================
# Перезапуск сервисов в правильном порядке (как в CLAUDE.md)
# =============================================================================
restart_services() {
    log_step "Перезапуск сервисов"

    # 1. nftables (правила + sets)
    systemctl restart nftables 2>/dev/null && log_ok "nftables" || log_warn "nftables restart failed"

    # 2. vpn-sets-restore (загружаем blocked_static)
    nft -f /etc/nftables-blocked-static.conf 2>/dev/null && log_ok "blocked_static" || log_warn "blocked_static load failed"

    # 3. WireGuard интерфейсы (если конфиги есть)
    for wg in wg0 wg1; do
        if [[ -f "/etc/wireguard/${wg}.conf" ]]; then
            systemctl restart "wg-quick@${wg}" 2>/dev/null && log_ok "${wg}" || log_warn "${wg} restart failed"
        fi
    done

    # 4. Policy routing
    if systemctl is-enabled vpn-routes &>/dev/null; then
        systemctl restart vpn-routes 2>/dev/null && log_ok "vpn-routes" || true
    fi

    # 5. dnsmasq
    systemctl restart dnsmasq 2>/dev/null && log_ok "dnsmasq" || log_warn "dnsmasq restart failed"

    # 6. Hysteria2
    if [[ -f /etc/hysteria/config.yaml ]]; then
        systemctl restart hysteria2 2>/dev/null && log_ok "hysteria2" || log_warn "hysteria2 restart failed"
    fi

    # 7. Docker Compose (telegram-bot, xray, cloudflared)
    if command -v docker &>/dev/null && [[ -f "$REPO_DIR/docker-compose.yml" ]]; then
        (cd "$REPO_DIR" && docker compose up -d --remove-orphans 2>/dev/null) && \
            log_ok "Docker Compose" || log_warn "Docker Compose failed"
    fi

    # 8. Watchdog (последним — зависит от всего)
    systemctl restart watchdog 2>/dev/null && log_ok "watchdog" || log_warn "watchdog restart failed"

    log_info "Ожидание стабилизации (10с)..."
    sleep 10
}

# =============================================================================
# Smoke-тесты после восстановления
# =============================================================================
run_smoke_tests() {
    log_step "Проверка работоспособности"
    local test_script="$REPO_DIR/tests/run-smoke-tests.sh"
    if [[ ! -f "$test_script" ]]; then
        log_warn "Smoke-тесты не найдены — пропуск"
        return 0
    fi

    if timeout 120 bash "$test_script"; then
        log_ok "Smoke-тесты прошли"
    else
        log_warn "Некоторые smoke-тесты не прошли — проверьте логи"
        log_info "Запустите вручную: bash $test_script"
    fi
}

# =============================================================================
# Main
# =============================================================================
main() {
    [[ "$EUID" -eq 0 ]] || die "Запустите: sudo bash restore.sh ..."

    mkdir -p "$(dirname "$LOG_FILE")"
    echo "" >> "$LOG_FILE"
    echo "════ Restore $(date '+%Y-%m-%d %H:%M:%S') ════" >> "$LOG_FILE"

    case "${1:-}" in
        --list)
            list_backups
            exit 0
            ;;
        --migrate-vps)
            [[ -n "${2:-}" ]] || die "Укажите IP нового VPS: --migrate-vps <IP>"
            # shellcheck disable=SC1090
            [[ -f "$ENV_FILE" ]] && source "$ENV_FILE" || true
            migrate_vps "$2"
            exit 0
            ;;
        "")
            die "Укажите файл бэкапа или флаг:\n  bash restore.sh <backup.tar.gz.gpg>\n  bash restore.sh --list\n  bash restore.sh --migrate-vps <IP>"
            ;;
    esac

    local backup_file="$1"
    [[ -f "$backup_file" ]] || die "Файл не найден: $backup_file"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║             VPN Infrastructure — Восстановление                 ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
    log_info "Файл бэкапа: $backup_file"
    log_info "Размер: $(du -sh "$backup_file" | cut -f1)"

    # Загружаем .env если есть (для GPG пароля и VPS IP)
    [[ -f "$ENV_FILE" ]] && {
        set -o allexport; source "$ENV_FILE"; set +o allexport
    } || true

    # Проверяем sha256 (на исходном зашифрованном файле)
    verify_checksum "$backup_file"

    # Расшифровываем
    local archive; archive="$(decrypt_backup "$backup_file")"

    # Распаковываем
    log_step "Распаковка"
    tar -xzf "$archive" -C "$RESTORE_TMP" 2>/dev/null || die "Ошибка распаковки"
    log_ok "Распаковано: $(ls "$RESTORE_TMP" | wc -l) объектов"

    # Показываем метаданные
    if [[ -f "$RESTORE_TMP/metadata.json" ]]; then
        log_info "Метаданные бэкапа:"
        python3 -c "
import json, sys
d = json.load(open('$RESTORE_TMP/metadata.json'))
print(f'  Создан:  {d.get(\"created_at\", \"?\")}')
print(f'  Версия:  {d.get(\"vpn_version\", \"?\")}')
print(f'  Хост:    {d.get(\"hostname\", \"?\")}')
print(f'  Пиров:   AWG={d.get(\"wg0_peers\", 0)} WG={d.get(\"wg1_peers\", 0)}')
" 2>/dev/null || cat "$RESTORE_TMP/metadata.json" | head -10
    fi

    # Подтверждение
    echo ""
    log_warn "ВНИМАНИЕ: будут перезаписаны текущие конфигурация и ключи!"
    read -rp "Продолжить восстановление? (yes/N): " confirm
    [[ "$confirm" == "yes" ]] || { log_info "Отменено пользователем"; exit 0; }

    # Устанавливаем базовые компоненты если нужно
    fresh_install_if_needed

    # Восстанавливаем конфиги
    restore_configs

    # Перезапускаем сервисы
    restart_services

    # Smoke-тесты
    run_smoke_tests

    echo ""
    log_ok "Восстановление завершено!"
    echo ""
    echo "  Следующие шаги:"
    echo "  • Проверьте состояние: systemctl status watchdog"
    echo "  • Логи:                journalctl -u watchdog -f"
    echo "  • Smoke-тесты:         bash $REPO_DIR/tests/run-smoke-tests.sh"
    echo ""
}

main "$@"

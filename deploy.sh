#!/bin/bash
# =============================================================================
# deploy.sh — Обновление VPN Infrastructure
# Получает обновления из git-зеркала на VPS (не напрямую из GitHub)
# =============================================================================
set -euo pipefail

REPO_DIR="/opt/vpn"
SNAPSHOT_DIR="/opt/vpn/.deploy-snapshot"
VPN_ENV="$REPO_DIR/.env"
LOG_FILE="/var/log/vpn-deploy.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*" | tee -a "$LOG_FILE"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}   $*" | tee -a "$LOG_FILE"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*" | tee -a "$LOG_FILE"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $*" | tee -a "$LOG_FILE"; }

source "$VPN_ENV" 2>/dev/null || true

notify_telegram() {
    local msg="$1"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}&text=${msg}&parse_mode=Markdown" \
        > /dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Функция: создание снапшота (для rollback)
# ---------------------------------------------------------------------------
create_snapshot() {
    log_info "Создание снапшота..."
    mkdir -p "$SNAPSHOT_DIR"
    SNAPSHOT_ID="$(date +%Y%m%d_%H%M%S)"
    SNAPSHOT_PATH="$SNAPSHOT_DIR/$SNAPSHOT_ID"
    mkdir -p "$SNAPSHOT_PATH"

    # Копируем важные файлы
    cp -r "$REPO_DIR/home" "$SNAPSHOT_PATH/" 2>/dev/null || true
    cp "$REPO_DIR/.env" "$SNAPSHOT_PATH/" 2>/dev/null || true
    sqlite3 /opt/vpn/telegram-bot/data/vpn_bot.db ".backup $SNAPSHOT_PATH/vpn_bot.db" 2>/dev/null || true

    # Сохраняем текущую версию
    CURRENT_VERSION=$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")
    echo "$CURRENT_VERSION" > "$SNAPSHOT_PATH/version"
    echo "$SNAPSHOT_ID" > "$SNAPSHOT_DIR/latest"

    log_ok "Снапшот создан: $SNAPSHOT_ID (версия $CURRENT_VERSION)"
}

# ---------------------------------------------------------------------------
# Функция: rollback
# ---------------------------------------------------------------------------
rollback() {
    local reason="${1:-Неизвестная причина}"
    log_error "Откат изменений: $reason"
    notify_telegram "⚠️ *Deploy failed* — автооткат\nПричина: $reason"

    if [[ ! -f "$SNAPSHOT_DIR/latest" ]]; then
        log_error "Снапшот не найден, откат невозможен"
        return 1
    fi

    SNAPSHOT_ID=$(cat "$SNAPSHOT_DIR/latest")
    SNAPSHOT_PATH="$SNAPSHOT_DIR/$SNAPSHOT_ID"

    log_info "Откат к снапшоту $SNAPSHOT_ID..."

    # Останавливаем сервисы
    systemctl stop watchdog 2>/dev/null || true
    cd /opt/vpn && docker compose down 2>/dev/null || true

    # Восстанавливаем файлы
    cp -r "$SNAPSHOT_PATH/home/." "$REPO_DIR/home/"
    cp "$SNAPSHOT_PATH/.env" "$REPO_DIR/.env" 2>/dev/null || true

    # Запускаем сервисы
    systemctl start watchdog 2>/dev/null || true
    cd /opt/vpn && docker compose up -d

    log_ok "Откат выполнен"
    notify_telegram "✅ Откат к $SNAPSHOT_ID выполнен"
}

# ---------------------------------------------------------------------------
# Функция: smoke-тест после деплоя
# ---------------------------------------------------------------------------
post_deploy_test() {
    log_info "Smoke-тест после деплоя..."
    if bash "$REPO_DIR/tests/run-smoke-tests.sh" > /tmp/deploy-test.log 2>&1; then
        log_ok "Smoke-тесты прошли"
        return 0
    else
        log_error "Smoke-тесты не прошли:"
        cat /tmp/deploy-test.log
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Функция: применение миграций
# ---------------------------------------------------------------------------
apply_migrations() {
    local migrations_dir="$REPO_DIR/migrations"
    [[ -d "$migrations_dir" ]] || return 0

    for migration in $(ls "$migrations_dir"/*.sh 2>/dev/null | sort); do
        local migration_name=$(basename "$migration" .sh)
        log_info "Применение миграции: $migration_name"
        bash "$migration" && log_ok "Миграция $migration_name выполнена" || \
            log_warn "Миграция $migration_name не выполнилась (возможно уже применена)"
    done
}

# ---------------------------------------------------------------------------
# Функция: git pull через SSH-туннель к VPS
# ---------------------------------------------------------------------------
git_pull_from_mirror() {
    source "$VPN_ENV" 2>/dev/null || true
    local vps_ip="${VPS_IP:-}"
    local ssh_port="${VPS_SSH_PORT:-22}"

    if [[ -z "$vps_ip" ]]; then
        log_warn "VPS_IP не задан, пробуем git pull напрямую..."
        git -C "$REPO_DIR" pull origin main 2>/dev/null || return 1
        return 0
    fi

    log_info "Pull из git-зеркала на VPS $vps_ip..."
    # Добавляем remote если нет
    git -C "$REPO_DIR" remote get-url vps-mirror 2>/dev/null || \
        git -C "$REPO_DIR" remote add vps-mirror \
            "ssh://sysadmin@$vps_ip:$ssh_port/opt/vpn/vpn-repo.git"

    git -C "$REPO_DIR" fetch vps-mirror 2>/dev/null || {
        log_warn "VPS недоступен, пробуем GitHub напрямую..."
        git -C "$REPO_DIR" fetch origin 2>/dev/null || return 1
    }
    git -C "$REPO_DIR" merge --ff-only FETCH_HEAD || return 1
}

# ---------------------------------------------------------------------------
# Функция: деплой на VPS
# ---------------------------------------------------------------------------
deploy_to_vps() {
    source "$VPN_ENV" 2>/dev/null || true
    local vps_ip="${VPS_IP:-}"
    [[ -z "$vps_ip" ]] && { log_warn "VPS_IP не задан, пропуск деплоя на VPS"; return 0; }

    local ssh_port="${VPS_SSH_PORT:-22}"
    log_info "Деплой на VPS $vps_ip..."

    ssh -p "$ssh_port" -o StrictHostKeyChecking=no "sysadmin@$vps_ip" \
        "cd /opt/vpn && git pull && docker compose pull && docker compose up -d --remove-orphans" \
        && log_ok "VPS обновлён" \
        || log_warn "Деплой на VPS не удался (retry вручную: /vps deploy)"
}

# ---------------------------------------------------------------------------
# Главный поток
# ---------------------------------------------------------------------------
main() {
    local force="${1:-}"

    echo "" | tee -a "$LOG_FILE"
    echo "=== Deploy $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"

    CURRENT_VERSION=$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")

    # Получаем обновление
    log_info "Получение обновлений..."
    git_pull_from_mirror || {
        log_warn "Обновление не получено"
        exit 0
    }

    NEW_VERSION=$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")

    if [[ "$CURRENT_VERSION" == "$NEW_VERSION" && "$force" != "--force" ]]; then
        log_info "Версия не изменилась ($CURRENT_VERSION), обновление не требуется"
        exit 0
    fi

    notify_telegram "🚀 Начало обновления $CURRENT_VERSION → $NEW_VERSION"
    log_info "Обновление: $CURRENT_VERSION → $NEW_VERSION"

    # Снапшот
    create_snapshot

    # Применяем миграции
    apply_migrations

    # Обновляем домашний сервер
    log_info "Обновление домашнего сервера..."
    WATCHDOG_NEEDS_RESTART=false

    # Если изменился watchdog.py — перезапускаем как отдельный процесс
    if git -C "$REPO_DIR" diff HEAD~1 -- home/watchdog/watchdog.py | grep -q .; then
        WATCHDOG_NEEDS_RESTART=true
    fi

    # Обновляем Docker образы
    cd "$REPO_DIR" && docker compose pull 2>/dev/null || true
    cd "$REPO_DIR" && docker compose up -d --remove-orphans

    # Перезапускаем watchdog если нужно
    if $WATCHDOG_NEEDS_RESTART; then
        log_info "Перезапуск watchdog..."
        # Деплой как отдельный процесс чтобы пережить рестарт watchdog
        systemctl restart watchdog &
    fi

    # Деплой на VPS
    deploy_to_vps

    # Smoke-тест
    sleep 5  # Ждём поднятия сервисов
    if ! post_deploy_test; then
        rollback "Smoke-тесты не прошли после деплоя"
        notify_telegram "❌ Deploy FAILED — откат выполнен"
        exit 1
    fi

    log_ok "Deploy завершён: $NEW_VERSION"
    notify_telegram "✅ Обновлено до $NEW_VERSION"
}

# Обработка аргументов
case "${1:-}" in
    --rollback) rollback "Ручной откат" ;;
    *)          main "$@" ;;
esac

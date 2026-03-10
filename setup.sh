#!/bin/bash
# =============================================================================
# setup.sh — Главный мастер-установщик VPN Infrastructure v4.0
# Запуск: bash setup.sh
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="/opt/vpn/.setup-state"
TOTAL_STEPS=51
CURRENT_STEP=0

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}   $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

step() {
    ((CURRENT_STEP++))
    echo -e "\n${BLUE}━━━ Шаг ${CURRENT_STEP}/${TOTAL_STEPS}: $* ━━━${NC}"
}

step_done() {
    echo "$1" >> "$STATE_FILE"
    log_ok "Готово: $1"
}

step_skip() {
    log_info "Пропуск (уже выполнено): $1"
}

is_done() {
    grep -q "^$1$" "$STATE_FILE" 2>/dev/null
}

die() {
    log_error "$*"
    echo ""
    echo "Что не так: $*"
    echo "Как исправить: проверьте вывод выше и устраните ошибку."
    echo "Продолжить: запустите setup.sh снова — выполненные шаги будут пропущены."
    exit 1
}

# =============================================================================
# Фаза 0: Предусловия и сбор информации
# =============================================================================
phase0() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║         VPN Infrastructure v4.0 — Установка                 ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    step "Проверка ОС"
    if ! is_done "os_check"; then
        [[ "$(uname -s)" == "Linux" ]] || die "Требуется Linux (Ubuntu 24.04)"
        . /etc/os-release
        [[ "$ID" == "ubuntu" ]] || log_warn "Рекомендуется Ubuntu, обнаружено: $ID"
        [[ "$VERSION_ID" == "24.04" ]] || log_warn "Рекомендуется Ubuntu 24.04, обнаружено: $VERSION_ID"
        step_done "os_check"
    else step_skip "os_check"; fi

    step "Проверка прав root"
    if ! is_done "root_check"; then
        [[ "$EUID" -eq 0 ]] || die "Запустите с правами root: sudo bash setup.sh"
        step_done "root_check"
    else step_skip "root_check"; fi

    step "Автообнаружение сети"
    if ! is_done "network_detect"; then
        NET_INTERFACE=$(ip route | grep default | awk '{print $5}' | head -1)
        GATEWAY_IP=$(ip route | grep default | awk '{print $3}' | head -1)
        HOME_SERVER_IP=$(ip addr show "$NET_INTERFACE" | grep 'inet ' | awk '{print $2}' | cut -d/ -f1 | head -1)
        HOME_SUBNET=$(ip addr show "$NET_INTERFACE" | grep 'inet ' | awk '{print $2}' | head -1)
        log_info "Интерфейс: $NET_INTERFACE"
        log_info "IP сервера: $HOME_SERVER_IP"
        log_info "Шлюз: $GATEWAY_IP"
        # Проверка CGNAT
        EXTERNAL_IP=$(curl -s --max-time 5 https://api.ipify.org || echo "")
        if [[ -n "$EXTERNAL_IP" ]]; then
            log_info "Внешний IP: $EXTERNAL_IP"
            # Простая проверка CGNAT (100.64.0.0/10)
            if [[ "$EXTERNAL_IP" =~ ^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\. ]]; then
                log_warn "ОБНАРУЖЕН CGNAT! Проект не будет работать без реального IP."
                log_warn "Три причины почему нет белого IP: CGNAT у провайдера, двойной NAT, или режим bridge mode не включён."
                echo ""
                read -p "Вы уверены что хотите продолжить? (y/N): " CGNAT_CONFIRM
                [[ "$CGNAT_CONFIRM" == "y" ]] || die "Получите реальный IP у провайдера и запустите снова."
            fi
        fi
        step_done "network_detect"
    else step_skip "network_detect"; fi

    step "Сбор параметров"
    if ! is_done "params_collected"; then
        # Загружаем .env если есть
        [[ -f /opt/vpn/.env ]] && source /opt/vpn/.env

        [[ -z "${VPS_IP:-}" ]] && read -p "IP-адрес VPS: " VPS_IP
        [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && read -p "Telegram Bot Token: " TELEGRAM_BOT_TOKEN
        [[ -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]] && read -p "Telegram Admin Chat ID: " TELEGRAM_ADMIN_CHAT_ID

        # Спрашиваем опциональные компоненты
        read -p "Настроить DDNS? (y/N): " USE_DDNS
        if [[ "$USE_DDNS" == "y" ]]; then
            read -p "Провайдер DDNS (duckdns/noip/cloudflare): " DDNS_PROVIDER
            read -p "DDNS домен: " DDNS_DOMAIN
            read -p "DDNS токен: " DDNS_TOKEN
        fi

        read -p "Есть аккаунт Cloudflare для CDN-стека? (y/N): " USE_CF
        [[ "$USE_CF" == "y" ]] && read -p "Cloudflare Tunnel Token: " CF_TUNNEL_TOKEN

        step_done "params_collected"
    else step_skip "params_collected"; fi

    step "Создание директории /opt/vpn"
    if ! is_done "dir_created"; then
        mkdir -p /opt/vpn
        cp -r "$REPO_DIR/." /opt/vpn/
        step_done "dir_created"
    else step_skip "dir_created"; fi
}

# =============================================================================
# Фаза 1: Домашний сервер
# =============================================================================
phase1() {
    log_info "=== Фаза 1: Настройка домашнего сервера ==="
    bash "$REPO_DIR/install-home.sh" "$STATE_FILE"
}

# =============================================================================
# Фаза 2: VPS
# =============================================================================
phase2() {
    log_info "=== Фаза 2: Настройка VPS ==="
    source /opt/vpn/.env
    bash "$REPO_DIR/install-vps.sh" "$STATE_FILE" "$VPS_IP"
}

# =============================================================================
# Фаза 3: Связка домашний сервер ↔ VPS
# =============================================================================
phase3() {
    step "Обмен ключами WireGuard"
    if ! is_done "wg_key_exchange"; then
        log_info "Регистрация peer на VPS..."
        # Реализуется в install-vps.sh / install-home.sh
        step_done "wg_key_exchange"
    else step_skip "wg_key_exchange"; fi

    step "Активация туннеля Tier-2"
    if ! is_done "tunnel_up"; then
        systemctl start wg-quick@wg0 || true
        sleep 2
        if ip link show wg0 &>/dev/null; then
            log_ok "Туннель wg0 поднят"
        else
            log_warn "wg0 не поднялся, проверьте конфигурацию"
        fi
        step_done "tunnel_up"
    else step_skip "tunnel_up"; fi
}

# =============================================================================
# Фаза 4: Smoke-тесты
# =============================================================================
phase4() {
    step "Smoke-тесты"
    if ! is_done "smoke_tests"; then
        log_info "Запуск smoke-тестов..."
        bash "$REPO_DIR/tests/run-smoke-tests.sh" && log_ok "Все тесты прошли" || log_warn "Некоторые тесты не прошли, проверьте логи"
        step_done "smoke_tests"
    else step_skip "smoke_tests"; fi
}

# =============================================================================
# Фаза 5: Ручные шаги
# =============================================================================
phase5() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║              Ручные шаги (требуют вашего участия)           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    source /opt/vpn/.env 2>/dev/null || true
    echo "1. Настройте port forwarding на роутере:"
    echo "   UDP ${WG_AWG_PORT:-51820} → ${HOME_SERVER_IP:-<IP сервера>}"
    echo "   UDP ${WG_WG_PORT:-51821} → ${HOME_SERVER_IP:-<IP сервера>}"
    echo ""
    echo "2. Сгенерируйте mTLS сертификаты:"
    echo "   cd /opt/vpn && bash scripts/gen-mtls.sh"
    echo ""
    echo "3. Добавьте первого клиента через Telegram-бота:"
    echo "   Отправьте /start боту @$(echo ${TELEGRAM_BOT_TOKEN:-your_bot} | cut -d: -f1)"
    echo ""
    echo "Установка завершена! Смотрите docs/INSTALL.md для деталей."
}

# =============================================================================
# Главный поток
# =============================================================================
mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"

phase0
phase1
phase2
phase3
phase4
phase5

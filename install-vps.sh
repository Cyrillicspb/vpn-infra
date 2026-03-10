#!/bin/bash
# =============================================================================
# install-vps.sh — Установка компонентов на VPS
# Вызывается из setup.sh
# Использует: sysadmin пользователь (не root)
# =============================================================================
set -euo pipefail

STATE_FILE="${1:-/opt/vpn/.setup-state}"
VPS_IP="${2:-}"
REPO_DIR="/opt/vpn"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $*"; }

STEP=30
TOTAL=51

step() { ((STEP++)); echo -e "\n${BLUE}━━━ Шаг ${STEP}/${TOTAL}: $* ━━━${NC}"; }
is_done()   { grep -q "^$1$" "$STATE_FILE" 2>/dev/null; }
step_done() { echo "$1" >> "$STATE_FILE"; log_ok "Готово: $1"; }
step_skip() { log_info "Пропуск (уже выполнено): $1"; }

[[ -z "$VPS_IP" ]] && { log_error "VPS_IP не задан"; exit 1; }

source "$REPO_DIR/.env" 2>/dev/null || true
SSH_PORT="${VPS_SSH_PORT:-22}"
SSH_USER="sysadmin"

# ---------------------------------------------------------------------------
# Хелпер: выполнить команду на VPS
# ---------------------------------------------------------------------------
vps_exec() {
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "$SSH_USER@$VPS_IP" "$@"
}

vps_copy() {
    scp -P "$SSH_PORT" -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "$@"
}

# ---------------------------------------------------------------------------
# Шаг: Проверка SSH доступа
# ---------------------------------------------------------------------------
step "Проверка SSH-доступа к VPS"
if ! is_done "vps_ssh_ok"; then
    if ! vps_exec "echo ok" &>/dev/null; then
        log_warn "SSH на порту $SSH_PORT не работает. Пробуем порт 22..."
        SSH_PORT=22
        vps_exec "echo ok" || {
            log_error "SSH недоступен. Если SSH:22 заблокирован:"
            log_error "  1. Зайдите в веб-консоль VPS у провайдера"
            log_error "  2. Создайте пользователя sysadmin: adduser sysadmin && usermod -aG sudo sysadmin"
            log_error "  3. Скопируйте SSH-ключ вручную"
            exit 1
        }
    fi
    log_ok "SSH доступен (порт $SSH_PORT)"
    step_done "vps_ssh_ok"
else step_skip "vps_ssh_ok"; fi

# ---------------------------------------------------------------------------
# Шаг: Базовая настройка VPS
# ---------------------------------------------------------------------------
step "Базовая настройка VPS (обновление, пакеты)"
if ! is_done "vps_base_setup"; then
    vps_exec "sudo apt-get update -qq && sudo apt-get upgrade -y -qq"
    vps_exec "sudo apt-get install -y -qq curl wget git jq docker.io docker-compose nftables fail2ban"
    vps_exec "sudo systemctl enable docker && sudo systemctl start docker"
    step_done "vps_base_setup"
else step_skip "vps_base_setup"; fi

# ---------------------------------------------------------------------------
# Шаг: Отключение IPv6 на VPS
# ---------------------------------------------------------------------------
step "Отключение IPv6 на VPS"
if ! is_done "vps_ipv6_disabled"; then
    vps_exec "cat << 'EOF' | sudo tee /etc/sysctl.d/99-disable-ipv6.conf
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF
sudo sysctl -p /etc/sysctl.d/99-disable-ipv6.conf"
    step_done "vps_ipv6_disabled"
else step_skip "vps_ipv6_disabled"; fi

# ---------------------------------------------------------------------------
# Шаг: fail2ban на VPS
# ---------------------------------------------------------------------------
step "fail2ban на VPS"
if ! is_done "vps_fail2ban"; then
    vps_exec "cat << 'EOF' | sudo tee /etc/fail2ban/jail.local
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
EOF
sudo systemctl enable fail2ban && sudo systemctl restart fail2ban"
    step_done "vps_fail2ban"
else step_skip "vps_fail2ban"; fi

# ---------------------------------------------------------------------------
# Шаг: Копирование файлов VPS
# ---------------------------------------------------------------------------
step "Копирование конфигурации на VPS"
if ! is_done "vps_files_copied"; then
    vps_exec "mkdir -p /opt/vpn"
    vps_copy -r "$REPO_DIR/vps/." "$SSH_USER@$VPS_IP:/opt/vpn/"
    vps_copy "$REPO_DIR/.env.example" "$SSH_USER@$VPS_IP:/opt/vpn/.env.example"
    step_done "vps_files_copied"
else step_skip "vps_files_copied"; fi

# ---------------------------------------------------------------------------
# Шаг: Git-зеркало на VPS
# ---------------------------------------------------------------------------
step "Настройка git-зеркала на VPS"
if ! is_done "vps_git_mirror"; then
    vps_exec "mkdir -p /opt/vpn/vpn-repo.git"
    # Инициализируем bare repo
    vps_exec "cd /opt/vpn/vpn-repo.git && git init --bare 2>/dev/null || true"
    # Cron для синхронизации с GitHub (если доступен)
    vps_exec "cat << 'EOF' | sudo tee /etc/cron.d/vpn-mirror
# Синхронизация git-зеркала с GitHub
*/30 * * * * $SSH_USER cd /opt/vpn/vpn-repo.git && git fetch --all 2>/dev/null || true
EOF"
    step_done "vps_git_mirror"
else step_skip "vps_git_mirror"; fi

# ---------------------------------------------------------------------------
# Шаг: mTLS CA (самоподписанный)
# ---------------------------------------------------------------------------
step "Генерация mTLS CA"
if ! is_done "vps_mtls_ca"; then
    vps_exec "mkdir -p /opt/vpn/nginx/mtls"
    vps_exec "openssl genrsa -out /opt/vpn/nginx/mtls/ca.key 4096 2>/dev/null && \
        openssl req -new -x509 -days 3650 \
            -key /opt/vpn/nginx/mtls/ca.key \
            -out /opt/vpn/nginx/mtls/ca.crt \
            -subj '/CN=VPN-CA' 2>/dev/null && \
        chmod 600 /opt/vpn/nginx/mtls/ca.key && \
        echo 'CA создан'"
    step_done "vps_mtls_ca"
else step_skip "vps_mtls_ca"; fi

# ---------------------------------------------------------------------------
# Шаг: Docker Compose на VPS
# ---------------------------------------------------------------------------
step "Запуск Docker Compose на VPS"
if ! is_done "vps_docker_up"; then
    vps_exec "[[ -f /opt/vpn/.env ]] || cp /opt/vpn/.env.example /opt/vpn/.env"
    vps_exec "cd /opt/vpn && sudo docker compose up -d --remove-orphans || true"
    step_done "vps_docker_up"
else step_skip "vps_docker_up"; fi

# ---------------------------------------------------------------------------
# Шаг: VPS healthcheck cron
# ---------------------------------------------------------------------------
step "Настройка VPS healthcheck"
if ! is_done "vps_healthcheck"; then
    ADMIN_CHAT="${TELEGRAM_ADMIN_CHAT_ID:-}"
    BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
    vps_exec "cat << 'CRONEOF' | sudo tee /etc/cron.d/vps-healthcheck
# VPS healthcheck каждые 5 мин
*/5 * * * * $SSH_USER bash /opt/vpn/scripts/vps-healthcheck.sh >> /var/log/vps-healthcheck.log 2>&1
CRONEOF"
    # Копируем скрипт
    vps_copy "$REPO_DIR/vps/scripts/vps-healthcheck.sh" \
        "$SSH_USER@$VPS_IP:/opt/vpn/scripts/vps-healthcheck.sh"
    step_done "vps_healthcheck"
else step_skip "vps_healthcheck"; fi

log_ok "install-vps.sh завершён для $VPS_IP"

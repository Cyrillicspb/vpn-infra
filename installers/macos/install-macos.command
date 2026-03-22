#!/usr/bin/env bash
# VPN Infrastructure — Установщик для macOS
# Двойной клик по этому файлу запускает установку.
# Требования: macOS 10.15+, SSH доступ к домашнему серверу (Ubuntu 24.04).

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Цвета
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

clear
echo -e "${BOLD}╔════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║     VPN Infrastructure — Установка     ║${RESET}"
echo -e "${BOLD}╚════════════════════════════════════════╝${RESET}"
echo ""
echo -e "Этот скрипт подключится к вашему домашнему серверу"
echo -e "и запустит автоматическую установку VPN-инфраструктуры."
echo ""

# Проверка SSH
if ! command -v ssh &>/dev/null; then
    echo -e "${RED}Ошибка: ssh не найден${RESET}"
    echo "Установите Xcode Command Line Tools: xcode-select --install"
    read -rp "Нажмите Enter для выхода..."
    exit 1
fi

# Проверка/создание SSH ключа
SSH_KEY="$HOME/.ssh/vpn_deploy_key"
if [[ ! -f "$SSH_KEY" ]]; then
    echo -e "${BLUE}Создание SSH ключа для деплоя...${RESET}"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "vpn-deploy-$(date +%Y%m%d)"
    echo ""
    echo -e "${GREEN}SSH ключ создан: $SSH_KEY${RESET}"
fi

echo -e "${BOLD}Введите данные вашего домашнего сервера:${RESET}"
echo "(Ubuntu Server 24.04, уже установленный и подключённый к сети)"
echo ""

# IP домашнего сервера
while true; do
    read -rp "IP-адрес домашнего сервера (например: 192.168.1.100): " SERVER_IP
    if [[ "$SERVER_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        break
    fi
    echo -e "${RED}Неверный IP-адрес. Попробуйте ещё раз.${RESET}"
done

# SSH пользователь
read -rp "Пользователь SSH [sysadmin]: " SERVER_USER
SERVER_USER="${SERVER_USER:-sysadmin}"

# SSH порт
read -rp "SSH порт [22]: " SSH_PORT
SSH_PORT="${SSH_PORT:-22}"

echo ""
echo -e "${BLUE}Копирование SSH ключа на сервер...${RESET}"
echo "(Вам потребуется ввести пароль от $SERVER_USER@$SERVER_IP)"
echo ""

if ssh-copy-id -i "${SSH_KEY}.pub" \
    -o "StrictHostKeyChecking=accept-new" \
    -p "$SSH_PORT" \
    "${SERVER_USER}@${SERVER_IP}" 2>/dev/null; then
    echo -e "${GREEN}✓ SSH ключ скопирован${RESET}"
else
    echo -e "${YELLOW}Не удалось скопировать ключ автоматически.${RESET}"
    echo "Добавьте ключ вручную на сервере:"
    echo "  echo '$(cat "${SSH_KEY}.pub")' >> ~/.ssh/authorized_keys"
    echo ""
    read -rp "Нажмите Enter когда ключ будет добавлен..."
fi

# Проверка подключения
echo ""
echo -e "${BLUE}Проверка подключения к серверу...${RESET}"
if ssh -i "$SSH_KEY" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "ConnectTimeout=10" \
    -p "$SSH_PORT" \
    "${SERVER_USER}@${SERVER_IP}" \
    'echo "OK: $(uname -n) / $(lsb_release -d 2>/dev/null | cut -f2 || cat /etc/os-release | grep PRETTY | cut -d= -f2 | tr -d "\"")"' 2>/dev/null; then
    echo -e "${GREEN}✓ Подключение успешно${RESET}"
else
    echo -e "${RED}Ошибка подключения к серверу.${RESET}"
    echo ""
    echo "Проверьте:"
    echo "  1. Сервер включён и подключён к сети"
    echo "  2. IP-адрес: $SERVER_IP, пользователь: $SERVER_USER, порт: $SSH_PORT"
    echo "  3. SSH доступен (openssh-server установлен на сервере)"
    read -rp "Нажмите Enter для выхода..."
    exit 1
fi

echo ""
echo -e "${BOLD}Всё готово для установки!${RESET}"
echo ""
echo -e "Подключение к: ${BOLD}${SERVER_USER}@${SERVER_IP}:${SSH_PORT}${RESET}"
echo ""
echo -e "${YELLOW}Внимание: Установка займёт 15-30 минут.${RESET}"
echo "Не закрывайте это окно до завершения."
echo ""
read -rp "Начать установку? [y/N]: " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Отменено."
    exit 0
fi

echo ""
echo -e "${BOLD}Запуск установки...${RESET}"
echo -e "${BLUE}══════════════════════════════════════════${RESET}"
echo ""

# Загрузка репо на сервер: сначала tar архив из локального репозитория,
# при отсутствии — скачать vpn-infra.tar.gz из последнего GitHub Release.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SETUP_PATH="/opt/vpn/setup.sh"

if [[ -f "$REPO_ROOT/setup.sh" && -f "$REPO_ROOT/install-home.sh" && -d "$REPO_ROOT/home" ]]; then
    echo -e "${BLUE}Упаковка репозитория в архив...${RESET}"
    TMP_ARCHIVE="$(mktemp /tmp/vpn-infra-XXXXXX.tar.gz)"
    tar -czf "$TMP_ARCHIVE" \
        --exclude='.git' --exclude='*.pyc' --exclude='__pycache__' \
        --exclude='*/venv/*' --exclude='node_modules' --exclude='*.log' \
        --exclude='.env' \
        -C "$REPO_ROOT" .
    echo -e "${BLUE}Загрузка архива на сервер...${RESET}"
    ssh -i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -p "$SSH_PORT" \
        "${SERVER_USER}@${SERVER_IP}" \
        "sudo mkdir -p /opt/vpn && sudo chown ${SERVER_USER}:${SERVER_USER} /opt/vpn"
    scp -i "$SSH_KEY" -P "$SSH_PORT" -o "StrictHostKeyChecking=accept-new" \
        "$TMP_ARCHIVE" "${SERVER_USER}@${SERVER_IP}:/tmp/vpn-infra.tar.gz"
    ssh -i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -p "$SSH_PORT" \
        "${SERVER_USER}@${SERVER_IP}" \
        "tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner 2>/dev/null; rm /tmp/vpn-infra.tar.gz"
    rm -f "$TMP_ARCHIVE"
    echo -e "${GREEN}✓ Репозиторий загружен из локальной копии${RESET}"
else
    echo -e "${BLUE}Скачивание последнего релиза на сервере...${RESET}"
    ssh -i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -o "ServerAliveInterval=30" \
        -p "$SSH_PORT" "${SERVER_USER}@${SERVER_IP}" 'bash -s' << 'REMOTE_EOF'
RELEASE_URL=$(curl -sSfL --max-time 10 \
    https://api.github.com/repos/Cyrillicspb/vpn-infra/releases/latest 2>/dev/null \
    | python3 -c "
import sys, json
assets = [a for a in json.load(sys.stdin)['assets'] if a['name']=='vpn-infra.tar.gz']
print(assets[0]['browser_download_url'] if assets else '')
" 2>/dev/null)
[ -z "$RELEASE_URL" ] && { echo "ERROR: GitHub Release не найден"; exit 1; }
curl -fsSL --max-time 120 "$RELEASE_URL" -o /tmp/vpn-infra.tar.gz
sudo mkdir -p /opt/vpn
sudo tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner 2>/dev/null; true
rm /tmp/vpn-infra.tar.gz
echo "OK"
REMOTE_EOF
    echo -e "${GREEN}✓ Последний релиз скачан с GitHub${RESET}"
fi

ssh -i "$SSH_KEY" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "ServerAliveInterval=30" \
    -o "ServerAliveCountMax=10" \
    -p "$SSH_PORT" \
    -t "${SERVER_USER}@${SERVER_IP}" \
    "tmux new-session -A -s vpn-install 'sudo bash $SETUP_PATH'"

RESULT=$?
echo ""
echo -e "${BLUE}══════════════════════════════════════════${RESET}"

if [[ $RESULT -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}✓ Установка завершена успешно!${RESET}"
    echo ""
    echo "Следующие шаги:"
    echo "  1. Настройте Port Forwarding на роутере:"
    echo "     UDP 51820 → ${SERVER_IP}:51820 (AmneziaWG)"
    echo "     UDP 51821 → ${SERVER_IP}:51821 (WireGuard)"
    echo "  2. Откройте Telegram и напишите боту /start"
    echo "  3. Получите конфиг через бот и импортируйте в WireGuard"
else
    echo -e "${RED}${BOLD}✗ Установка завершилась с ошибкой (код $RESULT)${RESET}"
    echo ""
    echo "Для диагностики:"
    echo "  ssh -i $SSH_KEY -p $SSH_PORT ${SERVER_USER}@${SERVER_IP}"
    echo "  cat /tmp/vpn-setup.log"
fi

echo ""
read -rp "Нажмите Enter для выхода..."

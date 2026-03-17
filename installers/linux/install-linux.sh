#!/usr/bin/env bash
# VPN Infrastructure — Установщик для Linux
# Запускается на машине пользователя (НЕ на домашнем сервере).
# Подключается к домашнему серверу по SSH и запускает setup.sh.
#
# Использование:
#   chmod +x install.sh && ./install.sh
#
# Требования: bash, ssh, scp

set -euo pipefail

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
    echo "Установите: sudo apt install openssh-client"
    exit 1
fi

# Проверка/создание SSH ключа
SSH_KEY="$HOME/.ssh/vpn_deploy_key"
if [[ ! -f "$SSH_KEY" ]]; then
    echo -e "${BLUE}Создание SSH ключа для деплоя...${RESET}"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "vpn-deploy-$(date +%Y%m%d)"
    echo -e "${GREEN}SSH ключ создан: $SSH_KEY${RESET}"
fi

echo -e "${BOLD}Введите данные вашего домашнего сервера:${RESET}"
echo "(Ubuntu Server 24.04, уже установленный и подключённый к сети)"
echo ""

while true; do
    read -rp "IP-адрес домашнего сервера (например: 192.168.1.100): " SERVER_IP
    if [[ "$SERVER_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        break
    fi
    echo -e "${RED}Неверный IP-адрес. Попробуйте ещё раз.${RESET}"
done

read -rp "Пользователь SSH [sysadmin]: " SERVER_USER
SERVER_USER="${SERVER_USER:-sysadmin}"

read -rp "SSH порт [22]: " SSH_PORT
SSH_PORT="${SSH_PORT:-22}"

echo ""
echo -e "${BLUE}Копирование SSH ключа на сервер...${RESET}"
echo "(Потребуется ввести пароль от $SERVER_USER@$SERVER_IP)"
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

echo ""
echo -e "${BLUE}Проверка подключения...${RESET}"
if ! ssh -i "$SSH_KEY" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "ConnectTimeout=10" \
    -p "$SSH_PORT" \
    "${SERVER_USER}@${SERVER_IP}" \
    'echo "OK: $(hostname)"' 2>/dev/null; then
    echo -e "${RED}Ошибка подключения к серверу.${RESET}"
    echo "Проверьте IP, пользователя, порт и доступность SSH."
    exit 1
fi
echo -e "${GREEN}✓ Подключение успешно${RESET}"

echo ""
echo -e "${BOLD}Цель: ${SERVER_USER}@${SERVER_IP}:${SSH_PORT}${RESET}"
echo ""
echo -e "${YELLOW}Внимание: установка займёт 25–45 минут. Не закрывайте терминал.${RESET}"
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

# Загрузка скриптов на сервер: сначала из локального репозитория,
# при отсутствии — скачать на сервере с GitHub (fallback: jsdelivr CDN)
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [[ -f "$REPO_ROOT/setup.sh" && -f "$REPO_ROOT/install-home.sh" ]]; then
    echo "Загрузка скриптов из локального репозитория..."
    scp -i "$SSH_KEY" -P "$SSH_PORT" -o "StrictHostKeyChecking=accept-new" \
        "$REPO_ROOT/setup.sh" \
        "$REPO_ROOT/install-home.sh" \
        "${SERVER_USER}@${SERVER_IP}:/tmp/"
    [[ -f "$REPO_ROOT/install-vps.sh" ]] && scp -i "$SSH_KEY" -P "$SSH_PORT" \
        -o "StrictHostKeyChecking=accept-new" \
        "$REPO_ROOT/install-vps.sh" "${SERVER_USER}@${SERVER_IP}:/tmp/"
    echo -e "${GREEN}✓ Скрипты загружены из локального репозитория${RESET}"
else
    echo "Скачивание скриптов на сервере (GitHub, fallback: jsdelivr CDN)..."
    ssh -i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -o "ServerAliveInterval=30" \
        -p "$SSH_PORT" "${SERVER_USER}@${SERVER_IP}" \
        'cd /tmp && for f in setup.sh install-home.sh install-vps.sh; do
            for b in https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master \
                     https://cdn.jsdelivr.net/gh/Cyrillicspb/vpn-infra@master; do
                curl -fsSL --max-time 30 "$b/$f" -o "$f" 2>/dev/null && echo "OK: $f" && break
            done
        done && chmod +x setup.sh install-home.sh install-vps.sh'
fi

echo ""
echo -e "${BOLD}Запуск setup.sh на сервере...${RESET}"

ssh -i "$SSH_KEY" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "ServerAliveInterval=30" \
    -o "ServerAliveCountMax=10" \
    -p "$SSH_PORT" \
    -t "${SERVER_USER}@${SERVER_IP}" \
    "sudo bash /tmp/setup.sh 2>&1 | tee /tmp/vpn-setup.log; exit \${PIPESTATUS[0]}"

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
    echo "  3. Получите конфиг через бот и импортируйте в WireGuard / AmneziaWG"
else
    echo -e "${RED}${BOLD}✗ Установка завершилась с ошибкой (код $RESULT)${RESET}"
    echo ""
    echo "Для диагностики:"
    echo "  ssh -i $SSH_KEY -p $SSH_PORT ${SERVER_USER}@${SERVER_IP}"
    echo "  cat /tmp/vpn-setup.log"
    echo ""
    echo "Повторный запуск безопасен — выполненные шаги будут пропущены."
fi

echo ""
read -rp "Нажмите Enter для выхода..."

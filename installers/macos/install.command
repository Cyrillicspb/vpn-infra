#!/bin/bash
# Установщик для macOS — подключается к домашнему серверу и запускает setup.sh
# Двойной клик → откроется Terminal и запустится установка

echo "=== VPN Infrastructure Installer ==="
echo ""
read -p "IP-адрес домашнего сервера: " SERVER_IP
read -p "Пользователь SSH (default: sysadmin): " SSH_USER
SSH_USER="${SSH_USER:-sysadmin}"

echo ""
echo "Подключение к $SSH_USER@$SERVER_IP..."
ssh -t "$SSH_USER@$SERVER_IP" "
    if [ -f /opt/vpn/setup.sh ]; then
        cd /opt/vpn && bash setup.sh
    else
        curl -fsSL https://raw.githubusercontent.com/your-repo/vpn-infra/main/setup.sh -o /tmp/setup.sh
        bash /tmp/setup.sh
    fi
"

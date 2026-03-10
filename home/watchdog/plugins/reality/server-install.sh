#!/bin/bash
# Настройка VLESS+REALITY в 3x-ui на VPS
# Используется как справочник — конфигурация через веб-панель 3x-ui
set -euo pipefail

echo "=== Настройка VLESS+REALITY ==="
echo ""
echo "ВАЖНО: VLESS+REALITY настраивается через панель 3x-ui."
echo ""
echo "1. Откройте 3x-ui: https://${VPS_IP}:2053"
echo "2. Добавьте inbound:"
echo "   - Протокол: vless"
echo "   - Порт: 443"
echo "   - Security: Reality"
echo "   - Dest: microsoft.com:443"
echo "   - SNI: microsoft.com, www.microsoft.com"
echo "   - Flow: xtls-rprx-vision"
echo "   - Fingerprint: chrome"
echo ""
echo "3. Скопируйте публичный ключ в .env → XRAY_PUBLIC_KEY"
echo "4. Скопируйте UUID → XRAY_UUID"

# Проверка: 3x-ui запущен?
if docker ps | grep -q "3x-ui"; then
    echo ""
    echo "OK: 3x-ui запущен"
else
    echo "ERROR: 3x-ui не запущен. Запустите: cd /opt/vpn && docker compose up -d 3x-ui"
    exit 1
fi

echo ""
echo "=== Готово ==="

#!/bin/bash
# Настройка Cloudflare CDN стека на VPS
set -euo pipefail

echo "=== Настройка Cloudflare CDN стека ==="
echo ""

# Проверка CF_TUNNEL_TOKEN
[[ -z "${CF_TUNNEL_TOKEN:-}" ]] && {
    echo "ERROR: CF_TUNNEL_TOKEN не задан в .env"
    exit 1
}

echo "1. Создание cloudflared tunnel на VPS..."
docker run --rm cloudflare/cloudflared:2024.2.1 \
    tunnel login 2>/dev/null || echo "  (Пропуск — используем token)"

echo ""
echo "2. Xray WebSocket inbound настраивается в 3x-ui:"
echo "   - Протокол: vless"
echo "   - Порт: 8080 (listen: 127.0.0.1)"
echo "   - Transport: WebSocket"
echo "   - Path: /vless"
echo "   - Security: none (TLS на cloudflared)"
echo ""
echo "3. cloudflared на VPS настраивается через CF_TUNNEL_TOKEN"
echo "   Уже добавлен в docker-compose.yml"
echo ""

if docker ps | grep -q "cloudflared"; then
    echo "OK: cloudflared запущен на VPS"
else
    cd /opt/vpn && docker compose up -d cloudflared
fi

echo ""
echo "=== Cloudflare CDN настроен ==="

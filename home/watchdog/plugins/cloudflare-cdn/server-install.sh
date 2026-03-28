#!/bin/bash
# Настройка Cloudflare CDN стека на VPS
set -euo pipefail

echo "=== Настройка Cloudflare CDN стека ==="
echo ""

echo "1. Xray WebSocket inbound настраивается в 3x-ui:"
echo "   - Протокол: vless"
echo "   - Порт: 8080 (listen: 127.0.0.1)"
echo "   - Transport: WebSocket"
echo "   - Path: /vless"
echo "   - Security: none (TLS на cloudflared)"
echo ""
echo "2. cloudflared на VPS работает в режиме sleep (CDN через Workers, tunnel не нужен)"
echo ""

if docker ps | grep -q "cloudflared"; then
    echo "OK: cloudflared запущен на VPS"
else
    cd /opt/vpn && docker compose up -d cloudflared
fi

echo ""
echo "=== Cloudflare CDN настроен ==="

#!/bin/bash
# Установка Hysteria2 сервера на VPS
# Вызывается через: ssh vps "bash -s" < server-install.sh
set -euo pipefail

HYSTERIA_VERSION="app/v2.5.1"
HYSTERIA_URL="https://github.com/apernet/hysteria/releases/download/${HYSTERIA_VERSION}/hysteria-linux-amd64"

echo "=== Установка Hysteria2 на VPS ==="

# Установка бинаря
if [[ ! -f /usr/local/bin/hysteria ]]; then
    echo "Скачивание Hysteria2..."
    curl -fsSL "$HYSTERIA_URL" -o /usr/local/bin/hysteria
    chmod +x /usr/local/bin/hysteria
    echo "OK: hysteria установлен"
fi

# Создание конфига
mkdir -p /etc/hysteria
cat > /etc/hysteria/config.yaml << 'YAML_EOF'
listen: :443

tls:
  cert: /etc/hysteria/server.crt
  key: /etc/hysteria/server.key

auth:
  type: password
  password: "${HYSTERIA2_AUTH}"

obfs:
  type: salamander
  salamander:
    password: "${HYSTERIA2_OBFS_PASSWORD}"

quic:
  keepAlivePeriod: 20s
  maxIdleTimeout: 60s

bandwidth:
  up: 1 gbps
  down: 1 gbps

masquerade:
  type: proxy
  proxy:
    url: https://news.ycombinator.com/
    rewriteHost: true

log:
  level: warn
YAML_EOF

# Самоподписанный TLS сертификат для Hysteria2
if [[ ! -f /etc/hysteria/server.crt ]]; then
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /etc/hysteria/server.key \
        -out /etc/hysteria/server.crt \
        -days 3650 \
        -subj "/CN=hysteria-server"
    chmod 600 /etc/hysteria/server.key
fi

# Systemd unit
cat > /etc/systemd/system/hysteria2.service << 'SERVICE_EOF'
[Unit]
Description=Hysteria2 VPN Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/hysteria server --config /etc/hysteria/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

systemctl daemon-reload
systemctl enable hysteria2
systemctl restart hysteria2

# Проверка
sleep 2
if systemctl is-active --quiet hysteria2; then
    echo "OK: Hysteria2 запущен"
else
    echo "ERROR: Hysteria2 не запустился"
    journalctl -u hysteria2 -n 20
    exit 1
fi

# nftables rate limiting на VPS
nft add rule inet filter input udp dport 443 limit rate 1000/second burst 2000 packets accept 2>/dev/null || true

echo "=== Hysteria2 установлен успешно ==="

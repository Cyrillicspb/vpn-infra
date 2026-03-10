#!/usr/bin/env bash
# Миграция 003: Обновление конфига logrotate
# Устанавливает корректный logrotate для vpn логов (rotate 14, compress, dateext)

set -euo pipefail

LOGROTATE_CONF="/etc/logrotate.d/vpn"

echo "[migration 003] Обновление logrotate конфига..."

cat > "$LOGROTATE_CONF" << 'EOF'
/var/log/vpn-*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    dateext
    dateformat -%Y%m%d
    create 640 root adm
    postrotate
        systemctl kill -s HUP watchdog.service 2>/dev/null || true
    endscript
}

/var/log/vpn-watchdog.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    dateext
    dateformat -%Y%m%d
    size 50M
    create 640 root adm
    postrotate
        systemctl kill -s HUP watchdog.service 2>/dev/null || true
    endscript
}

/var/log/vpn-backup.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 640 root adm
}
EOF

chmod 644 "$LOGROTATE_CONF"

# Проверить конфиг
if logrotate --debug "$LOGROTATE_CONF" &>/dev/null; then
    echo "[migration 003] logrotate конфиг применён: $LOGROTATE_CONF"
else
    echo "[migration 003] Предупреждение: logrotate --debug завершился с предупреждениями"
fi

echo "[migration 003] OK"

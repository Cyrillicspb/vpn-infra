#!/bin/bash
# =============================================================================
# disk-cleanup.sh — Очистка дискового пространства
# Вызывается watchdog при превышении порогов
# =============================================================================
set -euo pipefail

LEVEL="${1:-auto}"  # auto | aggressive | critical

log() { echo "[$(date '+%H:%M:%S')] DISK-CLEANUP: $*"; }

DISK_PERCENT=$(df / | awk 'NR==2{print $5}' | tr -d '%')
log "Заполнение диска: ${DISK_PERCENT}%"

case "$LEVEL" in
    auto|80)
        log "Уровень: обычная очистка (>80%)"
        docker system prune -f 2>/dev/null || true
        journalctl --vacuum-size=200M 2>/dev/null || true
        find /var/log/vpn-*.log -mtime +7 -delete 2>/dev/null || true
        ;;

    aggressive|90)
        log "Уровень: агрессивная очистка (>90%)"
        docker system prune -af 2>/dev/null || true
        docker volume prune -f 2>/dev/null || true
        journalctl --vacuum-size=100M 2>/dev/null || true
        # Удаляем старые бэкапы (оставляем последние 5)
        ls -t /opt/vpn/backups/vpn-backup-* 2>/dev/null | tail -n +6 | xargs rm -f || true
        # Очищаем логи
        find /var/log -name "*.gz" -mtime +3 -delete 2>/dev/null || true
        ;;

    critical|95)
        log "Уровень: КРИТИЧЕСКАЯ очистка (>95%)"
        # Останавливаем некритичные сервисы
        docker stop homepage portainer 2>/dev/null || true
        docker system prune -af --volumes 2>/dev/null || true
        journalctl --vacuum-size=50M 2>/dev/null || true
        # Удаляем ВСЕ бэкапы кроме последнего
        ls -t /opt/vpn/backups/vpn-backup-* 2>/dev/null | tail -n +2 | xargs rm -f || true
        ;;
esac

DISK_AFTER=$(df / | awk 'NR==2{print $5}' | tr -d '%')
log "После очистки: ${DISK_AFTER}%"

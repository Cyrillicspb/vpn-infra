#!/bin/bash
# =============================================================================
# postboot-check.sh — Проверка системы после загрузки
# Systemd unit: vpn-postboot.service
# =============================================================================
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true

log() { echo "[$(date '+%H:%M:%S')] POSTBOOT: $*"; }
PASS=0
FAIL=0
REPORT=""

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" > /dev/null 2>&1; then
        log "OK: $name"
        ((PASS++))
        REPORT+="✅ $name\n"
    else
        log "FAIL: $name"
        ((FAIL++))
        REPORT+="❌ $name\n"
    fi
}

log "=== Post-boot проверка ==="

# Ждём поднятия сервисов
sleep 15

# Проверки
check "nftables" "systemctl is-active nftables"
check "dnsmasq" "systemctl is-active dnsmasq"
check "watchdog" "systemctl is-active watchdog"
check "docker" "systemctl is-active docker"
check "wg0 (AWG)" "ip link show wg0"
check "wg1 (WG)" "ip link show wg1"
check "DNS резолвинг" "dig @127.0.0.1 google.com +short +time=5"
check "nft blocked_static" "nft list set inet vpn blocked_static"
check "DKMS awg" "dkms status | grep -q 'amneziawg'"
check "telegram-bot" "docker inspect --format '{{.State.Running}}' telegram-bot | grep -q true"

# Проверка DKMS после обновления ядра
CURRENT_KERNEL=$(uname -r)
log "Ядро: $CURRENT_KERNEL"

# Отправляем отчёт
HOSTNAME=$(hostname)
STATUS_EMOJI=$([[ $FAIL -eq 0 ]] && echo "✅" || echo "⚠️")
MESSAGE="${STATUS_EMOJI} *Post-boot отчёт* (${HOSTNAME})

Прошло: ${PASS}, Не прошло: ${FAIL}

$(echo -e "$REPORT")
Ядро: \`${CURRENT_KERNEL}\`
Время загрузки: $(uptime -p)"

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}&text=${MESSAGE}&parse_mode=Markdown" \
        > /dev/null || true
fi

log "Проверка завершена: ${PASS} OK, ${FAIL} FAIL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

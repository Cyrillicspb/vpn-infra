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

# Мониторинг (фаза 2) — опционален при загрузке, не влияет на FAIL-счётчик
if docker inspect prometheus &>/dev/null 2>&1; then
    if docker inspect --format '{{.State.Running}}' prometheus 2>/dev/null | grep -q true; then
        log "OK: мониторинг (prometheus running)"
        PASS=$((PASS+1))
        REPORT+="✅ Мониторинг (prometheus)\n"
    else
        log "WARN: prometheus существует но не running"
        REPORT+="⚠️ Мониторинг (prometheus не running)\n"
    fi
else
    log "INFO: мониторинг не установлен (фаза 2 — установится после поднятия VPN)"
    REPORT+="ℹ️ Мониторинг: установится после VPN\n"
fi

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

# Дождаться watchdog и запросить /health (deep check через monitoring_loop)
# Watchdog поднимается async, даём 90 сек
WD_PORT="${WATCHDOG_PORT:-8080}"
WD_TOKEN="${WATCHDOG_API_TOKEN:-}"
for i in {1..9}; do
    sleep 10
    AUTH=""
    [[ -n "$WD_TOKEN" ]] && AUTH="-H \"Authorization: Bearer ${WD_TOKEN}\""
    if curl -sf --max-time 5 ${AUTH} "http://127.0.0.1:${WD_PORT}/status" > /dev/null 2>&1; then
        log "Watchdog API доступен (попытка ${i})"
        break
    fi
done

[[ $FAIL -eq 0 ]] && exit 0 || exit 1

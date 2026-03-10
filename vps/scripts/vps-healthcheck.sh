#!/bin/bash
# =============================================================================
# vps-healthcheck.sh — Healthcheck VPS (cron каждые 5 мин)
# =============================================================================
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0
FAIL=0
ALERTS=""

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" > /dev/null 2>&1; then
        ((PASS++))
    else
        ((FAIL++))
        ALERTS+="❌ $name\n"
    fi
}

notify() {
    local msg="$1"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}&text=${msg}&parse_mode=Markdown" \
        > /dev/null 2>&1 || true
}

# Проверки
check "3x-ui running"   "docker inspect --format '{{.State.Running}}' 3x-ui | grep -q true"
check "nginx running"   "docker inspect --format '{{.State.Running}}' nginx | grep -q true"
check "cloudflared"     "docker inspect --format '{{.State.Running}}' cloudflared | grep -q true"
check "prometheus"      "docker inspect --format '{{.State.Running}}' prometheus | grep -q true"
check "Xray port 443"   "nc -z -w3 localhost 443"
check "DNS external"    "dig @1.1.1.1 google.com +short +time=5"
check "Disk < 90%"      "[ $(df / | awk 'NR==2{print $5}' | tr -d '%') -lt 90 ]"

# Алерт если есть ошибки
if [[ $FAIL -gt 0 ]]; then
    HOSTNAME=$(hostname)
    notify "⚠️ *VPS healthcheck FAIL* (${HOSTNAME})\n\n${ALERTS}"
fi

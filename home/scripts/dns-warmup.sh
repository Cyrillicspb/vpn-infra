#!/bin/bash
# =============================================================================
# dns-warmup.sh — Прогрев DNS-кэша dnsmasq при старте
# Вызывается из dnsmasq.service ExecStartPost или postboot
# =============================================================================

DOMAINS=(
    # Популярные заблокированные ресурсы
    youtube.com
    instagram.com
    facebook.com
    twitter.com
    x.com
    tiktok.com
    # Google сервисы
    google.com
    googleapis.com
    gstatic.com
    googlevideo.com
    # Meta
    fbcdn.net
    cdninstagram.com
    # CDN
    cloudflare.com
    fastly.com
    akamai.net
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRITICAL_BLOCKED_DOMAINS_FILE="/opt/vpn/home/config/critical-blocked-domains.txt"
if [[ ! -f "$CRITICAL_BLOCKED_DOMAINS_FILE" ]]; then
    CRITICAL_BLOCKED_DOMAINS_FILE="${SCRIPT_DIR%/scripts}/config/critical-blocked-domains.txt"
fi

if [[ -f "$CRITICAL_BLOCKED_DOMAINS_FILE" ]]; then
    while IFS= read -r line; do
        domain="${line%%#*}"
        domain="${domain//[[:space:]]/}"
        [[ -n "$domain" ]] || continue
        DOMAINS+=("$domain")
    done < "$CRITICAL_BLOCKED_DOMAINS_FILE"
fi

mapfile -t DOMAINS < <(printf '%s\n' "${DOMAINS[@]}" | awk 'NF && !seen[$0]++')

log() { echo "[$(date '+%H:%M:%S')] DNS-WARMUP: $*"; }

log "Прогрев DNS-кэша (${#DOMAINS[@]} доменов)..."

WARMED=0
for domain in "${DOMAINS[@]}"; do
    dig @127.0.0.1 "$domain" +short +time=3 > /dev/null 2>&1 && ((WARMED++)) || true
    sleep 0.1  # Небольшая пауза между запросами
done

log "Прогрев завершён: ${WARMED}/${#DOMAINS[@]}"

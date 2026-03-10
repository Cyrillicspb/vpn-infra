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

log() { echo "[$(date '+%H:%M:%S')] DNS-WARMUP: $*"; }

log "Прогрев DNS-кэша (${#DOMAINS[@]} доменов)..."

WARMED=0
for domain in "${DOMAINS[@]}"; do
    dig @127.0.0.1 "$domain" +short +time=3 > /dev/null 2>&1 && ((WARMED++)) || true
    sleep 0.1  # Небольшая пауза между запросами
done

log "Прогрев завершён: ${WARMED}/${#DOMAINS[@]}"

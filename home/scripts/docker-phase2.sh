#!/usr/bin/env bash
# docker-phase2.sh — доустановка мониторинга после поднятия VPN
#
# Запускается из cron каждые 15 минут (пока мониторинг не установлен).
# Идемпотентен: если prometheus уже работает — выходит сразу.
#
# Логика:
#   1. Проверить — мониторинг уже установлен? → exit 0
#   2. Проверить — VPN работает (SOCKS5 :1080)? → иначе exit 0 (попробуем позже)
#   3. Настроить Docker HTTP proxy через xray SOCKS5
#   4. Pull образов мониторинга по одному
#   5. docker compose --profile monitoring up -d
#   6. Убрать cron-задание если всё успешно

set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

COMPOSE_FILE="/opt/vpn/docker-compose.yml"
LOG="/var/log/vpn-docker-phase2.log"
CRON_FILE="/etc/cron.d/vpn-docker-phase2"
PROXY_CONF="/etc/systemd/system/docker.service.d/http-proxy.conf"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# ── 1. Уже установлен? ────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^prometheus$"; then
    log "Мониторинг уже работает — выход"
    # Убираем cron-задание
    rm -f "$CRON_FILE" 2>/dev/null || true
    exit 0
fi

# ── 2. VPN готов? ─────────────────────────────────────────────────────────────
if ! curl -sf --max-time 10 --socks5 127.0.0.1:1080 \
        https://registry-1.docker.io/v2/ >/dev/null 2>&1; then
    log "VPN-стек (:1080) не готов — попробуем позже"
    exit 0
fi

log "=== Фаза 2: установка мониторинга ==="

# ── 3. Docker proxy через xray SOCKS5 ─────────────────────────────────────────
if [[ ! -f "$PROXY_CONF" ]]; then
    log "Настройка Docker HTTP proxy → socks5://127.0.0.1:1080"
    mkdir -p /etc/systemd/system/docker.service.d
    cat > "$PROXY_CONF" << 'EOF'
[Service]
Environment="HTTP_PROXY=socks5://127.0.0.1:1080"
Environment="HTTPS_PROXY=socks5://127.0.0.1:1080"
Environment="NO_PROXY=localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
EOF
    systemctl daemon-reload
    systemctl restart docker
    sleep 5
    log "Docker proxy настроен и docker перезапущен"
fi

# ── 4. Pull образов мониторинга ───────────────────────────────────────────────
MONITORING_IMAGES=(
    "prom/prometheus:latest"
    "prom/alertmanager:latest"
    "grafana/grafana:latest"
    "grafana/grafana-image-renderer:latest"
    "prom/node-exporter:latest"
)

_failed=0
for img in "${MONITORING_IMAGES[@]}"; do
    log "Pull: $img ..."
    if timeout 120 docker pull "$img" >> "$LOG" 2>&1; then
        log "  OK: $img"
    else
        log "  WARN: $img не скачался"
        ((_failed++)) || true
    fi
done

# ── 5. Запуск мониторинга ─────────────────────────────────────────────────────
if [[ ! -f "$COMPOSE_FILE" ]]; then
    log "WARN: docker-compose.yml не найден в /opt/vpn/"
    exit 1
fi

cd /opt/vpn

log "Запуск мониторинга (docker compose --profile monitoring up -d)..."
if docker compose --profile monitoring up -d >> "$LOG" 2>&1; then
    log "Мониторинг запущен"
else
    log "WARN: docker compose up завершился с ошибкой"
fi

# ── 6. Проверка результата ────────────────────────────────────────────────────
sleep 10
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^prometheus$"; then
    log "=== Мониторинг установлен успешно ==="
    # Убираем cron-задание — больше не нужно
    rm -f "$CRON_FILE" 2>/dev/null || true
    log "Cron-задание удалено"

    # Отправляем уведомление в Telegram
    _msg="✅ *Мониторинг установлен* — Prometheus, Grafana, Alertmanager запущены после поднятия VPN"
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]] && \
        curl -sf --max-time 10 \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
            --data-urlencode "text=${_msg}" \
            -d "parse_mode=Markdown" >/dev/null 2>&1 || true
else
    log "WARN: prometheus не запустился — попробуем при следующем запуске cron"
    [[ $_failed -gt 0 ]] && log "  Не скачалось образов: ${_failed}"
fi

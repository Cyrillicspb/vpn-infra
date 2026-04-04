#!/usr/bin/env bash
# docker-phase2.sh — повторная сборка telegram-bot и доустановка мониторинга
#
# Запускается из cron каждые 15 минут (пока мониторинг не установлен).
# Идемпотентен: если prometheus уже работает — выходит сразу.
#
# Логика:
#   1. Проверить — мониторинг уже установлен? → exit 0
#   2. Проверить — VPN работает (активный SOCKS5 watchdog) → иначе exit 0
#   3. Настроить Docker HTTP proxy через xray SOCKS5
#   4. Если telegram-bot не собран/не запущен — собрать и запустить его
#   5. Pull образов мониторинга по одному
#   6. docker compose --profile monitoring up -d
#   7. Убрать cron-задание если всё успешно

set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

COMPOSE_FILE="/opt/vpn/docker-compose.yml"
LOG="/var/log/vpn-docker-phase2.log"
CRON_FILE="/etc/cron.d/vpn-docker-phase2"
PROXY_CONF="/etc/systemd/system/docker.service.d/http-proxy.conf"
TG_SEND="/opt/vpn/scripts/tg-send.sh"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

notify_admin() {
    local msg="$1"

    if [[ -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
        log "WARN: TELEGRAM_ADMIN_CHAT_ID не задан — уведомление пропущено"
        return 1
    fi

    if [[ -x "$TG_SEND" ]]; then
        if "$TG_SEND" "${TELEGRAM_ADMIN_CHAT_ID}" "$msg"; then
            log "Уведомление в Telegram отправлено"
            return 0
        fi
        log "WARN: tg-send.sh не смог отправить уведомление"
        return 1
    fi

    log "WARN: tg-send.sh не найден — уведомление пропущено"
    return 1
}

critical_containers_running() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -Eq \
        '^(telegram-bot|socket-proxy|nginx|xray-client-xhttp|xray-client-cdn)$'
}

active_socks_works() {
    local port="$1"
    local code=""
    code="$(curl -sS --max-time 10 --socks5 "127.0.0.1:${port}" \
        -o /dev/null -w "%{http_code}" \
        https://registry-1.docker.io/v2/ 2>/dev/null || true)"
    [[ "$code" =~ ^(200|204|301|302|401)$ ]]
}

get_active_socks_port() {
    python3 - <<'PY'
import json
from pathlib import Path

state_path = Path("/opt/vpn/watchdog/state.json")
mapping = {
    "reality-xhttp": 1081,
    "cloudflare-cdn": 1082,
    "hysteria2": 1083,
    "vless-reality-vision": 1084,
}

if state_path.exists():
    try:
        state = json.loads(state_path.read_text())
        stack = state.get("active_stack", "")
        if stack in mapping:
            print(mapping[stack])
            raise SystemExit(0)
    except Exception:
        pass

for port in (1083, 1084, 1081, 1082):
    print(port)
    raise SystemExit(0)
PY
}

# ── 1. Уже установлен? ────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^prometheus$"; then
    log "Мониторинг уже работает — выход"
    # Убираем cron-задание
    rm -f "$CRON_FILE" 2>/dev/null || true
    exit 0
fi

# ── 2. VPN готов? ─────────────────────────────────────────────────────────────
SOCKS_PORT="$(get_active_socks_port)"
if ! active_socks_works "${SOCKS_PORT}"; then
    log "VPN-стек (:${SOCKS_PORT}) не готов — попробуем позже"
    exit 0
fi

log "=== Фаза 2: установка мониторинга ==="

if [[ -f /opt/vpn/scripts/docker-load-cache.sh ]]; then
    log "Загрузка локального Docker image cache перед pull..."
    if bash /opt/vpn/scripts/docker-load-cache.sh \
            --dir /opt/vpn/docker-images \
            --label "Monitoring Docker image cache" \
            --allow-empty >> "$LOG" 2>&1; then
        log "Локальный image cache обработан"
    else
        log "WARN: локальный image cache загрузился не полностью"
    fi
fi

if [[ "${VPN_STRICT_BUNDLE:-0}" == "1" ]] && [[ ! -d /opt/vpn/docker-images ]]; then
    log "Strict bundle mode: /opt/vpn/docker-images отсутствует, pull monitoring из сети запрещён"
    exit 1
fi

# ── 3.5. Повторная сборка telegram-bot после поднятия VPN ───────────────────
cd /opt/vpn

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^telegram-bot$"; then
    log "telegram-bot не запущен — пробуем собрать и поднять повторно"
    if docker compose build telegram-bot >> "$LOG" 2>&1; then
        log "telegram-bot собран"
        if docker compose up -d --no-build telegram-bot >> "$LOG" 2>&1; then
            log "telegram-bot запущен"
        else
            log "WARN: telegram-bot не запустился после успешной сборки"
        fi
    else
        log "WARN: повторная сборка telegram-bot не удалась"
    fi
fi

# ── 4. Docker proxy через xray SOCKS5 ─────────────────────────────────────────
_desired_proxy="socks5://127.0.0.1:${SOCKS_PORT}"
if [[ ! -f "$PROXY_CONF" ]] || ! grep -q "${_desired_proxy}" "$PROXY_CONF" 2>/dev/null; then
    log "Настройка Docker HTTP proxy → ${_desired_proxy}"
    mkdir -p /etc/systemd/system/docker.service.d
    cat > "$PROXY_CONF" << EOF
[Service]
Environment="HTTP_PROXY=${_desired_proxy}"
Environment="HTTPS_PROXY=${_desired_proxy}"
Environment="NO_PROXY=localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
EOF
    systemctl daemon-reload
    if critical_containers_running; then
        log "WARN: Docker proxy обновлён на диске, но restart docker отложен — есть активные контейнеры"
        log "WARN: Примените вручную в окно обслуживания: systemctl restart docker"
        exit 0
    fi
    systemctl restart docker
    sleep 5
    log "Docker proxy настроен и docker перезапущен"
fi

# ── 5. Pull образов мониторинга ───────────────────────────────────────────────
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
    if [[ "${VPN_STRICT_BUNDLE:-0}" == "1" ]]; then
        if docker image inspect "$img" >/dev/null 2>&1; then
            log "  OK: $img уже загружен локально"
        else
            log "  WARN: $img отсутствует в локальном cache при strict bundle mode"
            ((_failed++)) || true
        fi
    elif timeout 120 docker pull "$img" >> "$LOG" 2>&1; then
        log "  OK: $img"
    else
        log "  WARN: $img не скачался"
        ((_failed++)) || true
    fi
done

# ── 6. Запуск мониторинга ─────────────────────────────────────────────────────
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

# ── 7. Проверка результата ────────────────────────────────────────────────────
sleep 10
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^prometheus$"; then
    log "=== Мониторинг установлен успешно ==="
    # Убираем cron-задание — больше не нужно
    rm -f "$CRON_FILE" 2>/dev/null || true
    log "Cron-задание удалено"

    # Отправляем уведомление в Telegram
    _msg="✅ Мониторинг доустановлен

Prometheus, Grafana, Alertmanager и node-exporter запущены.
Фаза 2 завершена, система поднята полностью."
    notify_admin "$_msg" || true
else
    log "WARN: prometheus не запустился — попробуем при следующем запуске cron"
    [[ $_failed -gt 0 ]] && log "  Не скачалось образов: ${_failed}"
fi

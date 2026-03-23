#!/bin/bash
# =============================================================================
# vps-healthcheck.sh — Мониторинг VPS (cron каждые 5 мин)
#
# Cron: */5 * * * * root bash /opt/vpn/scripts/vps-healthcheck.sh >> /var/log/vpn-healthcheck.log 2>&1
#
# Проверяет:
#   - Docker-контейнеры (running + healthy)
#   - Xray XHTTP: порт 2087/tcp (microsoft.com) и 2083/tcp (cdn.jsdelivr.net)
#   - Hysteria2: контейнер running
#   - Cloudflared: HTTP 200 на metrics endpoint
#   - Nginx: HTTP 200 на /health
#   - Prometheus: HTTP 200 на /-/healthy
#   - Grafana: HTTP 200 на /api/health
#   - Внешний DNS
#   - Диск < 90%
#   - RAM < 90%
#
# Дедупликация: повтор алерта не чаще 1 раза в 30 мин для одной проблемы
# Recovery: уведомление в Telegram при восстановлении
# flock: не запускать параллельно
# =============================================================================
set -euo pipefail

LOCK_FILE="/var/run/vpn-healthcheck.lock"
DEDUP_DIR="/var/run/vpn-healthcheck"
DEDUP_TTL=1800   # 30 минут

# ── flock: один экземпляр за раз ─────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%H:%M:%S')] HEALTHCHECK: уже запущен (flock), выход"
    exit 0
fi

mkdir -p "$DEDUP_DIR"

# ── Загрузка .env ─────────────────────────────────────────────────────────────
source /opt/vpn/.env 2>/dev/null || true

# ── Логирование ───────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] HEALTHCHECK: $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] HEALTHCHECK: OK $*"; }
fail() { echo "[$(date '+%H:%M:%S')] HEALTHCHECK: FAIL $*" >&2; }

# ── Состояние проверок ────────────────────────────────────────────────────────
PASS=0
FAIL=0
ALERTS=()

# ── Telegram уведомление с дедупликацией ─────────────────────────────────────
notify_dedup() {
    local key="$1"
    local msg="$2"
    local dedup_file="${DEDUP_DIR}/${key}"

    # Пропустить если алерт отправлен < DEDUP_TTL сек назад
    if [[ -f "$dedup_file" ]]; then
        local last_sent
        last_sent=$(cat "$dedup_file")
        local now
        now=$(date +%s)
        if (( now - last_sent < DEDUP_TTL )); then
            log "Дедупликация: $key (следующий алерт через $(( DEDUP_TTL - (now - last_sent) )) сек)"
            return 0
        fi
    fi

    # Отправить алерт
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
        curl -sf --max-time 10 \
            --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"' "${TELEGRAM_BOT_TOKEN}") \
            -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
            --data-urlencode "text=${msg}" \
            -d "parse_mode=Markdown" \
            > /dev/null 2>&1 || true
    fi

    # Сохранить timestamp
    echo "$(date +%s)" > "$dedup_file"
}

# ── Сброс dedup при восстановлении + recovery уведомление ────────────────────
clear_dedup() {
    local key="$1"
    local name="${2:-}"
    # Если файл существует — проблема была, теперь восстановилась → уведомить
    if [[ -f "${DEDUP_DIR}/${key}" && -n "$name" ]]; then
        if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
            curl -sf --max-time 10 \
                --config <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"' "${TELEGRAM_BOT_TOKEN}") \
                -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
                --data-urlencode "text=✅ *VPS: восстановлено* — ${name}" \
                -d "parse_mode=Markdown" \
                > /dev/null 2>&1 || true
        fi
    fi
    rm -f "${DEDUP_DIR}/${key}"
}

# ── Проверка с записью результата ─────────────────────────────────────────────
check() {
    local name="$1"
    local key="$2"
    local cmd="$3"

    if eval "$cmd" > /dev/null 2>&1; then
        ((PASS++))
        ok "$name"
        clear_dedup "$key" "$name"
    else
        ((FAIL++))
        fail "$name"
        ALERTS+=("$name")
        notify_dedup "$key" "⚠️ *VPS: $name* FAIL\nHost: \`$(hostname)\`\nTime: $(date '+%Y-%m-%d %H:%M:%S')"
    fi
}

# ── Docker: проверка контейнера (running) ─────────────────────────────────────
check_container() {
    local name="$1"
    check "Docker: $name running" "docker_${name}" \
        "docker inspect --format '{{.State.Running}}' '$name' 2>/dev/null | grep -q '^true$'"
}

# ── Docker: проверка healthcheck статуса ──────────────────────────────────────
check_container_health() {
    local name="$1"
    check "Docker: $name healthy" "docker_health_${name}" \
        "docker inspect --format '{{.State.Health.Status}}' '$name' 2>/dev/null | grep -qE '^(healthy|starting)$'"
}

log "=== Healthcheck start ==="

# ── Docker контейнеры ─────────────────────────────────────────────────────────
check_container "3x-ui"
check_container "nginx"
check_container "cloudflared"
# prometheus, alertmanager, grafana НЕ запущены на VPS — мониторинг на домашнем сервере
check_container "node-exporter"
check_container "hysteria2"

check_container_health "3x-ui"
check_container_health "nginx"

# ── Xray XHTTP: порты 2087 (microsoft.com) и 2083 (cdn.jsdelivr.net) ─────────
check "Xray XHTTP 2087" "xray_xhttp_2087" \
    "nc -z -w5 localhost 2087"
check "Xray XHTTP 2083" "xray_xhttp_2083" \
    "nc -z -w5 localhost 2083"

# ── Nginx: health endpoint ────────────────────────────────────────────────────
check "Nginx health" "nginx_health" \
    "curl -sf --max-time 5 http://localhost:80/health | grep -q ok"

# ── Nginx mTLS panel: port 8443 TCP доступен ─────────────────────────────────
check "Nginx 8443" "nginx_8443" \
    "nc -z -w5 localhost 8443"

# ── Cloudflared: metrics endpoint ─────────────────────────────────────────────
check "Cloudflared metrics" "cloudflared_metrics" \
    "curl -sf --max-time 5 http://localhost:20241/metrics | grep -q cloudflared"

# ── 3x-ui panel: доступна (localhost) ─────────────────────────────────────────
check "3x-ui panel" "xui_panel" \
    "curl -sf --max-time 5 http://localhost:2053/ | grep -qi html"

# ── Внешний DNS ───────────────────────────────────────────────────────────────
check "DNS external" "dns_external" \
    "dig @1.1.1.1 google.com +short +time=5 | grep -qE '^[0-9]'"

# ── Диск < 90% ────────────────────────────────────────────────────────────────
DISK_PCT=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
if (( DISK_PCT < 90 )); then
    ((PASS++))
    ok "Disk ${DISK_PCT}% < 90%"
    clear_dedup "disk_space" "Диск в норме (${DISK_PCT}%)"
else
    ((FAIL++))
    fail "Disk ${DISK_PCT}% >= 90%"
    ALERTS+=("Диск ${DISK_PCT}%")
    notify_dedup "disk_space" "🔴 *VPS: критично мало места на диске*\n${DISK_PCT}% занято\nHost: $(hostname)"
fi

# ── RAM < 90% ─────────────────────────────────────────────────────────────────
RAM_AVAIL=$(free | awk '/^Mem:/{printf "%.0f", $7/$2*100}')
if (( RAM_AVAIL > 10 )); then
    ((PASS++))
    ok "RAM available ${RAM_AVAIL}%"
    clear_dedup "ram_pressure" "RAM в норме (${RAM_AVAIL}% свободно)"
else
    ((FAIL++))
    RAM_USED=$(( 100 - RAM_AVAIL ))
    fail "RAM used ${RAM_USED}%"
    ALERTS+=("RAM ${RAM_USED}%")
    notify_dedup "ram_pressure" "⚠️ *VPS: высокое потребление RAM*\n${RAM_USED}% занято\nHost: $(hostname)"
fi

# ── Итог ──────────────────────────────────────────────────────────────────────
log "=== Итог: OK=${PASS} FAIL=${FAIL} ==="

if [[ ${#ALERTS[@]} -gt 0 ]]; then
    ALERTS_STR=$(printf ' • %s\n' "${ALERTS[@]}")
    log "Проблемы:${ALERTS_STR}"
    # Сводный алерт если много проблем сразу (> 3)
    if [[ ${#ALERTS[@]} -ge 3 ]]; then
        notify_dedup "mass_failure" "🚨 *VPS: множественные проблемы (${#ALERTS[@]})*\n\n${ALERTS_STR}\n\nHost: $(hostname)\nTime: $(date '+%Y-%m-%d %H:%M:%S')"
    fi
fi

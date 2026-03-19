#!/bin/bash
# =============================================================================
# post-install-check.sh — Комплексная проверка после установки
#
# Запуск: sudo bash /opt/vpn/scripts/post-install-check.sh
# Проверяет все подсистемы и отправляет детальный отчёт в Telegram админу.
# =============================================================================
set -euo pipefail

ENV_FILE="/opt/vpn/.env"
[[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

# ── Цвета ─────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

# Telegram-отчёт: массив строк
REPORT_LINES=()

log_ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
log_fail() { echo -e "${RED}[✗]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $*"; }
log_info() { echo -e "${CYAN}[i]${NC} $*"; }

section() {
    echo ""
    echo -e "${CYAN}${BOLD}━━━ $* ━━━${NC}"
    REPORT_LINES+=("")
    REPORT_LINES+=("*$**")
}

# ── Проверки ──────────────────────────────────────────────────────────────────

ok() {
    local name="$1"
    log_ok "$name"
    ((PASS++))
    REPORT_LINES+=("✅ $name")
}

fail() {
    local name="$1" hint="${2:-}"
    log_fail "$name${hint:+ — $hint}"
    ((FAIL++))
    REPORT_LINES+=("❌ $name${hint:+ (${hint})}")
}

warn() {
    local name="$1" hint="${2:-}"
    log_warn "$name${hint:+ — $hint}"
    ((WARN++))
    REPORT_LINES+=("⚠️ $name${hint:+ (${hint})}")
}

check() {
    local name="$1" cmd="$2" hint="${3:-}"
    if eval "$cmd" > /dev/null 2>&1; then
        ok "$name"
    else
        fail "$name" "$hint"
    fi
}

check_warn() {
    local name="$1" cmd="$2" hint="${3:-}"
    if eval "$cmd" > /dev/null 2>&1; then
        ok "$name"
    else
        warn "$name" "$hint"
    fi
}

# ── Отправка в Telegram ───────────────────────────────────────────────────────

send_telegram() {
    local text="$1"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]] && return 0
    curl -sf --max-time 15 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        -d "parse_mode=Markdown" \
        > /dev/null 2>&1 || true
}

# ── СТАРТ ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║   Post-Install Check — VPN Infrastructure ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
log_info "Хост: $(hostname) | Ядро: $(uname -r) | $(date '+%Y-%m-%d %H:%M:%S')"
log_info "Внешний IP: $(curl -sf --max-time 5 https://api.ipify.org 2>/dev/null || echo 'недоступен')"

REPORT_LINES+=("🔍 *Post-Install отчёт* — $(hostname)")
REPORT_LINES+=("$(date '+%Y-%m-%d %H:%M:%S') | Ядро: $(uname -r)")

# ═══════════════════════════════════════════════════════════════════════════════
section "1. Системные сервисы"
# ═══════════════════════════════════════════════════════════════════════════════

check "nftables"          "systemctl is-active nftables"             "systemctl restart nftables"
check "dnsmasq"           "systemctl is-active dnsmasq"              "journalctl -u dnsmasq -n 20"
check "watchdog"          "systemctl is-active watchdog"             "journalctl -u watchdog -n 20"
check "hysteria2"         "systemctl is-active hysteria2"            "journalctl -u hysteria2 -n 10"
check "vpn-sets-restore"  "systemctl is-active vpn-sets-restore || systemctl is-failed vpn-sets-restore | grep -v failed" \
                          "systemctl status vpn-sets-restore"
check_warn "vpn-routes"   "systemctl is-active vpn-routes"           "нужен tier-2 туннель"
check_warn "autossh-vpn"  "systemctl is-active autossh-vpn"          "нужен tier-2 туннель"

# ═══════════════════════════════════════════════════════════════════════════════
section "2. WireGuard интерфейсы"
# ═══════════════════════════════════════════════════════════════════════════════

# Systemd-юниты
check "awg-quick@wg0 (systemd)" "systemctl is-active awg-quick@wg0"    "systemctl restart awg-quick@wg0"
check "wg-quick@wg1 (systemd)"  "systemctl is-active wg-quick@wg1"     "systemctl restart wg-quick@wg1"

# Интерфейсы подняты
check "wg0 интерфейс UP"  "ip link show wg0 2>/dev/null | grep -q UP"  "awg show wg0"
check "wg1 интерфейс UP"  "ip link show wg1 2>/dev/null | grep -q UP"  "wg show wg1"

# UDP порты слушают
check "UDP 51820 (AWG)"   "ss -ulnp | grep -q ':51820 '"               "awg show wg0"
check "UDP 51821 (WG)"    "ss -ulnp | grep -q ':51821 '"               "wg show wg1"

# IP адреса интерфейсов
WG0_IP=$(ip addr show wg0 2>/dev/null | awk '/inet /{print $2}' | head -1 || echo "нет")
WG1_IP=$(ip addr show wg1 2>/dev/null | awk '/inet /{print $2}' | head -1 || echo "нет")
log_info "wg0 IP: ${WG0_IP}  |  wg1 IP: ${WG1_IP}"
REPORT_LINES+=("   wg0: ${WG0_IP} | wg1: ${WG1_IP}")

# ═══════════════════════════════════════════════════════════════════════════════
section "3. nftables — наборы и правила"
# ═══════════════════════════════════════════════════════════════════════════════

check "nft table inet vpn"      "nft list table inet vpn"                "nft -f /etc/nftables.conf"
check "set blocked_static"      "nft list set inet vpn blocked_static"   "проверьте /etc/nftables-blocked-static.conf"
check "set blocked_dynamic"     "nft list set inet vpn blocked_dynamic"  ""

STATIC_COUNT=$(nft list set inet vpn blocked_static 2>/dev/null | grep -c 'elements' || echo 0)
DYNAMIC_COUNT=$(nft list set inet vpn blocked_dynamic 2>/dev/null | grep -c 'elements' || echo 0)
log_info "blocked_static элементов: $(nft list set inet vpn blocked_static 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | wc -l)"
log_info "blocked_dynamic элементов: $(nft list set inet vpn blocked_dynamic 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | wc -l)"

# ═══════════════════════════════════════════════════════════════════════════════
section "4. DNS"
# ═══════════════════════════════════════════════════════════════════════════════

check "DNS 127.0.0.1 → google.com"  "dig @127.0.0.1 google.com +short +time=5 | grep -q '\\.'" \
                                     "dnsmasq не отвечает"
check_warn "DNS заблокированный"    "dig @127.0.0.1 youtube.com +short +time=5 | grep -q '\\.'" \
                                     "нужен tier-2 для резолва через VPS"

DNS_RESP=$(dig @127.0.0.1 google.com +short +time=3 2>/dev/null | head -1 || echo "—")
log_info "google.com → ${DNS_RESP}"

# ═══════════════════════════════════════════════════════════════════════════════
section "5. Docker контейнеры (домашний сервер)"
# ═══════════════════════════════════════════════════════════════════════════════

docker_running() { docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

for cname in telegram-bot socket-proxy xray-client xray-client-2 xray-client-cdn \
             node-exporter prometheus grafana alertmanager nginx; do
    if docker_running "$cname"; then
        ok "docker: $cname"
    else
        STATUS=$(docker inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo "не найден")
        fail "docker: $cname" "$STATUS"
    fi
done

# ═══════════════════════════════════════════════════════════════════════════════
section "6. Xray клиенты (SOCKS5)"
# ═══════════════════════════════════════════════════════════════════════════════

check "xray-client  SOCKS5 :1080"  "nc -z 127.0.0.1 1080"  "docker logs xray-client"
check "xray-client-2 SOCKS5 :1081" "nc -z 127.0.0.1 1081"  "docker logs xray-client-2"
check "xray-client-cdn SOCKS5 :1082" "nc -z 127.0.0.1 1082" "docker logs xray-client-cdn"

# ═══════════════════════════════════════════════════════════════════════════════
section "7. Watchdog API"
# ═══════════════════════════════════════════════════════════════════════════════

WATCHDOG_TOKEN="${WATCHDOG_API_TOKEN:-}"
if [[ -n "$WATCHDOG_TOKEN" ]]; then
    check "watchdog /status" \
        "curl -sf --max-time 5 -H 'Authorization: Bearer ${WATCHDOG_TOKEN}' http://127.0.0.1:8080/status" \
        "journalctl -u watchdog -n 20"
    check "watchdog /metrics" \
        "curl -sf --max-time 5 -H 'Authorization: Bearer ${WATCHDOG_TOKEN}' http://127.0.0.1:8080/metrics" \
        ""
else
    warn "watchdog API" "WATCHDOG_API_TOKEN не задан"
fi

# ═══════════════════════════════════════════════════════════════════════════════
section "8. Мониторинг"
# ═══════════════════════════════════════════════════════════════════════════════

check "Prometheus :9090"    "curl -sf --max-time 5 http://172.20.0.30:9090/-/healthy"   "docker logs prometheus"
check "Grafana :3000"       "curl -sf --max-time 5 http://172.20.0.32:3000/api/health"  "docker logs grafana"
check "Alertmanager :9093"  "curl -sf --max-time 5 http://172.20.0.31:9093/-/healthy"   "docker logs alertmanager"
check "node-exporter :9100" "curl -sf --max-time 5 http://127.0.0.1:9100/metrics"       "docker logs node-exporter"

# ═══════════════════════════════════════════════════════════════════════════════
section "9. VPS подключение"
# ═══════════════════════════════════════════════════════════════════════════════

VPS_IP="${VPS_IP:-}"
SSH_KEY="/root/.ssh/vpn_id_ed25519"

if [[ -n "$VPS_IP" && -f "$SSH_KEY" ]]; then
    check "SSH к VPS (sysadmin)" \
        "ssh -i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes sysadmin@${VPS_IP} echo ok" \
        "проверьте SSH ключ и доступность VPS"

    check_warn "Ping tier-2 (10.177.2.2)" \
        "ping -c 2 -W 3 10.177.2.2" \
        "tier-2 WG туннель не поднят"

    # Docker на VPS
    VPS_CONTAINERS=$(ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
        "sysadmin@${VPS_IP}" \
        "sudo docker ps --format '{{.Names}}:{{.Status}}' 2>/dev/null" 2>/dev/null || echo "")

    if [[ -n "$VPS_CONTAINERS" ]]; then
        for cname in 3x-ui hysteria2 cloudflared node-exporter; do
            if echo "$VPS_CONTAINERS" | grep -q "^${cname}:Up"; then
                ok "VPS docker: $cname"
            else
                STATUS=$(echo "$VPS_CONTAINERS" | grep "^${cname}:" | cut -d: -f2 || echo "не найден")
                fail "VPS docker: $cname" "${STATUS:-не найден}"
            fi
        done
    else
        warn "VPS docker контейнеры" "не удалось получить список"
    fi
else
    warn "VPS проверка" "VPS_IP не задан или SSH ключ отсутствует"
fi

# ═══════════════════════════════════════════════════════════════════════════════
section "10. DKMS — AmneziaWG модуль"
# ═══════════════════════════════════════════════════════════════════════════════

KERNEL=$(uname -r)
if dkms status 2>/dev/null | grep -qi "amneziawg"; then
    DKMS_STATUS=$(dkms status 2>/dev/null | grep -i amneziawg | head -1)
    if echo "$DKMS_STATUS" | grep -qi "installed"; then
        ok "DKMS amneziawg (ядро: ${KERNEL})"
    else
        warn "DKMS amneziawg" "$DKMS_STATUS"
    fi
else
    fail "DKMS amneziawg" "модуль не найден — awg не будет работать после смены ядра"
fi

# ═══════════════════════════════════════════════════════════════════════════════
section "11. Диск и ресурсы"
# ═══════════════════════════════════════════════════════════════════════════════

DISK_USE=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
DISK_FREE=$(df -h / | awk 'NR==2 {print $4}')
RAM_USE=$(free -m | awk '/^Mem:/{printf "%.0f%%", $3/$2*100}')

if (( DISK_USE < 80 )); then
    ok "Диск: ${DISK_USE}% (свободно ${DISK_FREE})"
elif (( DISK_USE < 90 )); then
    warn "Диск: ${DISK_USE}% — скоро заполнится (свободно ${DISK_FREE})"
else
    fail "Диск: ${DISK_USE}% — критически мало (свободно ${DISK_FREE})"
fi

log_info "RAM: ${RAM_USE} | Uptime: $(uptime -p)"
REPORT_LINES+=("   Диск: ${DISK_USE}% | RAM: ${RAM_USE}")

# ═══════════════════════════════════════════════════════════════════════════════
section "12. Telegram бот"
# ═══════════════════════════════════════════════════════════════════════════════

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    BOT_INFO=$(curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || echo "")
    if echo "$BOT_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
        BOT_NAME=$(echo "$BOT_INFO" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print(d['result'].get('username','?'))" 2>/dev/null)
        ok "Telegram API (@${BOT_NAME})"
    else
        fail "Telegram API" "токен невалиден или API недоступен"
    fi
else
    fail "Telegram API" "TELEGRAM_BOT_TOKEN не задан"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# ИТОГ
# ═══════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${CYAN}${BOLD}━━━ ИТОГ ━━━${NC}"

TOTAL=$((PASS + FAIL + WARN))
if (( FAIL == 0 && WARN == 0 )); then
    STATUS_EMOJI="✅"
    STATUS_TEXT="Всё работает"
    echo -e "${GREEN}${BOLD}✅ Все проверки пройдены! (${PASS}/${TOTAL})${NC}"
elif (( FAIL == 0 )); then
    STATUS_EMOJI="⚠️"
    STATUS_TEXT="Есть предупреждения"
    echo -e "${YELLOW}${BOLD}⚠️  Прошло: ${PASS}, Предупреждения: ${WARN} (из ${TOTAL})${NC}"
else
    STATUS_EMOJI="❌"
    STATUS_TEXT="Есть ошибки"
    echo -e "${RED}${BOLD}❌ Прошло: ${PASS}, Ошибок: ${FAIL}, Предупреждений: ${WARN} (из ${TOTAL})${NC}"
fi

echo ""

# ── Отправка в Telegram ───────────────────────────────────────────────────────

REPORT_LINES+=("")
REPORT_LINES+=("${STATUS_EMOJI} *Итог:* ✅${PASS} ❌${FAIL} ⚠️${WARN} из ${TOTAL}")
REPORT_LINES+=("Ядро: \`${KERNEL}\` | $(date '+%H:%M %d.%m.%Y')")

TELEGRAM_TEXT=$(printf '%s\n' "${REPORT_LINES[@]}")

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]]; then
    log_info "Отправка отчёта в Telegram..."
    send_telegram "$TELEGRAM_TEXT"
    log_ok "Отчёт отправлен администратору"
else
    log_warn "TELEGRAM_BOT_TOKEN или TELEGRAM_ADMIN_CHAT_ID не заданы — отчёт не отправлен"
    echo ""
    echo "=== Текст отчёта ==="
    echo "$TELEGRAM_TEXT"
fi

echo ""
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

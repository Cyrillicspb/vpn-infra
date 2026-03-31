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
    PASS=$((PASS+1))
    REPORT_LINES+=("✅ $name")
}

fail() {
    local name="$1" hint="${2:-}"
    log_fail "$name${hint:+ — $hint}"
    FAIL=$((FAIL+1))
    REPORT_LINES+=("❌ $name${hint:+ (${hint})}")
}

warn() {
    local name="$1" hint="${2:-}"
    log_warn "$name${hint:+ — $hint}"
    WARN=$((WARN+1))
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

check_sync_pair() {
    local name="$1" src="$2" dst="$3" hint="${4:-}"
    if [[ ! -f "$src" ]]; then
        warn "$name" "source отсутствует: $src"
        return
    fi
    if [[ ! -f "$dst" ]]; then
        fail "$name" "${hint:-target отсутствует: $dst}"
        return
    fi
    if cmp -s "$src" "$dst"; then
        ok "$name"
    else
        fail "$name" "$hint"
    fi
}

# ── Отправка в Telegram ───────────────────────────────────────────────────────

send_telegram() {
    local text="$1"
    local notify_url="${BOT_NOTIFY_URL:-http://172.20.0.11:8090/notify}"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]] && return 0

    # Предпочитаем notify relay через telegram-bot контейнер:
    # хостовый direct egress до Telegram может быть закрыт политикой маршрутизации,
    # тогда сам бот всё равно остаётся рабочим.
    if [[ -n "${WATCHDOG_API_TOKEN:-}" ]] && curl -sf --max-time 10 \
        -H "Authorization: Bearer ${WATCHDOG_API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$(python3 - "$text" <<'PY'
import json, sys
print(json.dumps({"message": sys.argv[1], "target": "admin"}))
PY
)" \
        "${notify_url}" > /dev/null 2>&1; then
        return 0
    fi

    curl -sf --max-time 15 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        -d "parse_mode=Markdown" \
        > /dev/null 2>&1
}

active_socks_works() {
    local port="$1"
    local code=""
    code="$(curl -sS --max-time 10 --socks5 "127.0.0.1:${port}" \
        -o /dev/null -w "%{http_code}" \
        https://registry-1.docker.io/v2/ 2>/dev/null || true)"
    [[ "$code" =~ ^(200|204|301|302|401)$ ]]
}

telegram_getme_json() {
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "telegram-bot"; then
        docker exec -i telegram-bot python - <<'PY' 2>/dev/null || true
import os
import sys
import urllib.error
import urllib.request

url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN', '')}/getMe"
try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        sys.stdout.write(resp.read().decode())
except urllib.error.HTTPError as exc:
    if exc.fp is not None:
        sys.stdout.write(exc.fp.read().decode())
except Exception:
    pass
PY
        return 0
    fi

    curl -sS --max-time 15 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true
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

section "0. Source Of Truth"

if [[ -d /opt/vpn/.git ]]; then
    check "repo HEAD attached" \
        "git -C /opt/vpn symbolic-ref --quiet HEAD" \
        "detached HEAD маскирует drift и ломает воспроизводимый deploy/reinstall"
    check_warn "repo tracked source clean" \
        "! git -C /opt/vpn status --porcelain --untracked-files=no -- \
            home/scripts home/systemd home/watchdog install-home.sh install-vps.sh setup.sh deploy.sh restore.sh \
            home/docker-compose.yml home/nftables/nftables.conf | grep -q '.'" \
        "tracked source tree в /opt/vpn dirty — live runtime и installer могут разойтись"
else
    warn "repo git metadata" "/opt/vpn/.git отсутствует"
fi

check_sync_pair "runtime sync: post-install-check.sh" \
    "/opt/vpn/home/scripts/post-install-check.sh" \
    "/opt/vpn/scripts/post-install-check.sh" \
    "active /opt/vpn/scripts/post-install-check.sh отстаёт от source tree"
check_sync_pair "runtime sync: generate-nftables.sh" \
    "/opt/vpn/home/scripts/generate-nftables.sh" \
    "/opt/vpn/scripts/generate-nftables.sh" \
    "active generate-nftables.sh не совпадает с source tree"
check_sync_pair "runtime sync: update-routes.py" \
    "/opt/vpn/home/scripts/update-routes.py" \
    "/opt/vpn/scripts/update-routes.py" \
    "active update-routes.py не совпадает с source tree"
check_sync_pair "runtime sync: watchdog-stop-cleanup.sh" \
    "/opt/vpn/home/scripts/watchdog-stop-cleanup.sh" \
    "/opt/vpn/scripts/watchdog-stop-cleanup.sh" \
    "active watchdog-stop-cleanup.sh не совпадает с source tree"
check_sync_pair "runtime sync: watchdog.py" \
    "/opt/vpn/home/watchdog/watchdog.py" \
    "/opt/vpn/watchdog/watchdog.py" \
    "active watchdog.py не совпадает с source tree"
check_sync_pair "runtime sync: watchdog base.py" \
    "/opt/vpn/home/watchdog/plugins/base.py" \
    "/opt/vpn/watchdog/plugins/base.py" \
    "active watchdog base.py не совпадает с source tree"
check_sync_pair "runtime sync: zapret client.py" \
    "/opt/vpn/home/watchdog/plugins/zapret/client.py" \
    "/opt/vpn/watchdog/plugins/zapret/client.py" \
    "active zapret client.py не совпадает с source tree"
check_sync_pair "runtime sync: zapret probe.py" \
    "/opt/vpn/home/watchdog/plugins/zapret/probe.py" \
    "/opt/vpn/watchdog/plugins/zapret/probe.py" \
    "active zapret probe.py не совпадает с source tree"
check_sync_pair "runtime sync: watchdog.service" \
    "/opt/vpn/home/systemd/watchdog.service" \
    "/etc/systemd/system/watchdog.service" \
    "installed watchdog.service не совпадает с source tree"
check_sync_pair "runtime sync: hysteria2.service" \
    "/opt/vpn/home/systemd/hysteria2.service" \
    "/etc/systemd/system/hysteria2.service" \
    "installed hysteria2.service не совпадает с source tree"
check_sync_pair "runtime sync: autossh-vpn.service" \
    "/opt/vpn/home/systemd/autossh-vpn.service" \
    "/etc/systemd/system/autossh-vpn.service" \
    "installed autossh-vpn.service не совпадает с source tree"
check_sync_pair "runtime sync: vpn-postboot.service" \
    "/opt/vpn/home/systemd/vpn-postboot.service" \
    "/etc/systemd/system/vpn-postboot.service" \
    "installed vpn-postboot.service не совпадает с source tree"

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
if systemctl list-unit-files autossh-tier2.service >/dev/null 2>&1; then
    check_warn "autossh-tier2" "systemctl is-active autossh-tier2" "нужен tier-2 туннель"
elif systemctl list-unit-files autossh-vpn.service >/dev/null 2>&1; then
    check_warn "autossh-vpn" "systemctl is-active autossh-vpn" "fallback SOCKS tunnel не поднят"
else
    warn "autossh" "юнит не найден"
fi

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

BLOCKED_TEST_DOMAIN="$(
    awk -F'/' '/^server=\// {print $2; exit}' /etc/dnsmasq.d/vpn-force.conf /etc/dnsmasq.d/vpn-domains.conf 2>/dev/null
)"
if [[ -n "$BLOCKED_TEST_DOMAIN" ]]; then
    log_info "blocked DNS test domain: ${BLOCKED_TEST_DOMAIN}"
    check_warn "DNS blocked @127.0.0.1 → ${BLOCKED_TEST_DOMAIN}" \
        "dig @127.0.0.1 ${BLOCKED_TEST_DOMAIN} +short +time=5 | grep -q '\\.'" \
        "dnsmasq не смог резолвить blocked domain через tier-2/VPS DNS"
    if [[ -n "${VPS_TUNNEL_IP:-}" ]]; then
        check_warn "DNS Tier-2 @${VPS_TUNNEL_IP} → ${BLOCKED_TEST_DOMAIN}" \
            "dig @${VPS_TUNNEL_IP} ${BLOCKED_TEST_DOMAIN} +short +time=5 | grep -q '\\.'" \
            "DNS на VPS tunnel endpoint не отвечает для blocked domain"
    fi
else
    warn "DNS blocked test domain" "не найден server=/... в /etc/dnsmasq.d/vpn-force.conf или vpn-domains.conf"
fi

if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${LAN_IFACE:-}" ]]; then
    check "send_redirects default = 0" \
        "[[ \"$(sysctl -n net.ipv4.conf.default.send_redirects 2>/dev/null)\" == \"0\" ]]" \
        "иначе новые интерфейсы могут раздавать ICMP redirects и обходить home-server"
    check "send_redirects ${LAN_IFACE} = 0" \
        "[[ \"$(sysctl -n net.ipv4.conf.${LAN_IFACE}.send_redirects 2>/dev/null)\" == \"0\" ]]" \
        "LAN-клиенты могут получить ICMP redirect на upstream router и обойти gateway policy"
fi

# ═══════════════════════════════════════════════════════════════════════════════
section "5. Docker контейнеры (домашний сервер)"
# ═══════════════════════════════════════════════════════════════════════════════

docker_running() { docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }
docker_exists()  { docker inspect "$1" &>/dev/null 2>&1; }

# Фаза 1 — критичные (FAIL если не running)
for cname in telegram-bot socket-proxy xray-client-xhttp xray-client-vision xray-client-cdn nginx; do
    if docker_running "$cname"; then
        ok "docker: $cname"
    else
        STATUS=$(docker inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo "не найден")
        fail "docker: $cname" "$STATUS"
    fi
done

for cname in prometheus grafana alertmanager node-exporter; do
    if docker_running "$cname"; then
        ok "мониторинг: $cname"
    else
        STATUS=$(docker inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo "не найден")
        fail "мониторинг: $cname" "$STATUS"
    fi
done

# ═══════════════════════════════════════════════════════════════════════════════
section "6. Xray клиенты (SOCKS5)"
# ═══════════════════════════════════════════════════════════════════════════════

check "xray-client-xhttp SOCKS5 :1081" "nc -z 127.0.0.1 1081"  "docker logs xray-client-xhttp"
check "xray-client-vision SOCKS5 :1084" "nc -z 127.0.0.1 1084" "docker logs xray-client-vision"
check "xray-client-cdn SOCKS5 :1082" "nc -z 127.0.0.1 1082" "docker logs xray-client-cdn"

# ═══════════════════════════════════════════════════════════════════════════════
section "7. Watchdog API"
# ═══════════════════════════════════════════════════════════════════════════════

WATCHDOG_TOKEN="${WATCHDOG_API_TOKEN:-}"
WATCHDOG_STATUS_JSON=""
if [[ -n "$WATCHDOG_TOKEN" ]]; then
    check "watchdog /status" \
        "curl -sf --max-time 5 -H 'Authorization: Bearer ${WATCHDOG_TOKEN}' http://127.0.0.1:8080/status" \
        "journalctl -u watchdog -n 20"
    check "watchdog /metrics" \
        "curl -sf --max-time 5 -H 'Authorization: Bearer ${WATCHDOG_TOKEN}' http://127.0.0.1:8080/metrics" \
        ""
    WATCHDOG_STATUS_JSON=$(curl -sf --max-time 5 -H "Authorization: Bearer ${WATCHDOG_TOKEN}" \
        http://127.0.0.1:8080/status 2>/dev/null || echo "")
else
    warn "watchdog API" "WATCHDOG_API_TOKEN не задан"
fi

check "watchdog KillMode != process" \
    "! systemctl cat watchdog 2>/dev/null | grep -q '^KillMode=process$'" \
    "watchdog stop/restart будет оставлять tun2socks/nfqws как left-over процессы"
check "watchdog ExecStopPost cleanup" \
    "systemctl cat watchdog 2>/dev/null | grep -q '^ExecStopPost=/opt/vpn/scripts/watchdog-stop-cleanup.sh$'" \
    "после stop останутся stale vpn-active/pid файлы и старый route table marked"
check_warn "watchdog sd_notify noise" \
    "! journalctl -u watchdog -n 10 --no-pager | grep -q 'Got notification message from PID'" \
    "дочерние процессы watchdog всё ещё наследуют NOTIFY_SOCKET/WATCHDOG_*"

ACTIVE_STACK=""
if [[ -n "$WATCHDOG_STATUS_JSON" ]]; then
    ACTIVE_STACK=$(echo "$WATCHDOG_STATUS_JSON" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('active_stack',''))" 2>/dev/null || true)
fi

ACTIVE_SOCKS_PORT=""
case "$ACTIVE_STACK" in
    reality-xhttp) ACTIVE_SOCKS_PORT="1081" ;;
    cloudflare-cdn) ACTIVE_SOCKS_PORT="1082" ;;
    hysteria2) ACTIVE_SOCKS_PORT="1083" ;;
    vless-reality-vision) ACTIVE_SOCKS_PORT="1084" ;;
esac

if [[ -n "$ACTIVE_SOCKS_PORT" ]]; then
    check "Активный SOCKS5 :${ACTIVE_SOCKS_PORT}" \
        "active_socks_works ${ACTIVE_SOCKS_PORT}" \
        "watchdog поднял стек ${ACTIVE_STACK}, но активный SOCKS5 не работает"
fi

if ip route show table 200 2>/dev/null | grep -q "^unreachable default"; then
    fail "table 200" "default route = unreachable (watchdog не поднял рабочий tun)"
else
    ok "table 200"
fi

if [[ -f /run/vpn-active-tun ]]; then
    ACTIVE_TUN="$(cat /run/vpn-active-tun 2>/dev/null || true)"
    if [[ -n "$ACTIVE_TUN" ]] && ip route show table 200 2>/dev/null | grep -q "dev ${ACTIVE_TUN}"; then
        ok "vpn-active-tun (${ACTIVE_TUN})"
    else
        fail "vpn-active-tun" "${ACTIVE_TUN:-пусто} не совпадает с table 200"
    fi
fi

if [[ -n "${VPS_IP:-}" ]]; then
    OUTPUT_CHAIN="$(nft list chain inet vpn output 2>/dev/null || true)"
    PREROUTING_CHAIN="$(nft list chain inet vpn prerouting 2>/dev/null || true)"
    if grep -q 'ip daddr @control_direct_ips accept' <<<"$OUTPUT_CHAIN" && \
       grep -q 'iifname "br-vpn" ip daddr @control_direct_ips accept' <<<"$PREROUTING_CHAIN"; then
        ok "VPS control-plane route"
    else
        fail "VPS control-plane route" "нет nft bypass для control_direct_ips → возможен self-tunneling loop к VPS"
    fi
fi

if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${LAN_IFACE:-}" && -n "${LAN_SUBNET:-}" ]]; then
    POSTROUTING_CHAIN="$(nft list chain inet vpn postrouting 2>/dev/null || true)"
    if grep -q "ip saddr ${LAN_SUBNET} ip daddr != ${LAN_SUBNET} oifname \"${LAN_IFACE}\" masquerade" <<<"$POSTROUTING_CHAIN"; then
        ok "LAN direct masquerade"
    else
        fail "LAN direct masquerade" "direct LAN egress останется асимметричным и обойдёт stateful path home-server"
    fi
fi

if [[ -x /usr/local/bin/nfqws ]]; then
    ok "zapret/nfqws binary"
else
    fail "zapret/nfqws binary" "/usr/local/bin/nfqws отсутствует"
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
        "SSH tier-2 туннель не поднят (autossh-tier2)"

    # Docker на VPS
    VPS_CONTAINERS=$(ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
        "sysadmin@${VPS_IP}" \
        "sudo docker ps --format '{{.Names}}:{{.Status}}' 2>/dev/null" 2>/dev/null || echo "")

    if [[ -n "$VPS_CONTAINERS" ]]; then
        for cname in 3x-ui xray-reality-vision xray-reality-xhttp hysteria2 node-exporter; do
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

# Проверка Secure Boot — модуль amneziawg не загрузится если включён
SB_STATE=$(mokutil --sb-state 2>/dev/null || echo "unknown")
if echo "$SB_STATE" | grep -qi "SecureBoot enabled"; then
    warn "Secure Boot" "ВКЛЮЧЁН — модуль amneziawg не загрузится. Отключите в настройках VM/BIOS: Proxmox VM → Оборудование → BIOS → снять Secure Boot"
fi

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
    BOT_INFO="$(telegram_getme_json)"
    if echo "$BOT_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
        BOT_NAME=$(echo "$BOT_INFO" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print(d['result'].get('username','?'))" 2>/dev/null)
        ok "Telegram API (@${BOT_NAME})"
    else
        fail "Telegram API" "бот не может выполнить getMe (невалидный токен или нет egress из container)"
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
    if send_telegram "$TELEGRAM_TEXT"; then
        log_ok "Отчёт отправлен администратору"
    else
        log_warn "Отчёт в Telegram не отправлен"
    fi
else
    log_warn "TELEGRAM_BOT_TOKEN или TELEGRAM_ADMIN_CHAT_ID не заданы — отчёт не отправлен"
    echo ""
    echo "=== Текст отчёта ==="
    echo "$TELEGRAM_TEXT"
fi

echo ""
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

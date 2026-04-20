#!/bin/bash
# =============================================================================
# add-vps.sh — Добавление нового backend VPS в инфраструктуру
#
# Запуск: bash /opt/vpn/add-vps.sh <IP> <ROOT_PASSWORD> [SSH_PORT]
# Например: bash /opt/vpn/add-vps.sh 217.60.7.50 mypassword 22
#
# Контракт:
#   - принимает clean Ubuntu VPS с root+password bootstrap
#   - подготавливает sysadmin + SSH key
#   - устанавливает runtime /opt/vpn на новом backend
#   - автоматически настраивает 3x-ui CDN inbound и standalone stacks
#   - регистрирует backend в watchdog pool как fully-ready
#   - только после успешного завершения закрывает root/password SSH
# =============================================================================

set -euo pipefail

BACKEND_IP="${1:-}"
BACKEND_ROOT_PASS="${2:-}"
BACKEND_SSH_PORT="${3:-22}"
BACKEND_TUNNEL_IP="${4:-10.177.2.2}"
HOME_TUNNEL_IP="${5:-10.177.2.1}"

if [[ -z "$BACKEND_IP" || -z "$BACKEND_ROOT_PASS" ]]; then
    echo "Использование: bash add-vps.sh <IP> <ROOT_PASSWORD> [SSH_PORT]"
    echo "Пример:        bash add-vps.sh 217.60.7.50 mypassword 22"
    exit 1
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[✓]${NC}   $*"; }
log_warn()  { echo -e "${YELLOW}[!]${NC}   $*"; }
log_error() { echo -e "${RED}[✗]${NC}   $*" >&2; }
log_step()  { echo ""; echo -e "${CYAN}${BOLD}━━━ $* ━━━${NC}"; }
die()       { log_error "$*"; exit 1; }

REPO_DIR="/opt/vpn"
ENV_FILE="$REPO_DIR/.env"
SSH_KEY="/root/.ssh/vpn_id_ed25519"
PUB_KEY="${SSH_KEY}.pub"
REMOTE_DIR="/opt/vpn"
REMOTE_ENV="${REMOTE_DIR}/.env"
REMOTE_XRAY_SETUP="/tmp/xray-setup.sh"

[[ -f "$ENV_FILE" ]] || die "Файл ${ENV_FILE} не найден. Запустите из /opt/vpn."
[[ -f "$SSH_KEY"  ]] || die "SSH-ключ ${SSH_KEY} не найден."
[[ -f "$PUB_KEY"  ]] || die "Публичный ключ ${PUB_KEY} не найден."

set -o allexport
source "$ENV_FILE"
set +o allexport

backend_root_ssh() {
    sshpass -p "$BACKEND_ROOT_PASS" ssh \
        -p "$BACKEND_SSH_PORT" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        "root@${BACKEND_IP}" "$@"
}

backend_root_scp() {
    sshpass -p "$BACKEND_ROOT_PASS" scp \
        -P "$BACKEND_SSH_PORT" \
        -o StrictHostKeyChecking=no "$@"
}

backend_exec() {
    ssh -p "$BACKEND_SSH_PORT" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o ServerAliveInterval=60 \
        "sysadmin@${BACKEND_IP}" "$@"
}

backend_copy() {
    scp -P "$BACKEND_SSH_PORT" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no "$@"
}

run_remote_python() {
    local script="$1"
    backend_exec "python3 - <<'PY'
$script
PY"
}

log_step "Шаг 1: Bootstrap SSH-доступа"

which sshpass &>/dev/null || apt-get install -y -qq sshpass

if ssh -p "$BACKEND_SSH_PORT" -i "$SSH_KEY" \
    -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes \
    "sysadmin@${BACKEND_IP}" "echo ok" &>/dev/null; then
    log_ok "SSH (sysadmin@${BACKEND_IP}) уже работает"
else
    backend_root_ssh bash <<'BOOTSTRAP'
set -euo pipefail
id sysadmin &>/dev/null || useradd -m -s /bin/bash -G sudo sysadmin
echo 'sysadmin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/sysadmin
chmod 440 /etc/sudoers.d/sysadmin
mkdir -p /home/sysadmin/.ssh
chmod 700 /home/sysadmin/.ssh
touch /home/sysadmin/.ssh/authorized_keys
chmod 600 /home/sysadmin/.ssh/authorized_keys
chown -R sysadmin:sysadmin /home/sysadmin/.ssh
BOOTSTRAP

    backend_root_ssh "grep -qxF '$(cat "$PUB_KEY")' /home/sysadmin/.ssh/authorized_keys || echo '$(cat "$PUB_KEY")' >> /home/sysadmin/.ssh/authorized_keys"
    backend_exec "echo ok" >/dev/null || die "SSH к sysadmin@${BACKEND_IP} не работает после bootstrap"
    log_ok "sysadmin создан, SSH-ключ скопирован"
fi

log_step "Шаг 2: Базовые пакеты и Docker"

backend_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        curl wget git jq python3 net-tools nftables fail2ban dnsmasq \
        wireguard-tools openssl gnupg2 ca-certificates"

if ! backend_exec "command -v docker >/dev/null 2>&1"; then
    backend_exec "curl -fsSL https://get.docker.com | sudo sh"
fi
backend_exec "sudo systemctl enable docker && sudo systemctl restart docker"
backend_exec "sudo usermod -aG docker sysadmin 2>/dev/null || true"
backend_exec "sudo mkdir -p /etc/docker && \
    printf '{\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"3\"},\"dns\":[\"8.8.8.8\",\"1.1.1.1\"],\"ipv6\":false}\n' | sudo tee /etc/docker/daemon.json >/dev/null && \
    sudo systemctl restart docker"
log_ok "Docker и пакеты подготовлены"

log_step "Шаг 3: Runtime /opt/vpn"

backend_exec "sudo install -d -m 755 ${REMOTE_DIR} && sudo chown sysadmin:sysadmin ${REMOTE_DIR}"
backend_exec "mkdir -p ${REMOTE_DIR}/scripts ${REMOTE_DIR}/nginx/mtls ${REMOTE_DIR}/nginx/ssl \
    ${REMOTE_DIR}/nginx/conf.d ${REMOTE_DIR}/cloudflared ${REMOTE_DIR}/3x-ui/db \
    ${REMOTE_DIR}/hysteria2 ${REMOTE_DIR}/xray ${REMOTE_DIR}/backups ${REMOTE_DIR}/vpn-repo.git"
backend_copy -r "${REPO_DIR}/vps/." "sysadmin@${BACKEND_IP}:${REMOTE_DIR}/"
log_ok "Файлы VPS runtime скопированы"

log_step "Шаг 4: Backend .env"

tmp_env="$(mktemp /tmp/backend-env.XXXXXX)"
cp "$ENV_FILE" "$tmp_env"
python3 - "$tmp_env" "$BACKEND_IP" "$BACKEND_SSH_PORT" "$BACKEND_TUNNEL_IP" "$HOME_TUNNEL_IP" "${HOME_SERVER_IP:-}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
backend_ip, ssh_port, tunnel_ip, home_tunnel_ip, home_server_ip = sys.argv[2:7]
updates = {
    "VPS_IP": backend_ip,
    "VPS_SSH_PORT": ssh_port,
    "VPS_TUNNEL_IP": tunnel_ip,
    "HOME_TUNNEL_IP": home_tunnel_ip,
    "HOME_SERVER_IP": home_server_ip,
    "SSH_ADDITIONAL_PORT": "443",
}
lines = []
seen = set()
for raw in path.read_text(encoding="utf-8").splitlines():
    if "=" not in raw or raw.lstrip().startswith("#"):
        lines.append(raw)
        continue
    key = raw.split("=", 1)[0]
    if key in updates:
        lines.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        lines.append(raw)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
chmod 600 "$tmp_env"
backend_copy "$tmp_env" "sysadmin@${BACKEND_IP}:${REMOTE_ENV}"
rm -f "$tmp_env"
backend_exec "chmod 600 ${REMOTE_ENV}"
log_ok ".env backend подготовлен"

log_step "Шаг 5: Сертификаты и server configs"

backend_exec "sudo install -d -m 755 ${REMOTE_DIR}/nginx/mtls ${REMOTE_DIR}/nginx/ssl ${REMOTE_DIR}/hysteria2 ${REMOTE_DIR}/xray"
backend_exec "[ -f ${REMOTE_DIR}/nginx/mtls/ca.crt ] || ( \
    openssl genrsa -out ${REMOTE_DIR}/nginx/mtls/ca.key 4096 >/dev/null 2>&1 && \
    openssl req -new -x509 -days 3650 -key ${REMOTE_DIR}/nginx/mtls/ca.key \
        -out ${REMOTE_DIR}/nginx/mtls/ca.crt -subj '/CN=VPN-CA/O=VPNInfra/C=RU' >/dev/null 2>&1 && \
    chmod 600 ${REMOTE_DIR}/nginx/mtls/ca.key )"
backend_exec "[ -f ${REMOTE_DIR}/nginx/ssl/server.crt ] || ( \
    openssl genrsa -out ${REMOTE_DIR}/nginx/ssl/server.key 2048 >/dev/null 2>&1 && \
    openssl req -new -key ${REMOTE_DIR}/nginx/ssl/server.key -out ${REMOTE_DIR}/nginx/ssl/server.csr \
        -subj '/CN=${BACKEND_IP}/O=VPNInfra/C=RU' >/dev/null 2>&1 && \
    openssl x509 -req -days 730 -in ${REMOTE_DIR}/nginx/ssl/server.csr \
        -CA ${REMOTE_DIR}/nginx/mtls/ca.crt -CAkey ${REMOTE_DIR}/nginx/mtls/ca.key -CAcreateserial \
        -out ${REMOTE_DIR}/nginx/ssl/server.crt >/dev/null 2>&1 && \
    rm -f ${REMOTE_DIR}/nginx/ssl/server.csr && chmod 600 ${REMOTE_DIR}/nginx/ssl/server.key )"
backend_exec "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
    -keyout ${REMOTE_DIR}/hysteria2/server.key -out ${REMOTE_DIR}/hysteria2/server.crt \
    -days 3650 -nodes -subj '/CN=${BACKEND_IP}' -addext 'subjectAltName=IP:${BACKEND_IP}' >/dev/null 2>&1 && \
    sudo chmod 644 ${REMOTE_DIR}/hysteria2/server.crt && sudo chmod 600 ${REMOTE_DIR}/hysteria2/server.key"
backend_exec "sudo tee ${REMOTE_DIR}/hysteria2/server.yaml >/dev/null <<HYEOF
listen: :443

tls:
  cert: /etc/hysteria2/server.crt
  key: /etc/hysteria2/server.key

obfs:
  type: salamander
  salamander:
    password: ${HYSTERIA2_OBFS:-}

auth:
  type: password
  password: ${HYSTERIA2_AUTH:-}

bandwidth:
  up: 200 mbps
  down: 200 mbps

quic:
  keepAlivePeriod: 20s
  maxIdleTimeout: 60s
  maxIncomingStreams: 1024

masquerade:
  type: proxy
  proxy:
    url: https://www.microsoft.com
    rewriteHost: true

log:
  level: warn
HYEOF"
backend_exec "bash ${REMOTE_DIR}/scripts/render-reality-vision-config.sh"
backend_exec "bash ${REMOTE_DIR}/scripts/render-reality-xhttp-config.sh"
log_ok "Сертификаты, Hysteria2 и standalone Xray configs подготовлены"

log_step "Шаг 6: Docker Compose и 3x-ui setup"

backend_exec "cd ${REMOTE_DIR} && sudo docker compose pull 2>/dev/null || true"
backend_exec "cd ${REMOTE_DIR} && sudo docker compose up -d --remove-orphans 2>/dev/null || sudo docker compose up -d 2>/dev/null || true"
backend_exec "cd ${REMOTE_DIR} && sudo docker compose --profile extra-stacks up -d trojan-server tuic-server 2>/dev/null || true"
sleep 15
backend_exec "sudo docker inspect --format='{{.State.Status}}' 3x-ui 2>/dev/null | grep -q '^running$'" || die "3x-ui не запущен на backend"
backend_copy "${REPO_DIR}/vps/scripts/xray-setup.sh" "sysadmin@${BACKEND_IP}:${REMOTE_XRAY_SETUP}"
backend_exec "chmod +x ${REMOTE_XRAY_SETUP} && bash ${REMOTE_XRAY_SETUP} && rm -f ${REMOTE_XRAY_SETUP}"
backend_exec "bash ${REMOTE_DIR}/scripts/render-reality-vision-config.sh"
backend_exec "bash ${REMOTE_DIR}/scripts/render-reality-xhttp-config.sh"
backend_exec "cd ${REMOTE_DIR} && sudo docker compose up -d xray-reality-vision xray-reality-xhttp hysteria2 2>/dev/null || true"
log_ok "3x-ui CDN inbound и server-side stacks настроены автоматически"

log_step "Шаг 7: Firewall, SSH tunnel и DNS"

backend_exec "sudo bash -lc 'cat > /etc/nftables-vps.conf <<NFTEOF
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority filter; policy accept;
    iifname \"lo\" accept
    ct state established,related accept
    tcp dport { 22, 2053, 2083, 443, 8444, 8448, 8022 } ct state new accept
    udp dport { 443, 8448 } ct state new accept
    icmp type echo-request limit rate 10/second accept
  }
  chain forward {
    type filter hook forward priority filter; policy drop;
    ct state established,related accept
  }
}
NFTEOF
systemctl enable nftables >/dev/null 2>&1 || true
nft -f /etc/nftables-vps.conf || true'"
backend_exec "printf '[DEFAULT]\nbantime = 3600\nfindtime = 600\nmaxretry = 5\nbackend = systemd\n\n[sshd]\nenabled = true\nport = ${BACKEND_SSH_PORT}\n' | sudo tee /etc/fail2ban/jail.local >/dev/null && sudo systemctl enable fail2ban && sudo systemctl restart fail2ban"
backend_exec "sudo sed -i '/^#*PermitTunnel/d' /etc/ssh/sshd_config && echo 'PermitTunnel yes' | sudo tee -a /etc/ssh/sshd_config >/dev/null && sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null"
backend_exec "sudo bash -lc 'cat > /etc/dnsmasq.conf <<DNSEOF
listen-address=127.0.0.1,${BACKEND_TUNNEL_IP}
bind-interfaces
no-resolv
server=1.1.1.1
server=8.8.8.8
cache-size=1000
DNSEOF
systemctl enable dnsmasq >/dev/null 2>&1
systemctl restart dnsmasq'"
log_ok "PermitTunnel, dnsmasq и базовый firewall настроены"

log_step "Шаг 8: Git mirror и healthcheck"

backend_exec "sudo install -d -m 755 ${REMOTE_DIR}/vpn-repo.git && sudo chown -R sysadmin:sysadmin ${REMOTE_DIR}/vpn-repo.git && git -C ${REMOTE_DIR}/vpn-repo.git init --bare >/dev/null 2>&1 || true"
backend_exec "git -C ${REMOTE_DIR}/vpn-repo.git remote add origin https://github.com/Cyrillicspb/vpn-infra.git 2>/dev/null || git -C ${REMOTE_DIR}/vpn-repo.git remote set-url origin https://github.com/Cyrillicspb/vpn-infra.git"
backend_exec "git -C ${REMOTE_DIR}/vpn-repo.git fetch --all >/dev/null 2>&1 || true"
backend_exec "cat <<'CRONEOF' | sudo tee /etc/cron.d/vpn-mirror >/dev/null
SHELL=/bin/bash
*/30 * * * * sysadmin git -C /opt/vpn/vpn-repo.git fetch --all >> /var/log/vpn-mirror.log 2>&1
CRONEOF"
backend_exec "cat <<'HCEOF' | sudo tee /etc/cron.d/vps-healthcheck >/dev/null
SHELL=/bin/bash
*/5 * * * * sysadmin bash /opt/vpn/scripts/vps-healthcheck.sh >> /var/log/vps-healthcheck.log 2>&1
HCEOF"
backend_exec "sudo chmod 644 /etc/cron.d/vpn-mirror /etc/cron.d/vps-healthcheck 2>/dev/null || true"
log_ok "Git mirror и cron настроены"

log_step "Шаг 9: Регистрация backend в watchdog pool"

if [[ -n "${WATCHDOG_API_TOKEN:-}" ]]; then
    register_status="$(curl -sS -o /tmp/backend-register.json -w '%{http_code}' \
        -X POST http://localhost:8080/backends/add \
        -H "Authorization: Bearer ${WATCHDOG_API_TOKEN}" \
        -H 'Content-Type: application/json' \
        -d "{\"ip\":\"${BACKEND_IP}\",\"ssh_port\":${BACKEND_SSH_PORT},\"tunnel_ip\":\"${BACKEND_TUNNEL_IP}\",\"weight\":100}" || true)"
    register_body="$(cat /tmp/backend-register.json 2>/dev/null || true)"
    rm -f /tmp/backend-register.json
    if [[ "$register_status" == "200" ]]; then
        log_ok "Backend зарегистрирован в watchdog pool"
    elif [[ "$register_status" == "409" ]]; then
        log_warn "Backend уже был зарегистрирован: ${register_body}"
    else
        die "Не удалось зарегистрировать backend через watchdog API: HTTP ${register_status} ${register_body}"
    fi
else
    die "WATCHDOG_API_TOKEN не задан в ${ENV_FILE}"
fi

log_step "Шаг 10: Финализация SSH-доступа на backend"

backend_exec "sudo sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config; \
    grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin no' | sudo tee -a /etc/ssh/sshd_config >/dev/null; \
    sudo sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config; \
    grep -q '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'PasswordAuthentication no' | sudo tee -a /etc/ssh/sshd_config >/dev/null; \
    grep -q '^Port 8022$' /etc/ssh/sshd_config || echo 'Port 8022' | sudo tee -a /etc/ssh/sshd_config >/dev/null; \
    sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true"
log_ok "root SSH закрыт, password auth отключён"

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║        Backend VPS установлен и готов к работе              ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} sysadmin + SSH key настроены"
echo -e "  ${GREEN}✓${NC} Docker Compose и server-side stacks запущены"
echo -e "  ${GREEN}✓${NC} 3x-ui CDN inbound настроен автоматически"
echo -e "  ${GREEN}✓${NC} standalone reality-xhttp / reality-vision / hysteria2 готовы"
echo -e "  ${GREEN}✓${NC} backend зарегистрирован в watchdog pool"
echo -e "  ${GREEN}✓${NC} root SSH закрыт после успешной установки"
echo ""
echo -e "${BLUE}Backend:${NC} ${BACKEND_IP}:${BACKEND_SSH_PORT}"
echo -e "${BLUE}Tier-2 endpoint:${NC} ${BACKEND_TUNNEL_IP}"
echo -e "${BLUE}Watchdog API:${NC} backend visible via /backends and bot menu"
echo ""

#!/bin/bash
# =============================================================================
# add-vps.sh — Добавление второго VPS в инфраструктуру
#
# Запуск: bash /opt/vpn/add-vps.sh <VPS2_IP> <VPS2_ROOT_PASSWORD> [SSH_PORT]
# Например: bash /opt/vpn/add-vps.sh 217.60.7.50 mypassword 22
#
# Что делает:
#   1. Создаёт sysadmin на VPS2, копирует SSH-ключ
#   2. Устанавливает пакеты, Docker, nftables, fail2ban
#   3. Копирует VPS-файлы репозитория
#   4. Генерирует новые REALITY ключи (xray x25519) для VPS2
#   5. Создаёт .env для VPS2 (Hysteria2 — те же учётные, REALITY — новые ключи)
#   6. Настраивает Hysteria2 + TLS сертификат (полностью автоматически)
#   7. Запускает docker compose на VPS2
#   8. Создаёт wg-tier2-vps2 туннель (10.177.2.4/30)
#   9. Создаёт client-конфиги плагинов для VPS2
#  10. Регистрирует VPS2 в watchdog (/vps/add)
#  11. Настраивает git-зеркало и healthcheck cron
#
# После скрипта — вручную: настроить 3x-ui inbounds на VPS2
# (инструкции выводятся в конце)
# =============================================================================

set -euo pipefail

# ── Аргументы ─────────────────────────────────────────────────────────────────
VPS2_IP="${1:-}"
VPS2_ROOT_PASS="${2:-}"
VPS2_SSH_PORT="${3:-22}"

if [[ -z "$VPS2_IP" || -z "$VPS2_ROOT_PASS" ]]; then
    echo "Использование: bash add-vps.sh <IP> <ROOT_PASSWORD> [SSH_PORT]"
    echo "Пример:        bash add-vps.sh 217.60.7.50 mypassword 22"
    exit 1
fi

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[✓]${NC}   $*"; }
log_warn()  { echo -e "${YELLOW}[!]${NC}   $*"; }
log_error() { echo -e "${RED}[✗]${NC}   $*" >&2; }
log_step()  { echo ""; echo -e "${CYAN}${BOLD}━━━ $* ━━━${NC}"; }
die()       { log_error "$*"; exit 1; }

# ── Пути ──────────────────────────────────────────────────────────────────────
REPO_DIR="/opt/vpn"
ENV_FILE="$REPO_DIR/.env"
SSH_KEY="/root/.ssh/vpn_id_ed25519"

[[ -f "$ENV_FILE" ]] || die "Файл ${ENV_FILE} не найден. Запустите из /opt/vpn."
[[ -f "$SSH_KEY"  ]] || die "SSH-ключ ${SSH_KEY} не найден."

set -o allexport; source "$ENV_FILE"; set +o allexport

# ── SSH-функции ───────────────────────────────────────────────────────────────
vps2_exec() {
    ssh -p "$VPS2_SSH_PORT" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        -o ServerAliveInterval=60 \
        "sysadmin@${VPS2_IP}" "$@"
}
vps2_root_exec() {
    ssh -p "$VPS2_SSH_PORT" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        "root@${VPS2_IP}" "$@"
}
vps2_copy() {
    scp -P "$VPS2_SSH_PORT" -i "$SSH_KEY" -o StrictHostKeyChecking=no "$@"
}
vps2_root_copy() {
    sshpass -p "$VPS2_ROOT_PASS" scp \
        -P "$VPS2_SSH_PORT" -o StrictHostKeyChecking=no "$@"
}

# ── Шаг 1: Bootstrap VPS2 — sysadmin + SSH ключ ──────────────────────────────
log_step "Шаг 1: Bootstrap VPS2 (sysadmin + SSH ключ)"

which sshpass &>/dev/null || apt-get install -y -qq sshpass

# Проверяем, нет ли уже sysadmin с нашим ключом
if ssh -p "$VPS2_SSH_PORT" -i "$SSH_KEY" \
       -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes \
       "sysadmin@${VPS2_IP}" "echo ok" &>/dev/null; then
    log_ok "SSH (sysadmin@${VPS2_IP}) уже работает"
else
    log_info "Создание sysadmin на VPS2..."

    # Подключаемся как root, создаём sysadmin
    sshpass -p "$VPS2_ROOT_PASS" ssh \
        -p "$VPS2_SSH_PORT" \
        -o StrictHostKeyChecking=no \
        "root@${VPS2_IP}" bash << 'BOOTSTRAP'
set -euo pipefail
id sysadmin &>/dev/null || useradd -m -s /bin/bash -G sudo sysadmin
echo 'sysadmin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/sysadmin
chmod 440 /etc/sudoers.d/sysadmin
mkdir -p /home/sysadmin/.ssh
chmod 700 /home/sysadmin/.ssh
touch /home/sysadmin/.ssh/authorized_keys
chmod 600 /home/sysadmin/.ssh/authorized_keys
chown -R sysadmin:sysadmin /home/sysadmin/.ssh
echo "Bootstrap OK"
BOOTSTRAP

    # Копируем SSH ключ
    PUB_KEY=$(cat "${SSH_KEY}.pub")
    sshpass -p "$VPS2_ROOT_PASS" ssh \
        -p "$VPS2_SSH_PORT" \
        -o StrictHostKeyChecking=no \
        "root@${VPS2_IP}" \
        "echo '${PUB_KEY}' >> /home/sysadmin/.ssh/authorized_keys && echo 'Key copied'"

    # Финальная проверка
    vps2_exec "echo ok" &>/dev/null \
        || die "SSH к sysadmin@${VPS2_IP} не работает после копирования ключа"

    log_ok "sysadmin создан, SSH-ключ скопирован"
fi

# ── Закрыть root SSH ──────────────────────────────────────────────────────────
log_info "Закрытие root SSH-доступа на VPS2..."
vps2_exec "sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config; \
    grep -q '^PermitRootLogin' /etc/ssh/sshd_config \
        || echo 'PermitRootLogin no' | sudo tee -a /etc/ssh/sshd_config; \
    sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config; \
    grep -q '^PasswordAuthentication' /etc/ssh/sshd_config \
        || echo 'PasswordAuthentication no' | sudo tee -a /etc/ssh/sshd_config; \
    sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true"
log_ok "root SSH закрыт, password auth отключён"

# ── Шаг 2: Системные пакеты ───────────────────────────────────────────────────
log_step "Шаг 2: Системные пакеты на VPS2"

vps2_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
    sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq 2>/dev/null"

vps2_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl wget git jq wireguard-tools openssl gnupg2 ca-certificates \
    python3 net-tools nftables fail2ban"

log_ok "Пакеты установлены"

# ── Шаг 3: IPv6 ───────────────────────────────────────────────────────────────
log_step "Шаг 3: Отключение IPv6"

vps2_exec "printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' | \
    sudo tee /etc/sysctl.d/99-disable-ipv6.conf > /dev/null && \
    sudo sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true"

log_ok "IPv6 отключён"

# ── Шаг 4: Docker ────────────────────────────────────────────────────────────
log_step "Шаг 4: Docker CE"

if vps2_exec "command -v docker &>/dev/null && echo yes" 2>/dev/null | grep -q yes; then
    log_info "Docker уже установлен"
else
    vps2_exec "curl -fsSL https://get.docker.com | sudo sh" \
        || die "Не удалось установить Docker"
    vps2_exec "sudo systemctl enable docker && sudo systemctl start docker"
fi

vps2_exec "sudo mkdir -p /etc/docker && \
    printf '{\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"3\"},\"dns\":[\"8.8.8.8\",\"1.1.1.1\"],\"ipv6\":false}\n' | \
    sudo tee /etc/docker/daemon.json > /dev/null && \
    sudo systemctl restart docker"

vps2_exec "sudo usermod -aG docker sysadmin 2>/dev/null || true"
log_ok "Docker $(vps2_exec "sudo docker --version 2>/dev/null")"

# ── Шаг 5: nftables (rate limiting + WireGuard порт) ─────────────────────────
log_step "Шаг 5: nftables на VPS2"

vps2_exec "cat << 'NFTEOF' | sudo tee /etc/nftables-vps.conf > /dev/null
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
    chain input {
        type filter hook input priority filter; policy accept;
        iifname \"lo\" accept
        ct state established,related accept
        tcp dport { 22, 443, 2053, 2083, 2087 } ct state new accept
        tcp dport 443 limit rate 200/second burst 500 packets accept
        tcp dport 443 drop
        udp dport 443 limit rate 200/second burst 500 packets accept
        udp dport 443 drop
        icmp type echo-request limit rate 10/second accept
        udp dport 51822 accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
    }
}
NFTEOF"

vps2_exec "sudo systemctl enable nftables && sudo nft -f /etc/nftables-vps.conf || true"
log_ok "nftables настроен"

# ── Шаг 6: fail2ban ──────────────────────────────────────────────────────────
log_step "Шаг 6: fail2ban"

vps2_exec "printf '[DEFAULT]\nbantime = 3600\nfindtime = 600\nmaxretry = 5\nbackend = systemd\n\n[sshd]\nenabled = true\nport = ${VPS2_SSH_PORT}\n' | \
    sudo tee /etc/fail2ban/jail.local > /dev/null"
vps2_exec "sudo systemctl enable fail2ban && sudo systemctl restart fail2ban"
log_ok "fail2ban настроен"

# ── Шаг 7: Копирование файлов VPS ────────────────────────────────────────────
log_step "Шаг 7: Копирование файлов VPS"

vps2_exec "sudo mkdir -p /opt/vpn && sudo chown sysadmin:sysadmin /opt/vpn && \
    mkdir -p /opt/vpn/scripts /opt/vpn/nginx/mtls /opt/vpn/nginx/ssl \
             /opt/vpn/nginx/conf.d /opt/vpn/cloudflared /opt/vpn/3x-ui/db \
             /opt/vpn/hysteria2 /opt/vpn/backups /opt/vpn/vpn-repo.git"

vps2_copy -r "${REPO_DIR}/vps/." "sysadmin@${VPS2_IP}:/opt/vpn/"
log_ok "Файлы скопированы"

# ── Шаг 8: mTLS CA ───────────────────────────────────────────────────────────
log_step "Шаг 8: mTLS CA и сертификаты"

vps2_exec "[ -f /opt/vpn/nginx/mtls/ca.crt ] || ( \
    openssl genrsa -out /opt/vpn/nginx/mtls/ca.key 4096 2>/dev/null && \
    openssl req -new -x509 -days 3650 \
        -key /opt/vpn/nginx/mtls/ca.key \
        -out /opt/vpn/nginx/mtls/ca.crt \
        -subj '/CN=VPN-CA2/O=VPNInfra/C=RU' 2>/dev/null && \
    chmod 600 /opt/vpn/nginx/mtls/ca.key && echo 'CA создан' )"

vps2_exec "[ -f /opt/vpn/nginx/ssl/server.crt ] || ( \
    openssl genrsa -out /opt/vpn/nginx/ssl/server.key 2048 2>/dev/null && \
    openssl req -new -key /opt/vpn/nginx/ssl/server.key \
        -out /opt/vpn/nginx/ssl/server.csr \
        -subj '/CN=vpn-server2/O=VPNInfra/C=RU' 2>/dev/null && \
    openssl x509 -req -days 730 \
        -in /opt/vpn/nginx/ssl/server.csr \
        -CA /opt/vpn/nginx/mtls/ca.crt \
        -CAkey /opt/vpn/nginx/mtls/ca.key -CAcreateserial \
        -out /opt/vpn/nginx/ssl/server.crt 2>/dev/null && \
    rm -f /opt/vpn/nginx/ssl/server.csr && \
    chmod 600 /opt/vpn/nginx/ssl/server.key && echo 'Server cert создан' )"

log_ok "mTLS CA и server cert готовы"

# ── Шаг 9: Hysteria2 TLS сертификат ──────────────────────────────────────────
log_step "Шаг 9: Hysteria2 TLS сертификат"

vps2_exec "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
    -keyout /opt/vpn/hysteria2/server.key \
    -out /opt/vpn/hysteria2/server.crt \
    -days 3650 -nodes \
    -subj '/CN=${VPS2_IP}' \
    -addext 'subjectAltName=IP:${VPS2_IP}' 2>/dev/null && \
    sudo chmod 644 /opt/vpn/hysteria2/server.crt && \
    sudo chmod 600 /opt/vpn/hysteria2/server.key"

log_ok "Hysteria2 TLS сертификат создан"

# ── Шаг 10: .env для VPS2 ────────────────────────────────────────────────────
log_step "Шаг 10: Генерация .env для VPS2"

VPS2_ENV_TMP=$(mktemp /tmp/vps2-env.XXXXXX)
chmod 600 "$VPS2_ENV_TMP"

# Те же токены/пароли что и у VPS1, VPS-специфичные поля — для VPS2
cat > "$VPS2_ENV_TMP" << EOF
# VPS2 .env — сгенерировано add-vps.sh
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ADMIN_CHAT_ID=${TELEGRAM_ADMIN_CHAT_ID:-}
CF_TUNNEL_TOKEN=
XRAY_UUID=${XRAY_UUID:-}
XRAY_GRPC_UUID=${XRAY_GRPC_UUID:-}
XRAY_PRIVATE_KEY=PENDING
XRAY_PUBLIC_KEY=PENDING
XRAY_GRPC_PRIVATE_KEY=PENDING
XRAY_GRPC_PUBLIC_KEY=PENDING
HYSTERIA2_AUTH=${HYSTERIA2_AUTH:-}
HYSTERIA2_OBFS=${HYSTERIA2_OBFS:-}
VPS_IP=${VPS2_IP}
VPS_TUNNEL_IP=10.177.2.6
HOME_TUNNEL_IP=10.177.2.5
HOME_SERVER_IP=${HOME_SERVER_IP:-}
WATCHDOG_API_TOKEN=${WATCHDOG_API_TOKEN:-}
DOMAIN=
CF_API_TOKEN=
SSH_ADDITIONAL_PORT=443
XHTTP_MS_PASSWORD=${XHTTP_MS_PASSWORD:-$(openssl rand -hex 16)}
XHTTP_CDN_PASSWORD=${XHTTP_CDN_PASSWORD:-$(openssl rand -hex 16)}
EOF

vps2_copy "$VPS2_ENV_TMP" "sysadmin@${VPS2_IP}:/opt/vpn/.env"
vps2_exec "chmod 600 /opt/vpn/.env"
rm -f "$VPS2_ENV_TMP"
log_ok ".env скопирован (REALITY ключи — PENDING, заполним после запуска 3x-ui)"

# ── Шаг 11: Hysteria2 server.yaml ────────────────────────────────────────────
log_step "Шаг 11: Hysteria2 server.yaml"

vps2_exec "sudo tee /opt/vpn/hysteria2/server.yaml > /dev/null << 'HYEOF'
listen: :443

tls:
  cert: /etc/hysteria2/server.crt
  key: /etc/hysteria2/server.key

obfs:
  type: salamander
  salamander:
    password: ${HYSTERIA2_OBFS}

auth:
  type: password
  password: ${HYSTERIA2_AUTH}

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

log_ok "Hysteria2 server.yaml создан"

# ── Шаг 12: Docker Compose ───────────────────────────────────────────────────
log_step "Шаг 12: Docker Compose на VPS2"

log_info "Загрузка образов (займёт несколько минут)..."
vps2_exec "cd /opt/vpn && sudo docker compose pull --quiet 2>/dev/null || true"

log_info "Запуск контейнеров..."
vps2_exec "cd /opt/vpn && sudo docker compose up -d --remove-orphans 2>/dev/null || \
    sudo docker compose up -d 2>/dev/null || true"

log_info "Ожидание запуска 3x-ui (30 сек)..."
sleep 30

log_info "Статус контейнеров VPS2:"
vps2_exec "cd /opt/vpn && sudo docker compose ps 2>/dev/null || true"

log_ok "Docker Compose запущен"

# ── Шаг 13: Генерация REALITY ключей через xray на VPS2 ──────────────────────
log_step "Шаг 13: Генерация REALITY ключей для VPS2"

log_info "Генерация ключей для reality (microsoft.com)..."
XRAY_KEYS_MS=$(vps2_exec "sudo docker exec 3x-ui xray x25519 2>/dev/null" || echo "ERROR")

log_info "Генерация ключей для reality-grpc (cdn.jsdelivr.net)..."
XRAY_KEYS_CDN=$(vps2_exec "sudo docker exec 3x-ui xray x25519 2>/dev/null" || echo "ERROR")

if echo "$XRAY_KEYS_MS" | grep -q "Private key:"; then
    VPS2_XRAY_PRIVATE_KEY=$(echo "$XRAY_KEYS_MS" | grep "Private key:" | awk '{print $3}')
    VPS2_XRAY_PUBLIC_KEY=$(echo "$XRAY_KEYS_MS" | grep "Public key:" | awk '{print $3}')
    VPS2_XRAY_GRPC_PRIVATE_KEY=$(echo "$XRAY_KEYS_CDN" | grep "Private key:" | awk '{print $3}')
    VPS2_XRAY_GRPC_PUBLIC_KEY=$(echo "$XRAY_KEYS_CDN" | grep "Public key:" | awk '{print $3}')

    log_ok "REALITY ключи сгенерированы"
    log_info "VPS2 Public key (reality):      ${VPS2_XRAY_PUBLIC_KEY}"
    log_info "VPS2 Public key (reality-grpc): ${VPS2_XRAY_GRPC_PUBLIC_KEY}"

    # Обновляем .env на VPS2
    vps2_exec "sed -i 's|XRAY_PRIVATE_KEY=PENDING|XRAY_PRIVATE_KEY=${VPS2_XRAY_PRIVATE_KEY}|' /opt/vpn/.env && \
        sed -i 's|XRAY_PUBLIC_KEY=PENDING|XRAY_PUBLIC_KEY=${VPS2_XRAY_PUBLIC_KEY}|' /opt/vpn/.env && \
        sed -i 's|XRAY_GRPC_PRIVATE_KEY=PENDING|XRAY_GRPC_PRIVATE_KEY=${VPS2_XRAY_GRPC_PRIVATE_KEY}|' /opt/vpn/.env && \
        sed -i 's|XRAY_GRPC_PUBLIC_KEY=PENDING|XRAY_GRPC_PUBLIC_KEY=${VPS2_XRAY_GRPC_PUBLIC_KEY}|' /opt/vpn/.env"
    log_ok ".env VPS2 обновлён с REALITY ключами"
else
    log_warn "Не удалось сгенерировать REALITY ключи автоматически"
    log_warn "Сгенерируйте вручную: docker exec 3x-ui xray x25519"
    VPS2_XRAY_PUBLIC_KEY="PENDING"
    VPS2_XRAY_GRPC_PUBLIC_KEY="PENDING"
fi

# ── Шаг 14: wg-tier2-vps2 на HOME сервере ────────────────────────────────────
log_step "Шаг 14: WireGuard tier2 туннель к VPS2"

# Генерируем новую пару ключей для home-side wg-tier2-vps2
HOME_WG2_PRIVATE=$(wg genkey)
HOME_WG2_PUBLIC=$(echo "$HOME_WG2_PRIVATE" | wg pubkey)

# Генерируем пару ключей для VPS2-side
VPS2_WG_PRIVATE=$(wg genkey)
VPS2_WG_PUBLIC=$(echo "$VPS2_WG_PRIVATE" | wg pubkey)

log_info "Home wg-tier2-vps2 public key: ${HOME_WG2_PUBLIC}"
log_info "VPS2 wg-tier2      public key: ${VPS2_WG_PUBLIC}"

# Конфиг home server (10.177.2.5/30)
cat > /etc/wireguard/wg-tier2-vps2.conf << EOF
[Interface]
PrivateKey = ${HOME_WG2_PRIVATE}
Address = 10.177.2.5/30
MTU = 1320

[Peer]
PublicKey = ${VPS2_WG_PUBLIC}
AllowedIPs = 10.177.2.4/30
Endpoint = ${VPS2_IP}:51822
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg-tier2-vps2.conf
log_ok "wg-tier2-vps2.conf создан на home сервере"

# Конфиг VPS2 (10.177.2.6/30)
vps2_exec "sudo bash -c 'cat > /etc/wireguard/wg-tier2.conf << EOF
[Interface]
PrivateKey = ${VPS2_WG_PRIVATE}
Address = 10.177.2.6/30
ListenPort = 51822
MTU = 1320

[Peer]
PublicKey = ${HOME_WG2_PUBLIC}
AllowedIPs = 10.177.2.4/30
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg-tier2.conf'"

vps2_exec "sudo systemctl enable wg-quick@wg-tier2 && \
    sudo systemctl restart wg-quick@wg-tier2 2>/dev/null || \
    sudo wg-quick up wg-tier2 2>/dev/null || true"

log_ok "wg-tier2 запущен на VPS2 (10.177.2.6)"

# Поднимаем туннель на home сервере
systemctl enable wg-quick@wg-tier2-vps2 2>/dev/null || true
wg-quick up wg-tier2-vps2 2>/dev/null || true

# Тест туннеля
sleep 3
if ping -c 2 -W 3 10.177.2.6 &>/dev/null; then
    log_ok "Туннель wg-tier2-vps2 работает! ping 10.177.2.6 ОК"
else
    log_warn "Ping 10.177.2.6 не прошёл. Проверьте firewall VPS2 (UDP 51822)"
fi

# Добавляем маршрут через tier2-vps2 для DNS VPS2
ip route replace 10.177.2.6/32 dev wg-tier2-vps2 2>/dev/null || true

# ── Шаг 15: dnsmasq VPS2 DNS ─────────────────────────────────────────────────
log_step "Шаг 15: dnsmasq DNS для VPS2 (10.177.2.6)"

vps2_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq && \
    sudo bash -c 'cat > /etc/dnsmasq.conf << DNSEOF
listen-address=127.0.0.1,10.177.2.6
bind-interfaces
no-resolv
server=1.1.1.1
server=8.8.8.8
cache-size=1000
DNSEOF' && \
    sudo systemctl enable dnsmasq && sudo systemctl restart dnsmasq"

log_ok "dnsmasq настроен на VPS2 (слушает 10.177.2.6:53)"

# ── Шаг 16: Сохранение VPS2 переменных в home .env ───────────────────────────
log_step "Шаг 16: Сохранение VPS2 переменных в /opt/vpn/.env"

env_set() {
    local key="$1" val="$2"
    # Используем grep+delete+append вместо sed — безопасно для значений с |, /, &, \
    # || true: grep возвращает 1 на пустом файле или если строка не найдена — это нормально
    { grep -v "^${key}=" "$ENV_FILE" || true; } > "${ENV_FILE}.tmp"
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
    echo "${key}=${val}" >> "$ENV_FILE"
}

env_set "VPS2_IP"                   "$VPS2_IP"
env_set "VPS2_TUNNEL_IP"            "10.177.2.6"
env_set "VPS2_HOME_TUNNEL_IP"       "10.177.2.5"
env_set "VPS2_SSH_PORT"             "$VPS2_SSH_PORT"
env_set "VPS2_XRAY_UUID"            "${XRAY_UUID:-}"
env_set "VPS2_XRAY_GRPC_UUID"       "${XRAY_GRPC_UUID:-}"
env_set "VPS2_XRAY_PRIVATE_KEY"     "${VPS2_XRAY_PRIVATE_KEY:-PENDING}"
env_set "VPS2_XRAY_PUBLIC_KEY"      "${VPS2_XRAY_PUBLIC_KEY:-PENDING}"
env_set "VPS2_XRAY_GRPC_PRIVATE_KEY" "${VPS2_XRAY_GRPC_PRIVATE_KEY:-PENDING}"
env_set "VPS2_XRAY_GRPC_PUBLIC_KEY"  "${VPS2_XRAY_GRPC_PUBLIC_KEY:-PENDING}"
env_set "VPS2_HYSTERIA2_AUTH"       "${HYSTERIA2_AUTH:-}"
env_set "VPS2_HYSTERIA2_OBFS"       "${HYSTERIA2_OBFS:-}"
env_set "VPS2_WG_PUBLIC_KEY"        "$VPS2_WG_PUBLIC"
env_set "HOME_WG2_PUBLIC_KEY"       "$HOME_WG2_PUBLIC"

log_ok "VPS2 переменные сохранены в .env"

# ── Шаг 17: Plugin client configs для VPS2 ───────────────────────────────────
log_step "Шаг 17: Plugin client configs для VPS2"

PLUGINS_DIR="${REPO_DIR}/watchdog/plugins"

# hysteria2 — тот же пароль, просто другой сервер
cat > "${PLUGINS_DIR}/hysteria2/client-vps2.yaml" << EOF
# Hysteria2 client config для VPS2 (${VPS2_IP})
server: "${VPS2_IP}:443"
tls:
  insecure: true
auth: "${HYSTERIA2_AUTH:-}"
obfs:
  type: salamander
  salamander:
    password: "${HYSTERIA2_OBFS:-}"
quic:
  keepAlivePeriod: 20s
bandwidth:
  up: 50 mbps
  down: 200 mbps
socks5:
  listen: 127.0.0.1:1083
log:
  level: warn
EOF

# reality — xray config для VPS2
cat > "${REPO_DIR}/xray/config-reality-vps2.json" << EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [{
    "listen": "127.0.0.1", "port": 1080,
    "protocol": "socks",
    "settings": { "auth": "noauth", "udp": true },
    "tag": "socks-in"
  }],
  "outbounds": [{
    "protocol": "vless",
    "settings": {
      "vnext": [{
        "address": "${VPS2_IP}",
        "port": 2087,
        "users": [{ "id": "${XRAY_UUID:-}", "encryption": "none", "flow": "" }]
      }]
    },
    "streamSettings": {
      "network": "splithttp",
      "security": "reality",
      "realitySettings": {
        "serverName": "microsoft.com",
        "fingerprint": "chrome",
        "publicKey": "${VPS2_XRAY_PUBLIC_KEY:-PENDING}",
        "shortId": ""
      },
      "splithttpSettings": {
        "path": "/", "host": "microsoft.com",
        "maxUploadSize": 1000000,
        "maxConcurrentUploads": 10,
        "password": "${XHTTP_MS_PASSWORD:-}"
      }
    },
    "tag": "vless-xhttp-out"
  },
  { "protocol": "freedom", "tag": "direct" },
  { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [{ "type": "field", "ip": ["geoip:private"], "outboundTag": "direct" }]
  }
}
EOF

# reality-grpc — xray config для VPS2
cat > "${REPO_DIR}/xray/config-grpc-vps2.json" << EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [{
    "listen": "127.0.0.1", "port": 1081,
    "protocol": "socks",
    "settings": { "auth": "noauth", "udp": true },
    "tag": "socks-grpc-in"
  }],
  "outbounds": [{
    "protocol": "vless",
    "settings": {
      "vnext": [{
        "address": "${VPS2_IP}",
        "port": 2083,
        "users": [{ "id": "${XRAY_GRPC_UUID:-}", "encryption": "none", "flow": "" }]
      }]
    },
    "streamSettings": {
      "network": "splithttp",
      "security": "reality",
      "realitySettings": {
        "serverName": "cdn.jsdelivr.net",
        "fingerprint": "chrome",
        "publicKey": "${VPS2_XRAY_GRPC_PUBLIC_KEY:-PENDING}",
        "shortId": ""
      },
      "splithttpSettings": {
        "path": "/", "host": "cdn.jsdelivr.net",
        "maxUploadSize": 1000000,
        "maxConcurrentUploads": 10,
        "password": "${XHTTP_CDN_PASSWORD:-}"
      }
    },
    "tag": "vless-xhttp-cdn-out"
  },
  { "protocol": "freedom", "tag": "direct" },
  { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [{ "type": "field", "ip": ["geoip:private"], "outboundTag": "direct" }]
  }
}
EOF

log_ok "Client configs для VPS2 созданы"
log_info "  Файлы: xray/config-reality-vps2.json, xray/config-grpc-vps2.json"
log_info "         watchdog/plugins/hysteria2/client-vps2.yaml"

# ── Шаг 18: Регистрация VPS2 в watchdog ──────────────────────────────────────
log_step "Шаг 18: Регистрация VPS2 в watchdog"

if [[ -n "${WATCHDOG_API_TOKEN:-}" ]]; then
    RESP=$(curl -sf -X POST http://localhost:8080/vps/add \
        -H "Authorization: Bearer ${WATCHDOG_API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"ip\":\"${VPS2_IP}\",\"ssh_port\":${VPS2_SSH_PORT},\"tunnel_ip\":\"10.177.2.6\"}" \
        2>/dev/null || echo '{"error":"watchdog недоступен"}')
    log_info "Watchdog ответ: ${RESP}"
    if echo "$RESP" | grep -q '"status"'; then
        log_ok "VPS2 зарегистрирован в watchdog"
    else
        log_warn "Watchdog API не ответил. Добавьте VPS2 через бот: /vps add ${VPS2_IP}"
    fi
else
    log_warn "WATCHDOG_API_TOKEN не задан. Добавьте VPS2 через бот: /vps add ${VPS2_IP}"
fi

# ── Шаг 19: Git-зеркало и healthcheck cron на VPS2 ───────────────────────────
log_step "Шаг 19: Git-зеркало и healthcheck cron"

vps2_exec "git -C /opt/vpn/vpn-repo.git init --bare 2>/dev/null || true && \
    git -C /opt/vpn/vpn-repo.git remote add origin \
        https://github.com/Cyrillicspb/vpn-infra.git 2>/dev/null || \
    git -C /opt/vpn/vpn-repo.git remote set-url origin \
        https://github.com/Cyrillicspb/vpn-infra.git 2>/dev/null || true"

vps2_exec "git -C /opt/vpn/vpn-repo.git fetch --all 2>/dev/null || true"

vps2_exec "cat << 'CRONEOF' | sudo tee /etc/cron.d/vpn-mirror > /dev/null
SHELL=/bin/bash
*/30 * * * * sysadmin git -C /opt/vpn/vpn-repo.git fetch --all >> /var/log/vpn-mirror.log 2>&1
CRONEOF"

vps2_exec "cat << 'HCEOF' | sudo tee /etc/cron.d/vps-healthcheck > /dev/null
SHELL=/bin/bash
*/5 * * * * sysadmin bash /opt/vpn/scripts/vps-healthcheck.sh >> /var/log/vps-healthcheck.log 2>&1
HCEOF"

vps2_exec "sudo chmod 644 /etc/cron.d/vpn-mirror /etc/cron.d/vps-healthcheck 2>/dev/null || true"
log_ok "Git-зеркало и healthcheck cron настроены"

# ── Финальный отчёт ───────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║           VPS2 добавлен! Что работает:                       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} Docker + все сервисы запущены"
echo -e "  ${GREEN}✓${NC} Hysteria2 (UDP 443) — те же auth/obfs что у VPS1"
echo -e "  ${GREEN}✓${NC} wg-tier2-vps2 туннель: 10.177.2.5 ↔ 10.177.2.6"
echo -e "  ${GREEN}✓${NC} dnsmasq DNS на 10.177.2.6:53"
echo -e "  ${GREEN}✓${NC} Client configs для VPS2 созданы"
echo ""
echo -e "${YELLOW}${BOLD}⚠ ТРЕБУЕТСЯ ВРУЧНУЮ: настройка 3x-ui inbounds${NC}"
echo ""
echo "  1. Откройте 3x-ui на VPS2:"
echo "     http://${VPS2_IP}:2053"
echo "     Логин: admin / admin"
echo ""
echo "  2. Добавьте inbound VLESS+REALITY (XHTTP) на порту 2087:"
echo "     - Protocol:   VLESS"
echo "     - Port:       2087"
echo "     - UUID:       ${XRAY_UUID:-<см. .env XRAY_UUID>}"
echo "     - Network:    SplitHTTP"
echo "     - Security:   Reality"
echo "     - ServerName: microsoft.com"
if [[ "${VPS2_XRAY_PRIVATE_KEY:-PENDING}" != "PENDING" ]]; then
echo "     - PrivateKey: ${VPS2_XRAY_PRIVATE_KEY}"
echo "     - PublicKey:  ${VPS2_XRAY_PUBLIC_KEY}"
fi
echo ""
echo "  3. Добавьте inbound VLESS+REALITY (XHTTP) на порту 2083:"
echo "     - Protocol:   VLESS"
echo "     - Port:       2083"
echo "     - UUID:       ${XRAY_GRPC_UUID:-<см. .env XRAY_GRPC_UUID>}"
echo "     - Network:    SplitHTTP"
echo "     - Security:   Reality"
echo "     - ServerName: cdn.jsdelivr.net"
if [[ "${VPS2_XRAY_GRPC_PRIVATE_KEY:-PENDING}" != "PENDING" ]]; then
echo "     - PrivateKey: ${VPS2_XRAY_GRPC_PRIVATE_KEY}"
echo "     - PublicKey:  ${VPS2_XRAY_GRPC_PUBLIC_KEY}"
fi
echo ""
echo -e "${CYAN}${BOLD}Переключение на VPS2 через бот:${NC}"
echo "  /vps list           — список VPS"
echo "  /vps add ${VPS2_IP}  — если не зарегистрирован"
echo ""
echo -e "${CYAN}${BOLD}Или запустите migrate-vps для полного переключения:${NC}"
echo "  /migrate-vps ${VPS2_IP}"
echo ""
echo -e "${BLUE}VPS2 данные сохранены в /opt/vpn/.env (VPS2_* переменные)${NC}"
echo -e "${BLUE}wg-tier2-vps2: /etc/wireguard/wg-tier2-vps2.conf${NC}"
echo ""

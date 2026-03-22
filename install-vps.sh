#!/bin/bash
# =============================================================================
# install-vps.sh — Установка компонентов на VPS
# Вызывается из setup.sh (STEP=31 bash install-vps.sh)
# Использует: sysadmin пользователь (не root)
# Шаги 32-44
# =============================================================================

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Ошибка: install-vps.sh должен запускаться от root (sudo bash install-vps.sh)" >&2
    exit 1
fi

# ── Swap: создать 1GB если RAM < 2048 MB и swap ещё нет ──────────────────────
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
if (( TOTAL_RAM_MB < 2048 )); then
    if ! swapon --show | grep -q '/swapfile'; then
        echo "[INFO] RAM ${TOTAL_RAM_MB}MB < 2048MB — создаём swap 1GB..."
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
        echo "[✓]   Swap 1GB активирован"
    fi
fi

# ── Константы и общие функции ─────────────────────────────────────────────────

STEP="${STEP:-31}"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$_SCRIPT_DIR/common.sh"
unset _SCRIPT_DIR

# ── Загрузка переменных и SSH-функции ────────────────────────────────────────

[[ -f "$ENV_FILE" ]] || die "Файл ${ENV_FILE} не найден. Сначала запустите setup.sh"
set -o allexport; source "$ENV_FILE"; set +o allexport

SSH_KEY="/root/.ssh/vpn_id_ed25519"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -f "$SSH_KEY" ]] || die "SSH-ключ ${SSH_KEY} не найден. Шаг 6 (setup.sh) не выполнен?"
[[ -n "${VPS_IP:-}" ]]  || die "VPS_IP не задан в ${ENV_FILE}"

vps_exec() {
    ssh -p "${VPS_SSH_PORT:-22}" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        "sysadmin@${VPS_IP}" "$@"
}

# vps_exec_long — для долгих команд на VPS (apt-get, docker pull и т.п.)
# Команда запускается через nohup и выживает при обрыве SSH.
# При реконнекте setup.sh просто повторяет шаг (идемпотентность).
vps_exec_long() {
    local log="/tmp/vps-cmd-$RANDOM.log"
    local done_file="${log}.done"
    local cmd="$*"
    local _ssh="ssh -p ${VPS_SSH_PORT:-22} -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 sysadmin@${VPS_IP}"

    # Запустить в фоне — выживет при обрыве SSH
    $_ssh "nohup bash -c '$cmd > $log 2>&1; echo \$? > $done_file' >/dev/null 2>&1 &"

    # Стримить вывод и ждать завершения
    $_ssh "tail -n +1 -f $log 2>/dev/null &
           TAIL=\$!
           until [[ -f $done_file ]]; do sleep 2; done
           sleep 1; kill \$TAIL 2>/dev/null
           exit \$(cat $done_file)"
}

vps_copy() {
    scp -P "${VPS_SSH_PORT:-22}" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no "$@"
}

vps_root_exec() {
    ssh -p "${VPS_SSH_PORT:-22}" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        "root@${VPS_IP}" "$@"
}

# ── Шаг 32: Проверка SSH-доступа к VPS ───────────────────────────────────────

if is_done "step32_vps_ssh_check"; then
    step_skip "step32_vps_ssh_check"
else
    step "Проверка SSH-доступа к VPS (sysadmin)"

    if ! vps_exec "echo ok" &>/dev/null 2>&1; then
        log_warn "SSH с ключом не работает. Попытка через sshpass..."

        [[ -z "${VPS_ROOT_PASSWORD:-}" ]] && \
            die "VPS_ROOT_PASSWORD не задан. Установите пароль в ${ENV_FILE} и повторите."

        sshpass -p "${VPS_ROOT_PASSWORD}" ssh-copy-id \
            -i "${SSH_KEY}.pub" \
            -p "${VPS_SSH_PORT:-22}" \
            -o StrictHostKeyChecking=no \
            "sysadmin@${VPS_IP}" 2>/dev/null \
            || die "SSH к VPS недоступен. Проверьте IP (${VPS_IP}), порт (${VPS_SSH_PORT:-22}) и учётные данные."
    fi

    # Финальная проверка
    vps_exec "echo ok" &>/dev/null 2>&1 \
        || die "SSH к VPS (sysadmin@${VPS_IP}) не работает даже после копирования ключа."

    log_ok "SSH-доступ к VPS подтверждён"
    step_done "step32_vps_ssh_check"
fi

# ── Шаг 33: Обновление пакетов на VPS ────────────────────────────────────────

if is_done "step33_vps_update_packages"; then
    step_skip "step33_vps_update_packages"
else
    step "Обновление системных пакетов на VPS"

    vps_exec_long "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
        sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq"

    vps_exec_long "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        curl wget git jq wireguard-tools openssl gnupg2 ca-certificates \
        python3 python3-pip net-tools mosh"

    log_ok "Пакеты на VPS обновлены"
    step_done "step33_vps_update_packages"
fi

# ── Шаг 34: Отключение IPv6 на VPS ───────────────────────────────────────────

if is_done "step34_vps_disable_ipv6"; then
    step_skip "step34_vps_disable_ipv6"
else
    step "Отключение IPv6 на VPS"

    vps_exec "printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\nnet.ipv6.conf.lo.disable_ipv6 = 1\n' | \
        sudo tee /etc/sysctl.d/99-disable-ipv6.conf > /dev/null && \
        sudo sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true"

    log_ok "IPv6 отключён на VPS"
    step_done "step34_vps_disable_ipv6"
fi

# ── Шаг 35: Установка Docker CE на VPS ───────────────────────────────────────

if is_done "step35_vps_install_docker"; then
    step_skip "step35_vps_install_docker"
else
    step "Установка Docker CE на VPS"

    # Проверяем, установлен ли уже Docker
    # Используем код возврата, а не grep по stdout — устойчиво к SSH сбоям
    if vps_exec "command -v docker &>/dev/null" 2>/dev/null; then
        log_info "Docker уже установлен на VPS"
    else
        log_info "Установка Docker на VPS через get.docker.com..."
        vps_exec_long "curl -fsSL https://get.docker.com | sudo sh" \
            || die "Не удалось установить Docker на VPS"
        vps_exec "sudo systemctl enable docker && sudo systemctl start docker"
    fi

    # На Ubuntu 24.04 iptables = iptables-nft (использует nf_tables backend).
    # Docker создаёт цепочки DOCKER-FORWARD через iptables-nft.
    # nft flush ruleset уничтожает их (один backend!).
    # Решение: переключить на iptables-legacy — тогда Docker и nftables не пересекаются.
    vps_exec "sudo update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null || true && \
        sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null || true"

    # Настройка daemon.json
    vps_exec "sudo mkdir -p /etc/docker && \
        printf '{\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"3\"},\"dns\":[\"8.8.8.8\",\"1.1.1.1\"],\"ipv6\":false}\n' | \
        sudo tee /etc/docker/daemon.json > /dev/null && \
        sudo systemctl restart docker"

    # Добавление sysadmin в группу docker
    vps_exec "sudo usermod -aG docker sysadmin 2>/dev/null || true"

    VPS_DOCKER_VER=$(vps_exec "sudo docker --version 2>/dev/null")
    log_ok "Docker на VPS: ${VPS_DOCKER_VER}"
    step_done "step35_vps_install_docker"
fi

# ── Шаг 36: Настройка nftables на VPS (rate limiting TCP/UDP 443) ─────────────

if is_done "step36_vps_nftables"; then
    step_skip "step36_vps_nftables"
else
    step "Настройка nftables на VPS (rate limiting + защита портов)"
    log_info "Rate limiting: TCP/UDP 443 — 200/сек, burst 500 (защита Xray + Hysteria2)"
    log_info "Открытые порты: 22 (SSH), 443 (Xray/Hysteria2), 8022 (SSH аварийный)"

    vps_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables"

    vps_exec "cat << 'NFTEOF' | sudo tee /etc/nftables-vps.conf > /dev/null
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
    chain input {
        type filter hook input priority filter; policy accept;

        # Loopback
        iifname \"lo\" accept

        # Established/related
        ct state established,related accept

        # SSH основной порт + аварийный 8022 (DPI блокирует 22 и 443 при падении стеков)
        tcp dport { 22, 8022 } ct state new accept

        # CDN-стек: Cloudflare Worker → VPS:8080 (VLESS+splithttp, защищён UUID)
        tcp dport 8080 ct state new accept

        # Rate limiting TCP 443 (защита Xray/Nginx от flood)
        tcp dport 443 limit rate 200/second burst 500 packets accept
        tcp dport 443 drop

        # Rate limiting UDP 443 (защита Hysteria2 от flood)
        udp dport 443 limit rate 200/second burst 500 packets accept
        udp dport 443 drop

        # ICMP
        icmp type echo-request limit rate 10/second accept

        # Mosh — UDP порты для терминала (только от tier-2 туннеля)
        iifname "tun0" udp dport 60000-61000 accept

        # DNS от Tier-2 туннеля (dnsmasq на VPS слушает на tun0 10.177.2.2)
        iifname "tun0" udp dport 53 accept
        iifname "tun0" tcp dport 53 accept
    }

    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        # Docker bridge-сети (br-*, vps-net и др.)
        iifname "docker*" accept
        oifname "docker*" accept
        iifname "br-*"    accept
        oifname "br-*"    accept
    }
}
NFTEOF"

    vps_exec "sudo systemctl enable nftables && \
        sudo nft -f /etc/nftables-vps.conf || true"

    log_ok "nftables настроен на VPS"
    step_done "step36_vps_nftables"
fi

# ── Шаг 36b: DNS-форвардер на VPS (dnsmasq на tun0) ─────────────────────────
# Домашний dnsmasq форвардит заблокированные домены на 10.177.2.2:53.
# VPS должен слушать на tun0 и форвардить в 1.1.1.1/8.8.8.8.

if is_done "step36b_vps_dns"; then
    step_skip "step36b_vps_dns"
else
    step "Установка DNS-форвардера на VPS (dnsmasq на tun0 10.177.2.2)"
    log_info "Домашний сервер форвардит DNS заблокированных доменов на 10.177.2.2:53."
    log_info "Устанавливаем dnsmasq на VPS — слушает на tun0, форвардит в 1.1.1.1/8.8.8.8."

    vps_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq"

    vps_exec "cat << 'DNSEOF' | sudo tee /etc/dnsmasq.d/vpn-tier2.conf > /dev/null
# DNS-форвардер для Tier-2 туннеля
# Слушает на tun0 (10.177.2.2), форвардит в 1.1.1.1/8.8.8.8
listen-address=127.0.0.1,10.177.2.2
bind-dynamic
no-resolv
server=1.1.1.1
server=8.8.8.8
cache-size=1000
DNSEOF"

    vps_exec "sudo systemctl enable dnsmasq && sudo systemctl restart dnsmasq"
    log_ok "dnsmasq запущен на VPS (слушает 127.0.0.1 + 10.177.2.2 при поднятии tun0)"
    step_done "step36b_vps_dns"
fi

# ── Шаг 37: Настройка fail2ban на VPS ────────────────────────────────────────

if is_done "step37_vps_fail2ban"; then
    step_skip "step37_vps_fail2ban"
else
    step "Настройка fail2ban на VPS"

    vps_exec "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban"
    vps_exec "printf '[DEFAULT]\nbantime = 86400\nfindtime = 300\nmaxretry = 3\nbackend = systemd\n\n[sshd]\nenabled = true\nport = 22,8022\nfilter = sshd\nmode = aggressive\nmaxretry = 3\n' | \
        sudo tee /etc/fail2ban/jail.local > /dev/null"
    vps_exec "sudo systemctl enable fail2ban && sudo systemctl restart fail2ban"

    log_ok "fail2ban настроен на VPS"
    step_done "step37_vps_fail2ban"
fi

# ── Шаг 38: Копирование файлов VPS ───────────────────────────────────────────

if is_done "step38_vps_copy_files"; then
    step_skip "step38_vps_copy_files"
else
    step "Копирование файлов на VPS"

    # Создаём директорию /opt/vpn на VPS
    vps_exec "sudo mkdir -p /opt/vpn && sudo chown sysadmin:sysadmin /opt/vpn && \
        mkdir -p /opt/vpn/scripts /opt/vpn/nginx/mtls /opt/vpn/nginx/ssl \
                 /opt/vpn/nginx/conf.d /opt/vpn/cloudflared /opt/vpn/3x-ui/db \
                 /opt/vpn/hysteria2 /opt/vpn/backups /opt/vpn/vpn-repo.git"

    # Копируем директорию vps/ из репозитория
    VPS_DIR="${REPO_DIR}/vps"
    if [[ -d "$VPS_DIR" ]]; then
        vps_copy -r "${VPS_DIR}/." "sysadmin@${VPS_IP}:/opt/vpn/"
        log_ok "Файлы VPS скопированы из ${VPS_DIR}"
    else
        log_warn "Директория vps/ не найдена в ${REPO_DIR}. Пропускаем копирование файлов VPS."
    fi

    step_done "step38_vps_copy_files"
fi

# ── Шаг 39: Генерация .env для VPS ───────────────────────────────────────────

if is_done "step39_vps_env"; then
    step_skip "step39_vps_env"
else
    step "Генерация .env файла для VPS"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    # Создаём временный файл .env для VPS
    VPS_ENV_TMP=$(mktemp /tmp/vps-env.XXXXXX)
    chmod 600 "$VPS_ENV_TMP"

    # Генерируем пароли для 3x-ui панели и Grafana если ещё нет
    [[ -z "${XRAY_PANEL_PASSWORD:-}" ]] && XRAY_PANEL_PASSWORD=$(openssl rand -hex 16) && env_set "XRAY_PANEL_PASSWORD" "$XRAY_PANEL_PASSWORD"
    [[ -z "${GRAFANA_PASSWORD:-}" ]]    && GRAFANA_PASSWORD=$(openssl rand -hex 16)    && env_set "GRAFANA_PASSWORD" "$GRAFANA_PASSWORD"

    cat > "$VPS_ENV_TMP" << EOF
# VPS .env — сгенерировано setup.sh
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ADMIN_CHAT_ID=${TELEGRAM_ADMIN_CHAT_ID:-}
CF_TUNNEL_TOKEN=${CF_TUNNEL_TOKEN:-}
CF_CDN_HOSTNAME=${CF_CDN_HOSTNAME:-}
CF_CDN_UUID=${CF_CDN_UUID:-}
XRAY_UUID=${XRAY_UUID:-}
XRAY_GRPC_UUID=${XRAY_GRPC_UUID:-}
XRAY_PRIVATE_KEY=${XRAY_PRIVATE_KEY:-}
XRAY_GRPC_PRIVATE_KEY=${XRAY_GRPC_PRIVATE_KEY:-}
XRAY_PUBLIC_KEY=${XRAY_PUBLIC_KEY:-}
XRAY_GRPC_PUBLIC_KEY=${XRAY_GRPC_PUBLIC_KEY:-}
XHTTP_MS_PASSWORD=${XHTTP_MS_PASSWORD:-}
XHTTP_CDN_PASSWORD=${XHTTP_CDN_PASSWORD:-}
HYSTERIA2_AUTH=${HYSTERIA2_AUTH:-}
HYSTERIA2_OBFS_PASSWORD=${HYSTERIA2_OBFS_PASSWORD:-}
XRAY_PANEL_PASSWORD=${XRAY_PANEL_PASSWORD:-}
GRAFANA_PASSWORD=${GRAFANA_PASSWORD:-}
VPS_IP=${VPS_IP:-}
VPS_TUNNEL_IP=${VPS_TUNNEL_IP:-10.177.2.2}
HOME_TUNNEL_IP=${HOME_TUNNEL_IP:-10.177.2.1}
HOME_SERVER_IP=${HOME_SERVER_IP:-}
WATCHDOG_API_TOKEN=${WATCHDOG_API_TOKEN:-}
DOMAIN=${DOMAIN:-}
CF_API_TOKEN=${CF_API_TOKEN:-}
SSH_ADDITIONAL_PORT=443
EOF

    vps_copy "$VPS_ENV_TMP" "sysadmin@${VPS_IP}:/opt/vpn/.env"
    vps_exec "chmod 600 /opt/vpn/.env"
    rm -f "$VPS_ENV_TMP"

    log_ok ".env скопирован на VPS"
    step_done "step39_vps_env"
fi

# ── Шаг 40: Генерация mTLS CA на VPS ─────────────────────────────────────────

if is_done "step40_vps_mtls_ca"; then
    step_skip "step40_vps_mtls_ca"
else
    step "Генерация mTLS CA (корневой сертификат)"
    log_info "mTLS CA: 4096 bit RSA, 10 лет — для защиты панели Grafana/3x-ui"
    log_info "Клиентский сертификат: запрос через Telegram /renew-cert"

    vps_exec "mkdir -p /opt/vpn/nginx/mtls /opt/vpn/nginx/ssl"

    # Генерируем CA только если ещё нет
    vps_exec "[ -f /opt/vpn/nginx/mtls/ca.crt ] && echo 'exists' || ( \
        openssl genrsa -out /opt/vpn/nginx/mtls/ca.key 4096 2>/dev/null && \
        openssl req -new -x509 -days 3650 \
            -key /opt/vpn/nginx/mtls/ca.key \
            -out /opt/vpn/nginx/mtls/ca.crt \
            -subj '/CN=VPN-CA/O=VPNInfra/C=RU' 2>/dev/null && \
        chmod 600 /opt/vpn/nginx/mtls/ca.key && \
        echo 'CA создан' \
    )" | grep -v '^$' | while IFS= read -r line; do log_info "$line"; done || true

    # Генерируем server.crt/server.key для nginx (подписываем нашим CA)
    vps_exec "[ -f /opt/vpn/nginx/ssl/server.crt ] && echo 'exists' || ( \
        openssl genrsa -out /opt/vpn/nginx/ssl/server.key 2048 2>/dev/null && \
        openssl req -new \
            -key /opt/vpn/nginx/ssl/server.key \
            -out /opt/vpn/nginx/ssl/server.csr \
            -subj '/CN=vpn-server/O=VPNInfra/C=RU' 2>/dev/null && \
        openssl x509 -req -days 730 \
            -in /opt/vpn/nginx/ssl/server.csr \
            -CA /opt/vpn/nginx/mtls/ca.crt \
            -CAkey /opt/vpn/nginx/mtls/ca.key \
            -CAcreateserial \
            -out /opt/vpn/nginx/ssl/server.crt 2>/dev/null && \
        rm -f /opt/vpn/nginx/ssl/server.csr && \
        chmod 600 /opt/vpn/nginx/ssl/server.key && \
        echo 'server cert создан' \
    )" | grep -v '^$' | while IFS= read -r line; do log_info "$line"; done || true

    log_ok "mTLS CA и server cert готовы на VPS"
    step_done "step40_vps_mtls_ca"
fi

# ── Шаг 41: Генерация конфига Hysteria2-сервера ─────────────────────────────

if is_done "step41_vps_hysteria2_config"; then
    step_skip "step41_vps_hysteria2_config"
else
    step "Генерация конфига Hysteria2-сервера и TLS-сертификата"

    # Генерируем self-signed cert для Hysteria2 (TLS over QUIC)
    vps_exec "sudo mkdir -p /opt/vpn/hysteria2 && \
        sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
            -keyout /opt/vpn/hysteria2/server.key \
            -out /opt/vpn/hysteria2/server.crt \
            -days 3650 -nodes \
            -subj \"/CN=${VPS_IP}\" \
            -addext \"subjectAltName=IP:${VPS_IP}\" 2>/dev/null && \
        sudo chmod 600 /opt/vpn/hysteria2/server.key"

    # Генерируем server.yaml с реальными значениями из .env
    vps_exec "sudo tee /opt/vpn/hysteria2/server.yaml > /dev/null << 'HYEOF'
listen: :443

# Сертификат монтируется в контейнер: ./hysteria2 → /etc/hysteria2
tls:
  cert: /etc/hysteria2/server.crt
  key: /etc/hysteria2/server.key

obfs:
  type: salamander
  salamander:
    password: ${HYSTERIA2_OBFS_PASSWORD}

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

    log_ok "Hysteria2 server.yaml и TLS cert сгенерированы"
    step_done "step41_vps_hysteria2_config"
fi

# ── Шаг 42: Запуск Docker Compose на VPS ────────────────────────────────────

if is_done "step42_vps_docker_compose"; then
    step_skip "step42_vps_docker_compose"
else
    step "Запуск Docker Compose на VPS"
    log_info "Запускаем: 3x-ui (Xray inbounds), nginx (mTLS :8443),"
    log_info "           cloudflared, prometheus, alertmanager, grafana, node-exporter, hysteria2"

    # Если SSH при socat bootstrap был добавлен на порт 443 — убираем его СЕЙЧАС,
    # до запуска 3x-ui/Xray которые занимают TCP 443.
    if vps_exec "grep -q '^Port 443' /etc/ssh/sshd_config" 2>/dev/null; then
        log_info "Освобождаем порт 443 от SSH (Xray займёт его)..."
        vps_exec "sudo sed -i '/^Port 443$/d' /etc/ssh/sshd_config && \
            sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null"
        env_set "VPS_SSH_PORT" "22"; VPS_SSH_PORT="22"
        log_ok "SSH освобождён с порта 443, остался на порту 22"
        log_info "Примечание: дальнейший доступ к VPS — через адаптивный SOCKS5 прокси (Block 2)"
    fi

    # Добавляем аварийный SSH порт 8022 — доступен напрямую при падении всех стеков.
    # DPI блокирует SSH на 22 и 443, 8022 остаётся как запасной выход без прокси.
    vps_exec "grep -q '^Port 8022$' /etc/ssh/sshd_config || { \
        grep -q '^Port 22$' /etc/ssh/sshd_config || echo 'Port 22' | sudo tee -a /etc/ssh/sshd_config; \
        echo 'Port 8022' | sudo tee -a /etc/ssh/sshd_config; \
        sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null; \
    }"
    log_ok "SSH слушает на порту 22 + 8022 (аварийный)"

    # Проверяем наличие docker-compose.yml на VPS
    if ! vps_exec "[ -f /opt/vpn/docker-compose.yml ] && echo exists" 2>/dev/null \
            | grep -q "exists"; then
        log_warn "docker-compose.yml не найден на VPS в /opt/vpn/"
        log_warn "Скопируйте файл VPS конфигурации и повторите шаг."
    else
        log_info "Загрузка Docker-образов на VPS..."
        vps_exec_long "cd /opt/vpn && sudo docker compose pull --quiet 2>/dev/null || true"

        log_info "Запуск контейнеров на VPS..."
        vps_exec_long "cd /opt/vpn && sudo docker compose up -d --remove-orphans 2>/dev/null \
            || sudo docker compose up -d 2>/dev/null || true"

        # Ожидание запуска 3x-ui
        sleep 15
        echo ""
        log_info "Статус контейнеров на VPS:"
        vps_exec "cd /opt/vpn && sudo docker compose ps 2>/dev/null || true"
    fi

    # Проверяем, что iperf3 запустился (слушает только на tier-2 интерфейсе)
    sleep 5
    IPERF3_STATUS=$(vps_exec "sudo docker inspect --format='{{.State.Status}}' iperf3 2>/dev/null || echo 'not_found'")
    if [[ "$IPERF3_STATUS" == "running" ]]; then
        log_ok "iperf3 запущен на VPS (будет доступен на 10.177.2.2:5201 после tier-2 туннеля)"
    else
        log_warn "iperf3 статус: ${IPERF3_STATUS} — проверьте 'sudo docker compose ps' на VPS"
    fi

    log_ok "Docker Compose на VPS запущен"
    step_done "step42_vps_docker_compose"
fi

# ── Шаг 43: Настройка инбаундов 3x-ui через API ─────────────────────────────

if is_done "step43_vps_3xui_inbounds"; then
    step_skip "step43_vps_3xui_inbounds"
else
    step "Настройка VLESS-XHTTP инбаундов в 3x-ui"
    log_info "Создаём 3 inbound в 3x-ui через API:"
    log_info "  VLESS-XHTTP-microsoft  :2087  (стек reality,      SNI: microsoft.com)"
    log_info "  VLESS-XHTTP-jsdelivr   :2083  (стек reality-grpc, SNI: cdn.jsdelivr.net)"
    log_info "  VLESS-WS-cdn           :8080  (стек cdn, для Cloudflare Worker)"

    SETUP_SCRIPT="${REPO_DIR}/vps/scripts/xray-setup.sh"
    if [[ ! -f "$SETUP_SCRIPT" ]]; then
        log_warn "Файл vps/scripts/xray-setup.sh не найден — пропуск"
        log_warn "Настройте инбаунды вручную: http://${VPS_IP}:2053"
    else
        vps_copy "$SETUP_SCRIPT" "sysadmin@${VPS_IP}:/tmp/xray-setup.sh"
        vps_exec "chmod +x /tmp/xray-setup.sh && bash /tmp/xray-setup.sh && rm -f /tmp/xray-setup.sh"
        log_ok "Инбаунды 3x-ui настроены"
        # После обновления инбаундов — синхронизировать ключи и пересоздать конфиги
        sed -i '/^step46b_sync_xray_keys$/d; /^step47_xray_client_configs$/d' "$STATE_FILE" 2>/dev/null || true
    fi

    step_done "step43_vps_3xui_inbounds"
fi

# ── Шаг 44: Git-зеркало и healthcheck cron на VPS ────────────────────────────

if is_done "step44_vps_git_mirror_cron"; then
    step_skip "step44_vps_git_mirror_cron"
else
    step "Настройка git-зеркала и healthcheck cron на VPS"

    # Инициализация bare git-репозитория (зеркало GitHub)
    vps_exec "mkdir -p /opt/vpn/vpn-repo.git && \
        git -C /opt/vpn/vpn-repo.git init --bare 2>/dev/null || true"

    vps_exec "git -C /opt/vpn/vpn-repo.git remote add origin \
        https://github.com/Cyrillicspb/vpn-infra.git 2>/dev/null || \
        git -C /opt/vpn/vpn-repo.git remote set-url origin \
        https://github.com/Cyrillicspb/vpn-infra.git 2>/dev/null || true"

    # Начальное зеркалирование
    log_info "Попытка начальной синхронизации с GitHub..."
    vps_exec "git -C /opt/vpn/vpn-repo.git fetch --all 2>/dev/null || \
        echo 'GitHub недоступен — синхронизация выполнится позже по cron'" || true

    # Cron для зеркалирования (каждые 30 минут)
    vps_exec "cat << 'CRONEOF' | sudo tee /etc/cron.d/vpn-mirror > /dev/null
SHELL=/bin/bash
# Git-зеркало GitHub (каждые 30 минут)
*/30 * * * * sysadmin git -C /opt/vpn/vpn-repo.git fetch --all \
    >> /var/log/vpn-mirror.log 2>&1
CRONEOF"

    # Cron для VPS healthcheck (каждые 5 минут)
    vps_exec "cat << 'HCEOF' | sudo tee /etc/cron.d/vps-healthcheck > /dev/null
SHELL=/bin/bash
# VPS healthcheck каждые 5 минут
*/5 * * * * sysadmin bash /opt/vpn/scripts/vps-healthcheck.sh \
    >> /var/log/vps-healthcheck.log 2>&1
HCEOF"

    # Создание скрипта healthcheck если его нет
    vps_exec "[ -f /opt/vpn/scripts/vps-healthcheck.sh ] && echo 'exists' || \
        mkdir -p /opt/vpn/scripts && cat << 'HSEOF' > /opt/vpn/scripts/vps-healthcheck.sh
#!/bin/bash
# vps-healthcheck.sh — Мониторинг состояния VPS
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true

send_alert() {
    local msg=\"\$1\"
    curl -sf \"https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN}/sendMessage\" \
        -d \"chat_id=\${TELEGRAM_ADMIN_CHAT_ID}&text=\${msg}\" \
        > /dev/null 2>&1 || true
}

# Проверка остановленных контейнеров
sudo docker ps --filter \"status=exited\" --format \"{{.Names}}\" 2>/dev/null \
    | while read -r container; do
        send_alert \"VPS: контейнер \${container} остановился\"
    done

# Проверка заполнения диска
DISK_USE=\$(df -h / 2>/dev/null | awk 'NR==2 {print int(\$5)}')
if [[ \${DISK_USE:-0} -ge 85 ]]; then
    send_alert \"VPS: диск заполнен на \${DISK_USE}%%\"
fi
HSEOF
chmod +x /opt/vpn/scripts/vps-healthcheck.sh"

    # Разрешения на cron-файлы
    vps_exec "sudo chmod 644 /etc/cron.d/vpn-mirror /etc/cron.d/vps-healthcheck \
        2>/dev/null || true"

    log_ok "Git-зеркало и healthcheck cron настроены на VPS"
    step_done "step44_vps_git_mirror_cron"
fi

log_info "═══ Фаза 2 (VPS) завершена ═══"
echo ""
echo -e "${YELLOW}ВАЖНО — SSH к VPS после настройки:${NC}"
echo "  Прямой SSH к VPS (порт 22) доступен ТОЛЬКО через SOCKS5-прокси xray-client-2."
echo "  После запуска VPN-стеков используйте команду с домашнего сервера:"
echo "    ssh -i /root/.ssh/vpn_id_ed25519 \\"
echo "        -o ProxyCommand=\"nc -X 5 -x 127.0.0.1:1081 %h %p\" \\"
echo "        sysadmin@${VPS_IP:-<VPS_IP>}"
echo "  (xray-client-2 должен быть запущен: docker ps | grep xray-client-2)"

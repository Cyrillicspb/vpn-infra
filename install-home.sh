#!/bin/bash
# =============================================================================
# install-home.sh — Установка компонентов на домашнем сервере
# Вызывается из setup.sh
# =============================================================================
set -euo pipefail

STATE_FILE="${1:-/opt/vpn/.setup-state}"
REPO_DIR="/opt/vpn"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

STEP=5  # Продолжаем нумерацию с шага 5
TOTAL=51

step() { ((STEP++)); echo -e "\n${BLUE}━━━ Шаг ${STEP}/${TOTAL}: $* ━━━${NC}"; }
is_done()   { grep -q "^$1$" "$STATE_FILE" 2>/dev/null; }
step_done() { echo "$1" >> "$STATE_FILE"; log_ok "Готово: $1"; }
step_skip() { log_info "Пропуск (уже выполнено): $1"; }

# ---------------------------------------------------------------------------
# Шаг: Обновление системы
# ---------------------------------------------------------------------------
step "Обновление пакетов"
if ! is_done "apt_update"; then
    apt-get update -qq
    apt-get upgrade -y -qq
    step_done "apt_update"
else step_skip "apt_update"; fi

# ---------------------------------------------------------------------------
# Шаг: Базовые зависимости
# ---------------------------------------------------------------------------
step "Установка зависимостей"
if ! is_done "deps_installed"; then
    apt-get install -y -qq \
        curl wget git jq nftables dnsmasq \
        python3 python3-pip python3-venv \
        wireguard-tools iproute2 iptables \
        sqlite3 flock net-tools conntrack \
        fail2ban unattended-upgrades \
        logrotate cron gnupg2 \
        aggregate6 || apt-get install -y -qq aggregate || true
    step_done "deps_installed"
else step_skip "deps_installed"; fi

# ---------------------------------------------------------------------------
# Шаг: Отключение IPv6
# ---------------------------------------------------------------------------
step "Отключение IPv6"
if ! is_done "ipv6_disabled"; then
    cat > /etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf
    step_done "ipv6_disabled"
else step_skip "ipv6_disabled"; fi

# ---------------------------------------------------------------------------
# Шаг: BBR и IP forwarding
# ---------------------------------------------------------------------------
step "Настройка ядра (BBR + forwarding)"
if ! is_done "kernel_tuning"; then
    cat > /etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
EOF
    sysctl -p /etc/sysctl.d/99-bbr.conf
    step_done "kernel_tuning"
else step_skip "kernel_tuning"; fi

# ---------------------------------------------------------------------------
# Шаг: Пин ядра (запрет автообновления ядра)
# ---------------------------------------------------------------------------
step "Закрепление версии ядра"
if ! is_done "kernel_pinned"; then
    KERNEL_VERSION=$(uname -r)
    cat > /etc/apt/preferences.d/pin-kernel <<EOF
Package: linux-image-*
Pin: version *
Pin-Priority: -1

Package: linux-image-${KERNEL_VERSION}
Pin: version *
Pin-Priority: 1001
EOF
    step_done "kernel_pinned"
else step_skip "kernel_pinned"; fi

# ---------------------------------------------------------------------------
# Шаг: Docker
# ---------------------------------------------------------------------------
step "Установка Docker"
if ! is_done "docker_installed"; then
    if ! command -v docker &>/dev/null; then
        curl -fsSL https://get.docker.com | sh
    fi
    # Конфиг Docker
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "dns": ["127.0.0.1", "1.1.1.1"]
}
EOF
    systemctl enable docker
    systemctl restart docker
    step_done "docker_installed"
else step_skip "docker_installed"; fi

# ---------------------------------------------------------------------------
# Шаг: AmneziaWG
# ---------------------------------------------------------------------------
step "Установка AmneziaWG"
if ! is_done "amneziawg_installed"; then
    add-apt-repository -y ppa:amnezia/ppa 2>/dev/null || \
        curl -fsSL https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu/dists/noble/Release.gpg \
            -o /etc/apt/trusted.gpg.d/amnezia.gpg
    apt-get update -qq
    apt-get install -y -qq amneziawg-dkms amneziawg-tools || \
        log_warn "AmneziaWG не установился — установите вручную"
    step_done "amneziawg_installed"
else step_skip "amneziawg_installed"; fi

# ---------------------------------------------------------------------------
# Шаг: Hysteria2
# ---------------------------------------------------------------------------
step "Установка Hysteria2"
if ! is_done "hysteria2_installed"; then
    HYSTERIA_VERSION="app/v2.5.1"
    HYSTERIA_URL="https://github.com/apernet/hysteria/releases/download/${HYSTERIA_VERSION}/hysteria-linux-amd64"
    curl -fsSL "$HYSTERIA_URL" -o /usr/local/bin/hysteria
    chmod +x /usr/local/bin/hysteria
    step_done "hysteria2_installed"
else step_skip "hysteria2_installed"; fi

# ---------------------------------------------------------------------------
# Шаг: tun2socks
# ---------------------------------------------------------------------------
step "Установка tun2socks"
if ! is_done "tun2socks_installed"; then
    TUN2SOCKS_URL="https://github.com/xjasonlyu/tun2socks/releases/download/v2.5.2/tun2socks-linux-amd64.zip"
    curl -fsSL "$TUN2SOCKS_URL" -o /tmp/tun2socks.zip
    cd /tmp && unzip -q tun2socks.zip
    mv tun2socks-linux-amd64 /usr/local/bin/tun2socks
    chmod +x /usr/local/bin/tun2socks
    rm -f /tmp/tun2socks.zip
    step_done "tun2socks_installed"
else step_skip "tun2socks_installed"; fi

# ---------------------------------------------------------------------------
# Шаг: Генерация ключей WireGuard
# ---------------------------------------------------------------------------
step "Генерация ключей WireGuard/AmneziaWG"
if ! is_done "wg_keys_generated"; then
    mkdir -p /etc/wireguard
    chmod 700 /etc/wireguard

    # wg0 (AmneziaWG)
    if [[ ! -f /etc/wireguard/wg0-server.key ]]; then
        wg genkey > /etc/wireguard/wg0-server.key
        wg pubkey < /etc/wireguard/wg0-server.key > /etc/wireguard/wg0-server.pub
        chmod 600 /etc/wireguard/wg0-server.key
    fi

    # wg1 (WireGuard)
    if [[ ! -f /etc/wireguard/wg1-server.key ]]; then
        wg genkey > /etc/wireguard/wg1-server.key
        wg pubkey < /etc/wireguard/wg1-server.key > /etc/wireguard/wg1-server.pub
        chmod 600 /etc/wireguard/wg1-server.key
    fi
    step_done "wg_keys_generated"
else step_skip "wg_keys_generated"; fi

# ---------------------------------------------------------------------------
# Шаг: nftables
# ---------------------------------------------------------------------------
step "Настройка nftables"
if ! is_done "nftables_configured"; then
    source "$REPO_DIR/.env" 2>/dev/null || true
    NET_IFACE="${NET_INTERFACE:-$(ip route | grep default | awk '{print $5}' | head -1)}"

    cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
# nftables — VPN Infrastructure
# Автогенерировано install-home.sh

flush ruleset

define VPN_NET  = { 10.177.1.0/24, 10.177.3.0/24 }
define DOCKER_NET = 172.20.0.0/24
define ETH_IFACE = "${NET_IFACE}"
define AWG_PORT  = 51820
define WG_PORT   = 51821

table inet filter {
    # nft sets для маршрутизации
    set blocked_static {
        type ipv4_addr
        flags interval
        # заполняется vpn-sets-restore.service
    }

    set blocked_dynamic {
        type ipv4_addr
        flags interval, timeout
        timeout 24h
        # заполняется dnsmasq nftset=/
    }

    chain input {
        type filter hook input priority 0; policy drop;

        # Базовые правила
        ct state established,related accept
        iifname lo accept

        # SSH
        tcp dport 22 accept

        # WireGuard / AmneziaWG
        udp dport { \$AWG_PORT, \$WG_PORT } limit rate 100/second burst 200 packets accept
        udp dport { \$AWG_PORT, \$WG_PORT } drop

        # Watchdog API — только из Docker-сети
        ip saddr \$DOCKER_NET tcp dport 8080 accept

        # ICMP
        icmp type { echo-request, echo-reply } accept
    }

    chain forward {
        type filter hook forward priority 0; policy drop;

        ct state established,related accept

        # Трафик от VPN-клиентов
        iifname { "wg0", "wg1" } accept

        # Kill switch: заблокированный трафик только через tun
        ip saddr \$VPN_NET ip daddr @blocked_static oifname != "tun*" drop
        ip saddr \$VPN_NET ip daddr @blocked_dynamic oifname != "tun*" drop

        # Разрешить остальной forwarding из VPN
        iifname { "wg0", "wg1" } oifname { \$ETH_IFACE, "tun*" } accept
    }

    chain output {
        type filter hook output priority 0; policy accept;
    }
}

table inet nat {
    chain postrouting {
        type nat hook postrouting priority 100;

        # NAT для VPN-клиентов → eth0 (незаблокированный трафик)
        ip saddr \$VPN_NET oifname \$ETH_IFACE masquerade

        # NAT для VPN-клиентов → tun (заблокированный трафик)
        ip saddr \$VPN_NET oifname "tun*" masquerade
    }
}

table inet mangle {
    chain prerouting {
        type filter hook prerouting priority -150;

        # fwmark для заблокированных адресов
        ip saddr \$VPN_NET ip daddr @blocked_static meta mark set 0x1
        ip saddr \$VPN_NET ip daddr @blocked_dynamic meta mark set 0x1
    }
}
EOF

    systemctl enable nftables
    systemctl restart nftables
    step_done "nftables_configured"
else step_skip "nftables_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: Policy routing
# ---------------------------------------------------------------------------
step "Настройка policy routing"
if ! is_done "policy_routing_configured"; then
    source "$REPO_DIR/.env" 2>/dev/null || true
    NET_IFACE="${NET_INTERFACE:-$(ip route | grep default | awk '{print $5}' | head -1)}"
    GW_IP="${GATEWAY_IP:-$(ip route | grep default | awk '{print $3}' | head -1)}"

    cat > /etc/systemd/system/vpn-routes.service <<EOF
[Unit]
Description=VPN Policy Routing
After=network-online.target nftables.service vpn-sets-restore.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash /opt/vpn/scripts/setup-routes.sh
ExecStop=/bin/bash /opt/vpn/scripts/teardown-routes.sh

[Install]
WantedBy=multi-user.target
EOF

    cat > /opt/vpn/scripts/setup-routes.sh <<EOF
#!/bin/bash
# Настройка policy routing
ETH_IFACE="${NET_IFACE}"
GW_IP="${GW_IP}"

# Таблица 100: прямой интернет
ip route add default via \$GW_IP dev \$ETH_IFACE table 100 2>/dev/null || true

# ip rules
ip rule add priority 100 fwmark 0x1 lookup 200 2>/dev/null || true
ip rule add priority 150 to 1.1.1.1 lookup 200 2>/dev/null || true
ip rule add priority 150 to 8.8.8.8 lookup 200 2>/dev/null || true
ip rule add priority 200 from 10.177.1.0/24 lookup 100 2>/dev/null || true
ip rule add priority 200 from 10.177.3.0/24 lookup 100 2>/dev/null || true
EOF
    chmod +x /opt/vpn/scripts/setup-routes.sh

    cat > /opt/vpn/scripts/teardown-routes.sh <<'EOF'
#!/bin/bash
ip rule del priority 100 fwmark 0x1 lookup 200 2>/dev/null || true
ip rule del priority 200 from 10.177.1.0/24 lookup 100 2>/dev/null || true
ip rule del priority 200 from 10.177.3.0/24 lookup 100 2>/dev/null || true
EOF
    chmod +x /opt/vpn/scripts/teardown-routes.sh

    systemctl daemon-reload
    systemctl enable vpn-routes.service
    step_done "policy_routing_configured"
else step_skip "policy_routing_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: vpn-sets-restore.service
# ---------------------------------------------------------------------------
step "Настройка vpn-sets-restore.service"
if ! is_done "sets_restore_configured"; then
    touch /etc/nftables-blocked-static.conf

    cat > /etc/systemd/system/vpn-sets-restore.service <<'EOF'
[Unit]
Description=Восстановление nft blocked_static после перезагрузки
After=nftables.service
Before=dnsmasq.service watchdog.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/nft -f /etc/nftables-blocked-static.conf

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable vpn-sets-restore.service
    step_done "sets_restore_configured"
else step_skip "sets_restore_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: dnsmasq
# ---------------------------------------------------------------------------
step "Настройка dnsmasq"
if ! is_done "dnsmasq_configured"; then
    cp "$REPO_DIR/home/dnsmasq/dnsmasq.conf" /etc/dnsmasq.conf
    mkdir -p /etc/dnsmasq.d
    cp "$REPO_DIR/home/dnsmasq/dnsmasq.d/"* /etc/dnsmasq.d/ 2>/dev/null || true
    # Останавливаем systemd-resolved чтобы dnsmasq мог занять порт 53
    systemctl disable --now systemd-resolved 2>/dev/null || true
    systemctl enable dnsmasq
    step_done "dnsmasq_configured"
else step_skip "dnsmasq_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: fail2ban
# ---------------------------------------------------------------------------
step "Настройка fail2ban"
if ! is_done "fail2ban_configured"; then
    cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = 22
EOF
    systemctl enable fail2ban
    systemctl restart fail2ban
    step_done "fail2ban_configured"
else step_skip "fail2ban_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: Watchdog Python venv
# ---------------------------------------------------------------------------
step "Создание Python venv для watchdog"
if ! is_done "watchdog_venv"; then
    mkdir -p /opt/vpn/watchdog
    cp -r "$REPO_DIR/home/watchdog/." /opt/vpn/watchdog/
    python3 -m venv /opt/vpn/watchdog/venv
    /opt/vpn/watchdog/venv/bin/pip install -q -r /opt/vpn/watchdog/requirements.txt
    step_done "watchdog_venv"
else step_skip "watchdog_venv"; fi

# ---------------------------------------------------------------------------
# Шаг: Systemd units watchdog
# ---------------------------------------------------------------------------
step "Установка watchdog.service"
if ! is_done "watchdog_service"; then
    cp /opt/vpn/watchdog/watchdog.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable watchdog.service
    step_done "watchdog_service"
else step_skip "watchdog_service"; fi

# ---------------------------------------------------------------------------
# Шаг: logrotate
# ---------------------------------------------------------------------------
step "Настройка logrotate"
if ! is_done "logrotate_configured"; then
    cat > /etc/logrotate.d/vpn <<'EOF'
/var/log/vpn-*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 root root
}
EOF
    step_done "logrotate_configured"
else step_skip "logrotate_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: journald limits
# ---------------------------------------------------------------------------
step "Ограничение journald"
if ! is_done "journald_configured"; then
    mkdir -p /etc/systemd/journald.conf.d
    cat > /etc/systemd/journald.conf.d/vpn.conf <<'EOF'
[Journal]
SystemMaxUse=500M
EOF
    systemctl restart systemd-journald
    step_done "journald_configured"
else step_skip "journald_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: unattended-upgrades
# ---------------------------------------------------------------------------
step "Настройка автообновлений безопасности"
if ! is_done "unattended_upgrades"; then
    cat > /etc/apt/apt.conf.d/50unattended-upgrades <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Package-Blacklist {
    "linux-image-*";
    "linux-headers-*";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
EOF
    step_done "unattended_upgrades"
else step_skip "unattended_upgrades"; fi

# ---------------------------------------------------------------------------
# Шаг: cron задания
# ---------------------------------------------------------------------------
step "Настройка cron"
if ! is_done "cron_configured"; then
    source "$REPO_DIR/.env" 2>/dev/null || true
    ADMIN_CHAT="${TELEGRAM_ADMIN_CHAT_ID:-}"
    BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"

    cat > /etc/cron.d/vpn-routes <<'EOF'
# Обновление маршрутов из баз РКН
0 3 * * * root flock /var/run/vpn-routes.lock python3 /opt/vpn/scripts/update-routes.py >> /var/log/vpn-routes.log 2>&1
EOF

    cat > /etc/cron.d/vpn-backup <<'EOF'
# Резервное копирование
0 4 * * * root bash /opt/vpn/scripts/backup.sh >> /var/log/vpn-backup.log 2>&1
EOF

    cat > /etc/cron.d/vpn-watchdog-failsafe <<EOF
# Failsafe: если watchdog мёртв — алерт в Telegram
*/5 * * * * root systemctl is-active --quiet watchdog || curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" -d "chat_id=${ADMIN_CHAT}&text=⚠️+WATCHDOG+МЁРТВ!+Сервер+$(hostname)" > /dev/null 2>&1
EOF
    step_done "cron_configured"
else step_skip "cron_configured"; fi

# ---------------------------------------------------------------------------
# Шаг: Docker Compose запуск
# ---------------------------------------------------------------------------
step "Запуск Docker Compose"
if ! is_done "docker_compose_up"; then
    cp -r "$REPO_DIR/home/." /opt/vpn/
    # Создаём плейсхолдер .env если нет
    [[ -f /opt/vpn/.env ]] || cp /opt/vpn/.env.example /opt/vpn/.env
    cd /opt/vpn && docker compose up -d --remove-orphans || \
        log_warn "Docker Compose не запустился — проверьте .env"
    step_done "docker_compose_up"
else step_skip "docker_compose_up"; fi

# ---------------------------------------------------------------------------
# Шаг: vpn-postboot.service
# ---------------------------------------------------------------------------
step "Настройка postboot-проверки"
if ! is_done "postboot_configured"; then
    cat > /etc/systemd/system/vpn-postboot.service <<'EOF'
[Unit]
Description=VPN Post-boot проверка и отчёт
After=watchdog.service docker.service
Wants=watchdog.service

[Service]
Type=oneshot
ExecStart=/opt/vpn/scripts/postboot-check.sh
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable vpn-postboot.service
    step_done "postboot_configured"
else step_skip "postboot_configured"; fi

log_ok "install-home.sh завершён"

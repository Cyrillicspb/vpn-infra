#!/bin/bash
# =============================================================================
# install-home.sh — Установка компонентов на домашнем сервере
# Вызывается из setup.sh (STEP=8 bash install-home.sh)
# Шаги 9-28
# =============================================================================

set -euo pipefail

# ── Цвета и константы ────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

STEP="${STEP:-8}"
TOTAL_STEPS=52
STATE_FILE="/opt/vpn/.setup-state"
ENV_FILE="/opt/vpn/.env"

# ── Вспомогательные функции ──────────────────────────────────────────────────

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[✓]${NC}   $*"; }
log_warn()  { echo -e "${YELLOW}[!]${NC}   $*"; }
log_error() { echo -e "${RED}[✗]${NC}   $*" >&2; }

step() {
    ((STEP++)) || true
    echo ""
    echo -e "${CYAN}${BOLD}━━━ Шаг ${STEP}/${TOTAL_STEPS}: $* ━━━${NC}"
}

is_done()   { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
step_done() { echo "$1" >> "$STATE_FILE"; log_ok "Готово: $1"; }
step_skip() { ((STEP++)) || true; log_info "Пропуск (уже выполнено): $1"; }

die() {
    log_error "$*"
    echo ""
    echo -e "${RED}━━━ Ошибка ━━━${NC}"
    echo "  Проблема: $*"
    echo "  Действие: проверьте вывод выше и устраните причину."
    echo "  Повтор:   sudo bash setup.sh  (выполненные шаги будут пропущены)"
    exit 1
}

env_set() {
    local key="$1" val="$2"
    mkdir -p "$(dirname "$ENV_FILE")"
    touch "$ENV_FILE"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

# ── Загрузка переменных окружения ────────────────────────────────────────────

[[ -f "$ENV_FILE" ]] || die "Файл ${ENV_FILE} не найден. Сначала запустите setup.sh"
set -o allexport; source "$ENV_FILE"; set +o allexport

# ── Шаг 9: apt update + upgrade ──────────────────────────────────────────────

if is_done "step09_apt_update"; then
    step_skip "step09_apt_update"
else
    step "Обновление системных пакетов"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
    log_ok "Система обновлена"
    step_done "step09_apt_update"
fi

# ── Шаг 10: Установка системных пакетов ──────────────────────────────────────

if is_done "step10_install_packages"; then
    step_skip "step10_install_packages"
else
    step "Установка системных пакетов"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        curl wget git jq rsync unzip \
        nftables dnsmasq \
        python3 python3-pip python3-venv python3-cryptography \
        wireguard-tools iproute2 \
        sqlite3 net-tools conntrack traceroute \
        fail2ban unattended-upgrades apt-transport-https \
        logrotate cron gnupg2 ca-certificates \
        sshpass autossh \
        uuid-runtime openssl dkms build-essential \
        iperf3
    pip3 install --quiet --break-system-packages aggregate6
    log_ok "Системные пакеты установлены"
    step_done "step10_install_packages"
fi

# ── Шаг 10b: Создание sysadmin и защита SSH ──────────────────────────────────

if is_done "step10b_home_sysadmin"; then
    step_skip "step10b_home_sysadmin"
else
    step "Создание sysadmin и защита SSH (домашний сервер)"

    # Создание sysadmin если не существует
    if ! id sysadmin &>/dev/null; then
        useradd -m -s /bin/bash sysadmin
        usermod -aG sudo sysadmin
        log_ok "Пользователь sysadmin создан"
    else
        log_info "Пользователь sysadmin уже существует"
    fi

    # sudo NOPASSWD
    echo 'sysadmin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/sysadmin
    chmod 440 /etc/sudoers.d/sysadmin

    # Добавить в docker group (понадобится после установки Docker)
    usermod -aG docker sysadmin 2>/dev/null || true

    # Перенос пароля root → sysadmin (копируем хэш из /etc/shadow)
    ROOT_HASH=$(getent shadow root | cut -d: -f2)
    if [[ -n "$ROOT_HASH" && "$ROOT_HASH" != "!" && "$ROOT_HASH" != "*" ]]; then
        usermod -p "$ROOT_HASH" sysadmin
        log_ok "Пароль sysadmin = пароль root"
    else
        log_warn "У root нет пароля — задайте пароль sysadmin вручную: passwd sysadmin"
    fi

    # Копирование SSH-ключей из root → sysadmin (если есть)
    mkdir -p /home/sysadmin/.ssh
    if [[ -f /root/.ssh/authorized_keys && -s /root/.ssh/authorized_keys ]]; then
        cp /root/.ssh/authorized_keys /home/sysadmin/.ssh/authorized_keys
        log_ok "SSH-ключи скопированы из root в sysadmin"
    else
        touch /home/sysadmin/.ssh/authorized_keys
    fi
    chown -R sysadmin:sysadmin /home/sysadmin/.ssh
    chmod 700 /home/sysadmin/.ssh
    chmod 600 /home/sysadmin/.ssh/authorized_keys

    # Защита SSH: запретить вход под root, вход по паролю для sysadmin оставить
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
    grep -q '^PermitRootLogin' /etc/ssh/sshd_config \
        || echo 'PermitRootLogin no' >> /etc/ssh/sshd_config

    systemctl reload sshd
    log_ok "SSH: PermitRootLogin no (вход по паролю для sysadmin сохранён)"
    log_warn "Для входа: ssh sysadmin@${HOME_SERVER_IP:-$(hostname -I | awk '{print $1}')}"

    step_done "step10b_home_sysadmin"
fi

# ── Шаг 11: Отключение IPv6 ──────────────────────────────────────────────────

if is_done "step11_disable_ipv6"; then
    step_skip "step11_disable_ipv6"
else
    step "Отключение IPv6"

    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true

    # Отключение IPv6 в GRUB
    if [[ -f /etc/default/grub ]]; then
        if ! grep -q "ipv6.disable=1" /etc/default/grub; then
            sed -i 's/GRUB_CMDLINE_LINUX="\(.*\)"/GRUB_CMDLINE_LINUX="\1 ipv6.disable=1"/' \
                /etc/default/grub
            update-grub 2>/dev/null || true
        fi
    fi

    log_ok "IPv6 отключён"
    step_done "step11_disable_ipv6"
fi

# ── Шаг 12: Настройка ядра (BBR + IP forwarding + rp_filter) ─────────────────

if is_done "step12_kernel_tuning"; then
    step_skip "step12_kernel_tuning"
else
    step "Настройка параметров ядра (BBR, IP forwarding, rp_filter)"

    cat > /etc/sysctl.d/99-vpn.conf << 'EOF'
# TCP BBR (оптимизация пропускной способности)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# IP forwarding (обязательно для VPN-сервера)
net.ipv4.ip_forward = 1

# Отключение reverse path filter (необходимо для policy routing)
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0

# Безопасность
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
EOF
    sysctl --system 2>/dev/null || sysctl -p /etc/sysctl.d/99-vpn.conf 2>/dev/null || true
    log_ok "Параметры ядра настроены"
    step_done "step12_kernel_tuning"
fi

# ── Шаг 13: Фиксация версии ядра (предотвращение автообновления) ─────────────

if is_done "step13_pin_kernel"; then
    step_skip "step13_pin_kernel"
else
    step "Фиксация версии ядра (pin kernel)"

    KERNEL_VERSION=$(uname -r)
    log_info "Текущее ядро: ${KERNEL_VERSION}"

    cat > /etc/apt/preferences.d/pin-kernel << EOF
# Запрет автообновления ядра (защита DKMS модулей)
Package: linux-image-*
Pin: version *
Pin-Priority: -1

Package: linux-image-${KERNEL_VERSION}
Pin: version *
Pin-Priority: 1001

Package: linux-headers-*
Pin: version *
Pin-Priority: -1

Package: linux-headers-${KERNEL_VERSION}
Pin: version *
Pin-Priority: 1001

Package: linux-modules-*
Pin: version *
Pin-Priority: -1

Package: linux-modules-${KERNEL_VERSION}
Pin: version *
Pin-Priority: 1001
EOF

    apt-mark hold "linux-image-${KERNEL_VERSION}" 2>/dev/null || true
    apt-mark hold "linux-headers-${KERNEL_VERSION}" 2>/dev/null || true

    log_ok "Версия ядра ${KERNEL_VERSION} зафиксирована"
    step_done "step13_pin_kernel"
fi

# ── Шаг 14: Установка Docker CE ──────────────────────────────────────────────

if is_done "step14_install_docker"; then
    step_skip "step14_install_docker"
else
    step "Установка Docker CE"

    if ! command -v docker &>/dev/null; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        # shellcheck disable=SC1091
        source /etc/os-release
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
            https://download.docker.com/linux/ubuntu \
            ${VERSION_CODENAME} stable" \
            | tee /etc/apt/sources.list.d/docker.list > /dev/null

        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-compose-plugin
        log_ok "Docker CE установлен"
    else
        log_info "Docker уже установлен: $(docker --version)"
    fi

    # Конфигурация Docker daemon
    cat > /etc/docker/daemon.json << 'EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "dns": ["8.8.8.8", "1.1.1.1"],
    "ipv6": false
}
EOF

    systemctl enable docker
    systemctl restart docker
    log_ok "Docker настроен и запущен"
    step_done "step14_install_docker"
fi

# ── Шаг 15: Установка AmneziaWG ──────────────────────────────────────────────

if is_done "step15_install_amneziawg"; then
    step_skip "step15_install_amneziawg"
else
    step "Установка AmneziaWG (DKMS модуль)"

    AWG_INSTALLED=0

    # Попытка установки из PPA Amnezia
    if add-apt-repository -y ppa:amnezia/ppa 2>/dev/null; then
        apt-get update -qq 2>/dev/null || true
        if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            amneziawg-dkms amneziawg-tools 2>/dev/null; then
            AWG_INSTALLED=1
            log_ok "AmneziaWG установлен из PPA"
        fi
    fi

    if [[ $AWG_INSTALLED -eq 0 ]]; then
        # Попытка ручного добавления PPA для noble
        log_warn "PPA недоступен. Попытка через ручной источник..."
        cat > /etc/apt/sources.list.d/amnezia.list \
            << 'EOF'
deb https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu noble main
EOF
        curl -fsSL \
            "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x2EBB9386EA7B5F00" \
            | gpg --dearmor > /etc/apt/trusted.gpg.d/amnezia.gpg 2>/dev/null || true
        apt-get update -qq 2>/dev/null || true

        if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            amneziawg-dkms amneziawg-tools 2>/dev/null; then
            AWG_INSTALLED=1
            log_ok "AmneziaWG установлен (ручной PPA)"
        fi
    fi

    if [[ $AWG_INSTALLED -eq 0 ]]; then
        log_warn "AmneziaWG не удалось установить автоматически."
        log_warn "Установите вручную: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
        log_warn "Продолжаем установку — AmneziaWG можно добавить позже."
    fi

    step_done "step15_install_amneziawg"
fi

# ── Шаг 16: Установка Hysteria2 ──────────────────────────────────────────────

if is_done "step16_install_hysteria2"; then
    step_skip "step16_install_hysteria2"
else
    step "Установка Hysteria2 (бинарник)"

    HYSTERIA_VERSION="v2.5.1"
    HYSTERIA_URL="https://github.com/apernet/hysteria/releases/download/app%2F${HYSTERIA_VERSION}/hysteria-linux-amd64"

    log_info "Загрузка Hysteria2 ${HYSTERIA_VERSION}..."
    curl -fsSL --progress-bar "$HYSTERIA_URL" -o /usr/local/bin/hysteria \
        || die "Не удалось загрузить Hysteria2 с ${HYSTERIA_URL}"
    chmod +x /usr/local/bin/hysteria

    # Проверка запуска
    /usr/local/bin/hysteria version 2>/dev/null \
        || die "Hysteria2 не запускается. Проверьте бинарник."

    mkdir -p /etc/hysteria
    log_ok "Hysteria2 ${HYSTERIA_VERSION} установлен"
    step_done "step16_install_hysteria2"
fi

# ── Шаг 17: Установка tun2socks ──────────────────────────────────────────────

if is_done "step17_install_tun2socks"; then
    step_skip "step17_install_tun2socks"
else
    step "Установка tun2socks"

    TUN2SOCKS_VER="v2.5.2"
    TUN2SOCKS_URL="https://github.com/xjasonlyu/tun2socks/releases/download/${TUN2SOCKS_VER}/tun2socks-linux-amd64.zip"

    log_info "Загрузка tun2socks ${TUN2SOCKS_VER}..."
    curl -fsSL "$TUN2SOCKS_URL" -o /tmp/tun2socks.zip \
        || die "Не удалось загрузить tun2socks"

    cd /tmp
    unzip -qo tun2socks.zip "tun2socks-linux-amd64" 2>/dev/null \
        || unzip -qo tun2socks.zip 2>/dev/null \
        || die "Не удалось распаковать tun2socks.zip"

    if [[ -f /tmp/tun2socks-linux-amd64 ]]; then
        mv /tmp/tun2socks-linux-amd64 /usr/local/bin/tun2socks
    elif [[ -f /tmp/tun2socks ]]; then
        mv /tmp/tun2socks /usr/local/bin/tun2socks
    else
        die "Бинарник tun2socks не найден после распаковки"
    fi

    chmod +x /usr/local/bin/tun2socks
    rm -f /tmp/tun2socks.zip

    log_ok "tun2socks ${TUN2SOCKS_VER} установлен"
    step_done "step17_install_tun2socks"
fi

# ── Шаг 18: Генерация ключей REALITY x25519 через Docker ─────────────────────

if is_done "step18_generate_reality_keys"; then
    step_skip "step18_generate_reality_keys"
else
    step "Генерация REALITY x25519 ключей (через Docker/xray)"

    # Загружаем актуальные переменные
    set -o allexport; source "$ENV_FILE"; set +o allexport

    XRAY_IMAGE="teddysun/xray:1.8.11"

    # Убедимся что Docker доступен
    if ! command -v docker &>/dev/null; then
        log_warn "Docker недоступен — пропускаем генерацию REALITY ключей."
        log_warn "Запустите шаг 18 вручную после установки Docker."
    else
        log_info "Загрузка образа ${XRAY_IMAGE}..."
        docker pull "${XRAY_IMAGE}" --quiet 2>/dev/null || true

        # Генерация ключей REALITY (stack 3: VLESS+REALITY, microsoft.com)
        if [[ -z "${XRAY_PUBLIC_KEY:-}" ]]; then
            XRAY_KEYS=$(docker run --rm "${XRAY_IMAGE}" xray x25519 2>/dev/null) \
                || die "Не удалось запустить xray x25519 в Docker"
            XRAY_PRIVATE_KEY=$(echo "$XRAY_KEYS" | grep "Private key:" | awk '{print $NF}')
            XRAY_PUBLIC_KEY=$(echo "$XRAY_KEYS" | grep "Public key:" | awk '{print $NF}')
            env_set "XRAY_PRIVATE_KEY" "$XRAY_PRIVATE_KEY"
            env_set "XRAY_PUBLIC_KEY"  "$XRAY_PUBLIC_KEY"
            log_ok "Ключи REALITY (microsoft.com) сгенерированы"
        else
            log_info "XRAY_PUBLIC_KEY уже существует"
        fi

        # Генерация ключей REALITY gRPC (stack 2: VLESS+REALITY+gRPC, cdn.jsdelivr.net)
        if [[ -z "${XRAY_GRPC_PUBLIC_KEY:-}" ]]; then
            XRAY_GRPC_KEYS=$(docker run --rm "${XRAY_IMAGE}" xray x25519 2>/dev/null) \
                || die "Не удалось запустить xray x25519 (gRPC) в Docker"
            XRAY_GRPC_PRIVATE_KEY=$(echo "$XRAY_GRPC_KEYS" | grep "Private key:" | awk '{print $NF}')
            XRAY_GRPC_PUBLIC_KEY=$(echo "$XRAY_GRPC_KEYS" | grep "Public key:" | awk '{print $NF}')
            env_set "XRAY_GRPC_PRIVATE_KEY" "$XRAY_GRPC_PRIVATE_KEY"
            env_set "XRAY_GRPC_PUBLIC_KEY"  "$XRAY_GRPC_PUBLIC_KEY"
            log_ok "Ключи REALITY gRPC (cdn.jsdelivr.net) сгенерированы"
        else
            log_info "XRAY_GRPC_PUBLIC_KEY уже существует"
        fi

        chmod 600 "$ENV_FILE"
        set -o allexport; source "$ENV_FILE"; set +o allexport
    fi

    step_done "step18_generate_reality_keys"
fi

# ── Шаг 19: Настройка nftables ───────────────────────────────────────────────

if is_done "step19_configure_nftables"; then
    step_skip "step19_configure_nftables"
else
    step "Настройка nftables (правила + nft sets)"

    # Копируем конфиг из репозитория
    if [[ -f /opt/vpn/home/nftables/nftables.conf ]]; then
        cp /opt/vpn/home/nftables/nftables.conf /etc/nftables.conf
    elif [[ -f /opt/vpn/nftables/nftables.conf ]]; then
        cp /opt/vpn/nftables/nftables.conf /etc/nftables.conf
    else
        log_warn "nftables.conf не найден в репозитории. Используем базовый шаблон."
        # Базовый шаблон на случай отсутствия файла в репозитории
        cat > /etc/nftables.conf << 'EOF'
#!/usr/sbin/nft -f
flush ruleset

table inet vpn {
    set blocked_static {
        type ipv4_addr
        flags interval
        auto-merge
    }

    set blocked_dynamic {
        type ipv4_addr
        flags timeout
        timeout 24h
        gc-interval 1h
    }

    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;
        iifname { "wg0", "wg1" } ip daddr @blocked_static  meta mark set 0x1 accept
        iifname { "wg0", "wg1" } ip daddr @blocked_dynamic meta mark set 0x1 accept
    }

    chain forward {
        type filter hook forward priority filter; policy drop;
        ip saddr 10.177.1.0/24 ip daddr @blocked_static  oifname != "tun*" drop
        ip saddr 10.177.1.0/24 ip daddr @blocked_dynamic oifname != "tun*" drop
        ip saddr 10.177.3.0/24 ip daddr @blocked_static  oifname != "tun*" drop
        ip saddr 10.177.3.0/24 ip daddr @blocked_dynamic oifname != "tun*" drop
        ct state established,related accept
        iifname { "wg0", "wg1" } accept
        oifname { "wg0", "wg1" } accept
        iifname "tun*" accept
        oifname "tun*" accept
        iifname "br-vpn" accept
        oifname "br-vpn" accept
    }

    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        udp dport 51820 limit rate 100/second burst 200 packets accept
        udp dport 51820 drop
        udp dport 51821 limit rate 100/second burst 200 packets accept
        udp dport 51821 drop
        tcp dport 22 ct state new accept
        ip saddr 172.20.0.0/24 tcp dport 8080 accept
        ip saddr { 127.0.0.1, 10.177.2.0/30 } tcp dport 8090 accept
        iifname { "wg0", "wg1" } udp dport 53 accept
        iifname { "wg0", "wg1" } tcp dport 53 accept
        icmp type echo-request limit rate 10/second accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr 10.177.1.0/24 oifname != { "wg0", "wg1", "tun*" } masquerade
        ip saddr 10.177.3.0/24 oifname != { "wg0", "wg1", "tun*" } masquerade
        ip saddr 10.177.1.0/24 oifname "tun*" masquerade
        ip saddr 10.177.3.0/24 oifname "tun*" masquerade
        ip saddr 172.20.0.0/24 oifname != "br-vpn" masquerade
    }
}
EOF
    fi

    # Копируем шаблон blocked-static
    if [[ -f /opt/vpn/home/nftables/nftables-blocked-static.conf ]]; then
        cp /opt/vpn/home/nftables/nftables-blocked-static.conf \
            /etc/nftables-blocked-static.conf
    else
        cat > /etc/nftables-blocked-static.conf << 'EOF'
#!/usr/sbin/nft -f
# Атомарное обновление set blocked_static
# Генерируется скриптом update-routes.py
flush set inet vpn blocked_static
add element inet vpn blocked_static { }
EOF
    fi

    chmod 644 /etc/nftables.conf /etc/nftables-blocked-static.conf

    systemctl enable nftables
    systemctl restart nftables \
        || log_warn "nftables restart завершился с ошибкой — проверьте конфиг: nft -c -f /etc/nftables.conf"

    log_ok "nftables настроен"
    step_done "step19_configure_nftables"
fi

# ── Шаг 19b: Проверка firewall — nmap + ss ────────────────────────────────────
# Firewall — единственная защита. Все порты кроме явно разрешённых DROP.
# Этот шаг убеждается что нет неожиданно открытых сервисов.

if is_done "step19b_verify_firewall"; then
    step_skip "step19b_verify_firewall"
else
    step "Проверка firewall (nmap + ss)"

    # Установить nmap если нет (не добавляем в шаг 10 — нужен только здесь)
    if ! command -v nmap &>/dev/null; then
        log_info "Устанавливаем nmap..."
        apt-get install -y -qq nmap
    fi

    # Тестируем через LAN IP (не loopback) — пакеты проходят через nftables INPUT
    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }
    LAN_IP="${HOME_SERVER_IP:-}"
    if [[ -z "$LAN_IP" ]]; then
        LAN_IP=$(ip -4 addr show "${NET_INTERFACE:-eth0}" 2>/dev/null \
            | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
    fi
    LAN_IP="${LAN_IP:-127.0.0.1}"

    log_info "nmap TCP (top-100 портов) → $LAN_IP ..."
    # --open: только открытые; -oG: grepable; -T4: быстро
    open_tcp=$(nmap -sT --top-ports 100 --open -T4 -oG - "$LAN_IP" 2>/dev/null \
        | grep -oP '\d+/open/tcp' | cut -d/ -f1 | sort -n | tr '\n' ' ' || true)
    open_tcp="${open_tcp%% }"   # trim trailing space

    log_info "Открытые TCP порты: ${open_tcp:-нет}"

    # Разрешённые TCP порты: только SSH
    # (AWG/WG — UDP; watchdog API — только из 172.20.0.0/24; DNS — только из wg0/wg1)
    unexpected_tcp=""
    for port in $open_tcp; do
        case "$port" in
            22) ;;   # SSH — ожидаем
            *)  unexpected_tcp="${unexpected_tcp} ${port}/tcp" ;;
        esac
    done

    if [[ -n "$unexpected_tcp" ]]; then
        log_warn "⚠️  Неожиданные открытые TCP порты:${unexpected_tcp}"
        log_warn "   Проверьте: nft list chain inet vpn input"
        log_warn "   Если порт легитимен — добавьте явно в home/nftables/nftables.conf"
    else
        log_ok "TCP: только SSH (22) открыт снаружи ✓"
    fi

    # Проверка UDP: убеждаемся что AWG/WG слушают (nftables их пропускает)
    log_info "Проверка UDP 51820/51821 (AWG/WG)..."
    for udp_port in 51820 51821; do
        if ss -ulnp 2>/dev/null | grep -q ":${udp_port} "; then
            log_ok "  UDP ${udp_port}: слушает ✓"
        else
            log_warn "  UDP ${udp_port}: не слушает (AWG/WG ещё не запущен — OK на этом этапе)"
        fi
    done

    # Итоговый вывод nft list chain input для финального контроля
    log_info "--- nft list chain inet vpn input ---"
    nft list chain inet vpn input 2>/dev/null || log_warn "nft list chain inet vpn input завершился с ошибкой"
    log_info "-------------------------------------"

    step_done "step19b_verify_firewall"
fi

# ── Шаг 20: Создание конфигов WireGuard-интерфейсов ──────────────────────────

if is_done "step20_wireguard_configs"; then
    step_skip "step20_wireguard_configs"
else
    step "Создание конфигов WireGuard-интерфейсов (wg0 AWG + wg1 WG)"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    mkdir -p /etc/wireguard
    chmod 700 /etc/wireguard

    # AmneziaWG конфиг (wg0, порт 51820)
    cat > /etc/wireguard/wg0.conf << EOF
[Interface]
Address = 10.177.1.1/24
PrivateKey = ${AWG_SERVER_PRIVATE_KEY}
ListenPort = ${WG_AWG_PORT:-51820}
MTU = ${WG_MTU:-1320}

# AWG параметры обфускации (Jc, Jmin, Jmax, S1, S2, H1-H4)
Jc = 4
Jmin = 50
Jmax = 1000
S1 = 30
S2 = 40
H1 = ${AWG_H1}
H2 = ${AWG_H2}
H3 = ${AWG_H3}
H4 = ${AWG_H4}

# Клиенты добавляются watchdog API / Telegram-ботом
# (см. команды /adddevice, /peer/add)
EOF

    # WireGuard конфиг (wg1, порт 51821)
    cat > /etc/wireguard/wg1.conf << EOF
[Interface]
Address = 10.177.3.1/24
PrivateKey = ${WG_SERVER_PRIVATE_KEY}
ListenPort = ${WG_WG_PORT:-51821}
MTU = ${WG_MTU:-1320}

# Клиенты добавляются watchdog API / Telegram-ботом
EOF

    chmod 600 /etc/wireguard/wg0.conf /etc/wireguard/wg1.conf
    log_ok "Конфиги wg0 (AWG) и wg1 (WG) созданы"
    # Примечание: wg-quick@wg0 и wg1 запускаются после Tier-2 в phase3 (шаг 41)
    step_done "step20_wireguard_configs"
fi

# ── Шаг 21: Настройка dnsmasq ────────────────────────────────────────────────

if is_done "step21_configure_dnsmasq"; then
    step_skip "step21_configure_dnsmasq"
else
    step "Настройка dnsmasq (DNS + nftset для blocked_dynamic)"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    # Отключение systemd-resolved (конфликт на порту 53)
    systemctl disable --now systemd-resolved 2>/dev/null || true
    rm -f /etc/resolv.conf
    echo "nameserver 127.0.0.1" > /etc/resolv.conf

    # Копирование конфига из репозитория или создание базового
    if [[ -f /opt/vpn/home/dnsmasq/dnsmasq.conf ]]; then
        cp /opt/vpn/home/dnsmasq/dnsmasq.conf /etc/dnsmasq.conf
        log_ok "dnsmasq.conf скопирован из репозитория"
    elif [[ -f /opt/vpn/dnsmasq/dnsmasq.conf ]]; then
        cp /opt/vpn/dnsmasq/dnsmasq.conf /etc/dnsmasq.conf
    else
        log_warn "dnsmasq.conf не найден — создаём базовый"
        cat > /etc/dnsmasq.conf << EOF
# dnsmasq — VPN Infrastructure (домашний сервер)
# Не логировать DNS-запросы (privacy)
# log-queries

# Слушать только на loopback (WireGuard-интерфейсы добавятся после их поднятия)
listen-address=127.0.0.1
bind-interfaces

# Отключить DHCP
no-dhcp-interface=

# Кэш
cache-size=2048

# Upstream DNS через VPS (заблокированные домены)
# server=/youtube.com/${VPS_TUNNEL_IP:-10.177.2.2}
# Файл с доменами: /etc/dnsmasq.d/vpn-domains.conf

# nftset для автоматического добавления IP заблокированных доменов
# nftset=/<domain>/4#inet#vpn#blocked_dynamic
# Файл: /etc/dnsmasq.d/vpn-nftset.conf

# Публичные DNS как fallback
server=1.1.1.1
server=8.8.8.8
EOF
    fi

    mkdir -p /etc/dnsmasq.d

    # Копирование дополнительных конфигов
    if [[ -d /opt/vpn/home/dnsmasq/dnsmasq.d ]]; then
        cp /opt/vpn/home/dnsmasq/dnsmasq.d/*.conf /etc/dnsmasq.d/ 2>/dev/null || true
        # Обновление IP VPS в конфигах
        VPS_TUN="${VPS_TUNNEL_IP:-10.177.2.2}"
        sed -i "s|10\.177\.2\.2|${VPS_TUN}|g" /etc/dnsmasq.d/*.conf 2>/dev/null || true
        log_ok "Конфиги dnsmasq.d скопированы из репозитория"
    fi

    systemctl enable dnsmasq
    if ! systemctl restart dnsmasq 2>/dev/null; then
        log_warn "dnsmasq не запустился — используем 8.8.8.8 как временный DNS"
        # Обеспечить DNS на время установки (dnsmasq поднимется после wg-интерфейсов)
        printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" > /etc/resolv.conf
        log_warn "dnsmasq не запустился — проверьте: journalctl -u dnsmasq"
    fi

    log_ok "dnsmasq настроен"
    step_done "step21_configure_dnsmasq"
fi

# ── Шаг 22: Настройка policy routing и systemd-юнитов ────────────────────────

if is_done "step22_policy_routing_units"; then
    step_skip "step22_policy_routing_units"
else
    step "Настройка policy routing и systemd-юнитов"

    # Скрипт policy routing уже в /opt/vpn/scripts/ (из репозитория)
    ROUTING_SCRIPT="/opt/vpn/scripts/vpn-policy-routing.sh"
    if [[ ! -f "$ROUTING_SCRIPT" ]]; then
        # Попробуем найти в другом месте
        ROUTING_SCRIPT="/opt/vpn/home/scripts/vpn-policy-routing.sh"
    fi
    [[ -f "$ROUTING_SCRIPT" ]] && chmod +x "$ROUTING_SCRIPT"

    # Создание /etc/iproute2/rt_tables записей если нет
    grep -q "^100 " /etc/iproute2/rt_tables 2>/dev/null \
        || echo "100 vpn" >> /etc/iproute2/rt_tables
    grep -q "^200 " /etc/iproute2/rt_tables 2>/dev/null \
        || echo "200 marked" >> /etc/iproute2/rt_tables

    # Установка systemd-юнитов из репозитория
    SYSTEMD_SRC=""
    for d in /opt/vpn/home/systemd /opt/vpn/systemd; do
        [[ -d "$d" ]] && SYSTEMD_SRC="$d" && break
    done

    if [[ -n "$SYSTEMD_SRC" ]]; then
        for unit in vpn-routes.service vpn-sets-restore.service hysteria2.service \
                    watchdog.service vpn-postboot.service \
                    "tun2socks@.service" autossh-vpn.service; do
            [[ -f "${SYSTEMD_SRC}/${unit}" ]] && \
                cp "${SYSTEMD_SRC}/${unit}" "/etc/systemd/system/${unit}"
        done
        log_ok "systemd-юниты скопированы из репозитория"
    else
        log_warn "Каталог systemd не найден — создаём базовые юниты"

        # Базовый vpn-routes.service
        cat > /etc/systemd/system/vpn-routes.service << 'EOF'
[Unit]
Description=VPN Policy Routing
After=network.target nftables.service awg-quick@wg0.service wg-quick@wg1.service
Wants=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/vpn/scripts/vpn-policy-routing.sh up
ExecStop=/opt/vpn/scripts/vpn-policy-routing.sh down
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

        # Базовый vpn-sets-restore.service
        cat > /etc/systemd/system/vpn-sets-restore.service << 'EOF'
[Unit]
Description=Restore VPN nft blocked_static set
After=nftables.service
Requires=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-blocked-static.conf

[Install]
WantedBy=multi-user.target
EOF

        # Базовый hysteria2.service
        cat > /etc/systemd/system/hysteria2.service << 'EOF'
[Unit]
Description=Hysteria2 VPN Client
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/hysteria client --config /etc/hysteria/config.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

        # Базовый watchdog.service
        cat > /etc/systemd/system/watchdog.service << 'EOF'
[Unit]
Description=VPN Watchdog Agent
After=network.target docker.service dnsmasq.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/vpn/watchdog
EnvironmentFile=/opt/vpn/.env
ExecStart=/opt/vpn/watchdog/venv/bin/python3 /opt/vpn/watchdog/watchdog.py
Restart=always
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=300
WatchdogSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

        # tun2socks@.service (шаблонный)
        cat > "/etc/systemd/system/tun2socks@.service" << 'EOF'
[Unit]
Description=tun2socks for %i
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/tun2socks -device tun-%i -proxy socks5://127.0.0.1:%i
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    fi

    # Установка cron failsafe
    FAILSAFE_SRC=""
    for f in /opt/vpn/home/systemd/vpn-watchdog-failsafe \
              /opt/vpn/systemd/vpn-watchdog-failsafe; do
        [[ -f "$f" ]] && FAILSAFE_SRC="$f" && break
    done

    if [[ -n "$FAILSAFE_SRC" ]]; then
        cp "$FAILSAFE_SRC" /etc/cron.d/vpn-watchdog-failsafe
    else
        cat > /etc/cron.d/vpn-watchdog-failsafe << 'EOF'
# Cron failsafe: проверка watchdog каждые 5 минут
SHELL=/bin/bash
*/5 * * * * root systemctl is-active watchdog &>/dev/null || \
    curl -sf "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN /opt/vpn/.env | cut -d= -f2)/sendMessage" \
        -d "chat_id=$(grep TELEGRAM_ADMIN_CHAT_ID /opt/vpn/.env | cut -d= -f2)&text=WATCHDOG+МЁРТВ" \
        > /dev/null 2>&1 || true
EOF
    fi
    chmod 644 /etc/cron.d/vpn-watchdog-failsafe

    systemctl daemon-reload
    systemctl enable vpn-routes vpn-sets-restore 2>/dev/null || true

    log_ok "Policy routing и systemd-юниты настроены"
    step_done "step22_policy_routing_units"
fi

# ── Шаг 23: Установка Watchdog Python venv ───────────────────────────────────

if is_done "step23_watchdog_venv"; then
    step_skip "step23_watchdog_venv"
else
    step "Создание Python venv для Watchdog"

    WATCHDOG_DIR="/opt/vpn/watchdog"
    mkdir -p "$WATCHDOG_DIR"

    if [[ ! -d "${WATCHDOG_DIR}/venv" ]]; then
        python3 -m venv "${WATCHDOG_DIR}/venv"
        log_ok "Python venv создан в ${WATCHDOG_DIR}/venv"
    else
        log_info "venv уже существует"
    fi

    # Установка зависимостей
    if [[ -f "${WATCHDOG_DIR}/requirements.txt" ]]; then
        "${WATCHDOG_DIR}/venv/bin/pip" install -q --no-cache-dir \
            -r "${WATCHDOG_DIR}/requirements.txt"
        log_ok "Зависимости установлены из requirements.txt"
    else
        log_warn "requirements.txt не найден — устанавливаем базовые зависимости"
        "${WATCHDOG_DIR}/venv/bin/pip" install -q --no-cache-dir \
            aiohttp fastapi uvicorn python-telegram-bot python-dotenv \
            psutil requests
    fi

    # Включение watchdog.service (запускается в phase3 после связки)
    systemctl enable watchdog 2>/dev/null || true

    log_ok "Watchdog Python venv готов"
    step_done "step23_watchdog_venv"
fi

# ── Шаг 24: Настройка fail2ban ───────────────────────────────────────────────

if is_done "step24_configure_fail2ban"; then
    step_skip "step24_configure_fail2ban"
else
    step "Настройка fail2ban (защита SSH)"

    cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = 22
filter   = sshd
maxretry = 5
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban \
        || log_warn "fail2ban не запустился — проверьте: journalctl -u fail2ban"

    log_ok "fail2ban настроен"
    step_done "step24_configure_fail2ban"
fi

# ── Шаг 25: Настройка logrotate + journald ───────────────────────────────────

if is_done "step25_logrotate_journald"; then
    step_skip "step25_logrotate_journald"
else
    step "Настройка logrotate и journald"

    # logrotate для VPN-логов
    cat > /etc/logrotate.d/vpn << 'EOF'
/var/log/vpn-*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 root root
    postrotate
        systemctl reload watchdog 2>/dev/null || true
    endscript
}
EOF

    # Ограничение размера journald
    mkdir -p /etc/systemd/journald.conf.d
    cat > /etc/systemd/journald.conf.d/vpn.conf << 'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=30day
EOF

    systemctl restart systemd-journald 2>/dev/null || true

    log_ok "logrotate и journald настроены"
    step_done "step25_logrotate_journald"
fi

# ── Шаг 26: Настройка unattended-upgrades ────────────────────────────────────

if is_done "step26_unattended_upgrades"; then
    step_skip "step26_unattended_upgrades"
else
    step "Настройка автоматических security-обновлений (unattended-upgrades)"

    cat > /etc/apt/apt.conf.d/50unattended-upgrades-vpn << 'EOF'
// VPN Infrastructure — автообновление только security-пакетов
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};

// Исключение ядра (защита DKMS модулей)
Unattended-Upgrade::Package-Blacklist {
    "linux-image-*";
    "linux-headers-*";
    "linux-modules-*";
    "linux-modules-extra-*";
};

Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Mail "";
EOF

    log_ok "unattended-upgrades настроен (только security, ядро исключено)"
    step_done "step26_unattended_upgrades"
fi

# ── Шаг 27: Настройка cron-заданий ───────────────────────────────────────────

if is_done "step27_configure_cron"; then
    step_skip "step27_configure_cron"
else
    step "Настройка cron-заданий (маршруты, бэкап, DNS-прогрев)"

    # Обновление баз РКН (03:00)
    cat > /etc/cron.d/vpn-routes << 'EOF'
SHELL=/bin/bash
# Обновление баз РКН ежедневно в 03:00
0 3 * * * root flock -n /var/run/vpn-routes.lock \
    python3 /opt/vpn/scripts/update-routes.py \
    >> /var/log/vpn-routes.log 2>&1
EOF

    # Резервное копирование (04:00)
    cat > /etc/cron.d/vpn-backup << 'EOF'
SHELL=/bin/bash
# Ежедневный бэкап в 04:00
0 4 * * * root bash /opt/vpn/scripts/backup.sh >> /var/log/vpn-backup.log 2>&1
EOF

    # DNS прогрев при перезагрузке
    cat > /etc/cron.d/vpn-dns-warmup << 'EOF'
SHELL=/bin/bash
# DNS прогрев при старте (и раз в неделю вручную)
@reboot root sleep 60 && bash /opt/vpn/scripts/dns-warmup.sh \
    >> /var/log/vpn-dns-warmup.log 2>&1
EOF

    chmod 644 /etc/cron.d/vpn-routes /etc/cron.d/vpn-backup /etc/cron.d/vpn-dns-warmup

    # Создание файлов логов
    for logf in vpn-routes vpn-backup vpn-dns-warmup; do
        touch "/var/log/${logf}.log"
        chmod 640 "/var/log/${logf}.log"
    done

    log_ok "Cron-задания настроены"
    step_done "step27_configure_cron"
fi

# ── Шаг 27b: Подготовка конфигов мониторинга ─────────────────────────────────

if is_done "step27b_monitoring_configs"; then
    step_skip "step27b_monitoring_configs"
else
    step "Подготовка конфигов мониторинга (Prometheus, Alertmanager, Grafana)"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    # Создаём директории
    mkdir -p /opt/vpn/prometheus/rules /opt/vpn/alertmanager /opt/vpn/grafana/provisioning

    # Копируем конфиги из репозитория
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -d "${REPO_DIR}/home/prometheus" ]]; then
        rsync -a "${REPO_DIR}/home/prometheus/" /opt/vpn/prometheus/
        log_ok "prometheus конфиг скопирован"
    else
        log_warn "home/prometheus/ не найден — пропускаем"
    fi

    if [[ -d "${REPO_DIR}/home/alertmanager" ]]; then
        rsync -a "${REPO_DIR}/home/alertmanager/" /opt/vpn/alertmanager/
        log_ok "alertmanager конфиг скопирован"
    else
        log_warn "home/alertmanager/ не найден — пропускаем"
    fi

    if [[ -d "${REPO_DIR}/home/grafana" ]]; then
        rsync -a "${REPO_DIR}/home/grafana/" /opt/vpn/grafana/
        log_ok "grafana конфиг скопирован"
    else
        log_warn "home/grafana/ не найден — пропускаем"
    fi

    # Записываем watchdog-token для Prometheus и Alertmanager
    if [[ -n "${WATCHDOG_API_TOKEN:-}" ]]; then
        echo "${WATCHDOG_API_TOKEN}" > /opt/vpn/prometheus/watchdog-token
        echo "${WATCHDOG_API_TOKEN}" > /opt/vpn/alertmanager/watchdog-token
        chmod 644 /opt/vpn/prometheus/watchdog-token /opt/vpn/alertmanager/watchdog-token
        log_ok "watchdog-token записан"
    else
        log_warn "WATCHDOG_API_TOKEN не задан — watchdog-token пустой"
    fi

    step_done "step27b_monitoring_configs"
fi

# ── Шаг 28: Запуск Docker Compose на домашнем сервере ────────────────────────

if is_done "step28_docker_compose_home"; then
    step_skip "step28_docker_compose_home"
else
    step "Запуск Docker Compose (домашний сервер)"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    COMPOSE_FILE="/opt/vpn/docker-compose.yml"
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        log_warn "docker-compose.yml не найден в /opt/vpn/"
        log_warn "Docker-контейнеры будут запущены позже."
    else
        cd /opt/vpn

        log_info "Загрузка Docker-образов..."
        docker compose pull --quiet 2>/dev/null || true

        log_info "Запуск контейнеров..."
        docker compose up -d --remove-orphans 2>/dev/null \
            || log_warn "docker compose up завершился с предупреждениями"

        sleep 5
        echo ""
        log_info "Статус Docker-контейнеров:"
        docker compose ps 2>/dev/null || true
    fi

    log_ok "Фаза 1 (домашний сервер) завершена"
    step_done "step28_docker_compose_home"
fi

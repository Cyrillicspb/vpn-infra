#!/bin/bash
# =============================================================================
# install-home.sh — Установка компонентов на домашнем сервере
# Вызывается из setup.sh (STEP=8 bash install-home.sh)
# Шаги 9-31
# =============================================================================

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Ошибка: install-home.sh должен запускаться от root (sudo bash install-home.sh)" >&2
    exit 1
fi

# ── Константы и общие функции ─────────────────────────────────────────────────

STEP="${STEP:-8}"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${_SCRIPT_DIR}"   # сохраняем до unset — используется для tools/
# shellcheck source=common.sh
source "$_SCRIPT_DIR/common.sh"
unset _SCRIPT_DIR

# ── Загрузка переменных окружения ────────────────────────────────────────────

[[ -f "$ENV_FILE" ]] || die "Файл ${ENV_FILE} не найден. Сначала запустите setup.sh"
set -o allexport; source "$ENV_FILE"; set +o allexport

# ── Шаг 9: apt update + upgrade ──────────────────────────────────────────────

if is_done "step09_apt_update"; then
    step_skip "step09_apt_update"
else
    step "Обновление системных пакетов"
    apt_quiet "Обновление списка пакетов" update -qq
    apt_quiet "Установка обновлений системы" upgrade -y -qq
    log_ok "Система обновлена"
    step_done "step09_apt_update"
fi

# ── Шаг 10: Установка системных пакетов ──────────────────────────────────────

if is_done "step10_install_packages"; then
    step_skip "step10_install_packages"
else
    step "Установка системных пакетов"
    apt_quiet "Установка системных пакетов" install -y -qq \
        curl wget git jq rsync unzip \
        nftables dnsmasq \
        python3 python3-pip python3-venv python3-cryptography \
        wireguard-tools iproute2 \
        sqlite3 net-tools conntrack traceroute \
        fail2ban unattended-upgrades apt-transport-https \
        logrotate cron gnupg2 ca-certificates \
        sshpass autossh ncat tmux \
        uuid-runtime openssl dkms build-essential \
        iperf3
    pip3 install --quiet --break-system-packages aggregate6 2>/dev/null \
        || pip3 install --break-system-packages aggregate6
    log_ok "Системные пакеты установлены"
    step_done "step10_install_packages"
fi

# ── Шаг 11: Создание sysadmin и защита SSH ──────────────────────────────────

if is_done "step11_home_sysadmin"; then
    step_skip "step11_home_sysadmin"
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

    # docker group добавляется в шаге 15, после установки Docker

    # Перенос пароля root → sysadmin (копируем хэш из /etc/shadow)
    ROOT_HASH=$(getent shadow root | cut -d: -f2)
    if [[ -n "$ROOT_HASH" && "$ROOT_HASH" != "!" && "$ROOT_HASH" != "*" ]]; then
        usermod -p "$ROOT_HASH" sysadmin
        log_ok "Пароль sysadmin = пароль root"
        log_warn "РЕКОМЕНДАЦИЯ: смените пароль sysadmin командой: passwd sysadmin"
        log_warn "Пароль от VPS-провайдера может быть небезопасным для постоянного использования"
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

    # Keepalive: сервер пингует клиента каждые 60 сек, 10 попыток = 10 мин терпения.
    # Без этого ТСПУ/NAT обрывает долгие SSH-сессии (docker build, apt upgrade).
    sed -i 's/^#*ClientAliveInterval.*/ClientAliveInterval 60/' /etc/ssh/sshd_config
    grep -q '^ClientAliveInterval' /etc/ssh/sshd_config \
        || echo 'ClientAliveInterval 60' >> /etc/ssh/sshd_config
    sed -i 's/^#*ClientAliveCountMax.*/ClientAliveCountMax 10/' /etc/ssh/sshd_config
    grep -q '^ClientAliveCountMax' /etc/ssh/sshd_config \
        || echo 'ClientAliveCountMax 10' >> /etc/ssh/sshd_config

    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    log_ok "SSH: PermitRootLogin no (вход по паролю для sysadmin сохранён)"
    log_warn "Для входа: ssh sysadmin@${HOME_SERVER_IP:-$(hostname -I | awk '{print $1}')}"

    # Tmux авто-старт при SSH-подключении (для эксплуатации и долгих операций)
    TMUX_SNIPPET='
# Авто-вход в tmux при SSH-подключении
if [[ -n "$SSH_CONNECTION" ]] && [[ -z "$TMUX" ]] && command -v tmux &>/dev/null; then
    tmux attach-session -t main 2>/dev/null || tmux new-session -s main
fi'
    for _rcfile in /home/sysadmin/.bashrc /root/.bashrc; do
        if ! grep -q 'tmux attach-session -t main' "$_rcfile" 2>/dev/null; then
            echo "$TMUX_SNIPPET" >> "$_rcfile"
        fi
    done
    log_ok "Tmux авто-старт добавлен в .bashrc (sysadmin + root)"

    step_done "step11_home_sysadmin"
fi

# ── Шаг 12: Отключение IPv6 ──────────────────────────────────────────────────

if is_done "step12_disable_ipv6"; then
    step_skip "step12_disable_ipv6"
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
    step_done "step12_disable_ipv6"
fi

# ── Шаг 13: Настройка ядра (BBR + IP forwarding + rp_filter) ─────────────────

if is_done "step13_kernel_tuning"; then
    step_skip "step13_kernel_tuning"
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

# Conntrack: дефолт Ubuntu ~65536 недостаточен при интенсивном docker pull.
# При overflow ядро дропает входящие пакеты до nftables — SSH/ping пропадают.
net.netfilter.nf_conntrack_max = 262144
net.netfilter.nf_conntrack_tcp_timeout_established = 300
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 30
EOF
    sysctl --system 2>/dev/null || sysctl -p /etc/sysctl.d/99-vpn.conf 2>/dev/null || true
    log_ok "Параметры ядра настроены"
    step_done "step13_kernel_tuning"
fi

# ── Шаг 14: Фиксация версии ядра (предотвращение автообновления) ─────────────

if is_done "step14_pin_kernel"; then
    step_skip "step14_pin_kernel"
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
    step_done "step14_pin_kernel"
fi

# ── Шаг 15: Установка Docker CE ──────────────────────────────────────────────

if is_done "step15_install_docker"; then
    step_skip "step15_install_docker"
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

        apt_quiet "Обновление APT для Docker" update -qq
        apt_quiet "Установка Docker CE" install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-compose-plugin
        log_ok "Docker CE установлен"
    else
        log_info "Docker уже установлен: $(docker --version)"
    fi

    # На Ubuntu 24.04 iptables = iptables-nft (nf_tables backend).
    # nft flush ruleset уничтожает Docker-цепочки → docker compose up падает.
    # Переключаем на iptables-legacy до запуска Docker.
    update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null || true
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null || true

    # Конфигурация Docker daemon
    # Намеренно: контейнеры резолвят через публичные DNS (8.8.8.8/1.1.1.1), минуя dnsmasq.
    # Это предотвращает попадание DNS-запросов бота (например /check domain) в blocked_dynamic
    # и нежелательную маршрутизацию контейнерного трафика через VPN.
    cat > /etc/docker/daemon.json << 'EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    },
    "dns": ["77.88.8.8", "77.88.8.1"],
    "ipv6": false,
    "max-concurrent-downloads": 2,
    "registry-mirrors": [
        "https://dockerhub.timeweb.cloud"
    ]
}
EOF

    systemctl enable docker
    systemctl restart docker
    # Добавляем sysadmin в docker group здесь — группа уже существует
    usermod -aG docker sysadmin 2>/dev/null || true
    log_ok "Docker настроен и запущен"
    step_done "step15_install_docker"
fi

# ── Шаг 16: Установка AmneziaWG ──────────────────────────────────────────────

if is_done "step16_install_amneziawg"; then
    step_skip "step16_install_amneziawg"
else
    step "Установка AmneziaWG (DKMS модуль)"
    log_info "AmneziaWG — модификация WireGuard с дополнительной обфускацией (junk packets)."
    log_info "Защищает от DPI-детекции WireGuard по сигнатурам пакетов."

    AWG_INSTALLED=0

    # Чистим все старые записи amnezia PPA — Ubuntu 24.04 выдаёт ошибку
    # "конфликтующие значения Signed-By" если PPA добавлен дважды разными способами.
    rm -f /etc/apt/sources.list.d/amnezia*.list \
          /etc/apt/sources.list.d/amnezia*.sources \
          /etc/apt/trusted.gpg.d/amnezia*.gpg \
          /usr/share/keyrings/amnezia-ppa.gpg 2>/dev/null || true

    # Ключ вшит в скрипт — keyserver.ubuntu.com заблокирован в России.
    # Публичный ключ Launchpad PPA for Iurii Egorov (fingerprint 2EBB9386EA7B5F00).
    log_info "Устанавливаем PGP-ключ AmneziaWG PPA (bundled)..."
    cat << 'AMNEZIA_KEY_EOF' | gpg --dearmor -o /usr/share/keyrings/amnezia-ppa.gpg 2>/dev/null
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGV0UhsBEAC33rMndHSN/k+u7gcZbh9/FjgYfGltQAtVe2QDxzn7UV+k/ChX
OrYRw6Izw/DrhaapkNCThK2jwJE64e0NjboLH7UrrmSJLXMfOlDFbyGJVRA+1sTB
lo7kKHY0xiZ1CHDzjKNV3czbesu80A9nuTZYyWHEn9ax6wsqKG3N8SvzQkUrIOVD
2wZjh0p273CCEGkBnax1ghAV3MF8OrsPU6FRJ+ZakzKbu54g68xoV+2813YECme0
JKsWfUUe/1uEJOXCvuACURSxnYr0sihJd8QI/jHSGlfeq72e5MflFEOrnu5xaDSJ
r2W5lvUetG7EGSxtNKd7Jm/KhUV04g7arA0qydRjRToW3QqyzG7VB2nXKz3AOBYN
earWAuBcTkfPvRVchxbjiYonKZA5tIlVrpawMZsdxKvYwl6LVnpBcccFWPhpudfy
4TpCqCxRoAanOCvSirI3/y7TcZMBw643SaxXi1ifGeg6eyMzrLtP3CeonKBHGzrt
1eeKGtEw/PFN4RmwpBePxi+uj0CoTD6zjCQa3c8EeB4Qz7tt6PnpibxdtZE8sBdd
51wSA/fPGi2tFph8IVAsws7oxcQxZYl8CyncKDLcoR4dxVHYdFEDDf1GjRjoQ3Ai
nD7fxD5qYzExe50DBVpuUbWcAiGICNxfvzQtUSRRtMoSHDcvzsy03KC6VwARAQAB
tB5MYXVuY2hwYWQgUFBBIGZvciBJdXJpaSBFZ29yb3aJAk4EEwEKADgWIQR1yd1y
x5mHDjEFQuJBZvLCVykIKAUCZXRSGwIbAwULCQgHAgYVCgkICwIEFgIDAQIeAQIX
gAAKCRBBZvLCVykIKBu5D/9akmHCHlUqm2RTTBeTMbLNGc0l6YugpPaCM6vz0O9k
BFP5PfRaNSRzyF7wHFHNY3JUHcor28my1fD8AE4+C3PwXz8tVYLh57UUsp4wjqHY
+MTl/1ngDViPGD3PRjB8ZlO+19yerfplZv1Jaw7FZZv2BZOAXb+ddqUG4EmlzOnC
EhcSDdFrzEBB3RGthjIb3QkKWKGbELDiMfogmsO9BE139Raiw23blagDrbnWsG4j
ReZeu3atjG6AW8eL7m+i7bKKshD2CYVMznI5cYGLMKo9w7sb33uylPj1Vx9O7joP
2GFf2rTpCY8wgzk7i1RqsipJ80u1/DY91Xdizv3f2BBe6UY7qHKoK00O11J0y8yU
is2Asycy33Wy51pf6rCFUBLQu+c1fEypHF6jqANmQwaH7pPBliy4gGWvrVggzV4m
xv7SnRiMi4PFyVwjKWm8dmuMxi/B9s++VG/ed+5aYgJYL58MohG3MUI/L58eitSC
DDcQ1iAnBmawnGMKPqzMgRFB3OU3wDwfh7LNVvQqWpQ4q7pr4Cq1CvZvGoggXWDo
1/vylPsRmiiuNetfsoVYmrkgtj1om07m5Xp1v4SyXJH11c3dc/xfMmn/4RlMWIpq
86IsOjpr3avsw3FVUNCgD5Wf5+rHG+7gNmM6Cm/F8MDfAnnRmsw4h6hgvcJNQT5D
ig==
=MT+d
-----END PGP PUBLIC KEY BLOCK-----
AMNEZIA_KEY_EOF

    if [[ -s /usr/share/keyrings/amnezia-ppa.gpg ]]; then
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/amnezia-ppa.gpg] \
https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu noble main" \
            > /etc/apt/sources.list.d/amnezia.list
        apt_quiet "Обновление APT для AmneziaWG" update -qq || true
        if apt_quiet "Установка AmneziaWG" install -y -qq \
            amneziawg-dkms amneziawg-tools; then
            AWG_INSTALLED=1
            log_ok "AmneziaWG установлен из PPA"
        fi
    fi

    if [[ $AWG_INSTALLED -eq 0 ]]; then
        log_warn "AmneziaWG не удалось установить автоматически."
        log_warn "Установите вручную: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
        log_warn "Продолжаем установку — AmneziaWG можно добавить позже."
    fi

    step_done "step16_install_amneziawg"
fi

# ── Шаг 17: Установка Hysteria2 ──────────────────────────────────────────────

if is_done "step17_install_hysteria2"; then
    step_skip "step17_install_hysteria2"
else
    step "Установка Hysteria2 (бинарник)"

    _HYSTERIA_FALLBACK="v2.7.1"
    _ARCH="$(uname -m)"; [[ "$_ARCH" == "aarch64" ]] && _ARCH="arm64" || _ARCH="amd64"
    _BUNDLED="$REPO_DIR/tools/hysteria2-linux-${_ARCH}"

    if [[ -f "$_BUNDLED" ]]; then
        # Версия из бандла — читаем из бинарника
        HYSTERIA_VERSION="bundled"
        log_info "Использую бандл hysteria2 (${_ARCH})..."
        cp "$_BUNDLED" /usr/local/bin/hysteria
    else
        # Запрашиваем актуальную версию через GitHub API — не зависим от хардкода
        HYSTERIA_VERSION=$(curl -sSfL --max-time 10 \
            https://api.github.com/repos/apernet/hysteria/releases/latest \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].replace('app/',''))" \
            2>/dev/null) || HYSTERIA_VERSION="$_HYSTERIA_FALLBACK"
        [[ -z "$HYSTERIA_VERSION" ]] && HYSTERIA_VERSION="$_HYSTERIA_FALLBACK"
        log_info "Загрузка Hysteria2 ${HYSTERIA_VERSION} с GitHub..."
        HYSTERIA_URL="https://github.com/apernet/hysteria/releases/download/app%2F${HYSTERIA_VERSION}/hysteria-linux-${_ARCH}"
        curl -fsSL --progress-bar "$HYSTERIA_URL" -o /usr/local/bin/hysteria \
            || die "Не удалось загрузить Hysteria2 с ${HYSTERIA_URL}"
    fi
    chmod +x /usr/local/bin/hysteria

    # Проверка запуска
    /usr/local/bin/hysteria version 2>/dev/null \
        || die "Hysteria2 не запускается. Проверьте бинарник."

    mkdir -p /etc/hysteria
    log_ok "Hysteria2 ${HYSTERIA_VERSION} установлен"
    step_done "step17_install_hysteria2"
fi

# ── Шаг 18: Установка tun2socks ──────────────────────────────────────────────

if is_done "step18_install_tun2socks"; then
    step_skip "step18_install_tun2socks"
else
    step "Установка tun2socks"

    _TUN2SOCKS_FALLBACK="v2.5.2"
    _ARCH="$(uname -m)"; [[ "$_ARCH" == "aarch64" ]] && _ARCH="arm64" || _ARCH="amd64"
    _BUNDLED="$REPO_DIR/tools/tun2socks-linux-${_ARCH}"

    if [[ -f "$_BUNDLED" ]]; then
        TUN2SOCKS_VER="bundled"
        log_info "Использую бандл tun2socks (${_ARCH})..."
        cp "$_BUNDLED" /usr/local/bin/tun2socks
    else
        TUN2SOCKS_VER=$(curl -sSfL --max-time 10 \
            https://api.github.com/repos/xjasonlyu/tun2socks/releases/latest \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" \
            2>/dev/null) || TUN2SOCKS_VER="$_TUN2SOCKS_FALLBACK"
        [[ -z "$TUN2SOCKS_VER" ]] && TUN2SOCKS_VER="$_TUN2SOCKS_FALLBACK"
        log_info "Загрузка tun2socks ${TUN2SOCKS_VER} с GitHub..."
        TUN2SOCKS_URL="https://github.com/xjasonlyu/tun2socks/releases/download/${TUN2SOCKS_VER}/tun2socks-linux-${_ARCH}.zip"
        curl -fsSL "$TUN2SOCKS_URL" -o /tmp/tun2socks.zip \
            || die "Не удалось загрузить tun2socks"
        cd /tmp
        unzip -qo tun2socks.zip "tun2socks-linux-${_ARCH}" 2>/dev/null \
            || unzip -qo tun2socks.zip 2>/dev/null \
            || die "Не удалось распаковать tun2socks.zip"
        if [[ -f /tmp/tun2socks-linux-${_ARCH} ]]; then
            mv /tmp/tun2socks-linux-${_ARCH} /usr/local/bin/tun2socks
        elif [[ -f /tmp/tun2socks ]]; then
            mv /tmp/tun2socks /usr/local/bin/tun2socks
        else
            die "Бинарник tun2socks не найден после распаковки"
        fi
        rm -f /tmp/tun2socks.zip
    fi

    chmod +x /usr/local/bin/tun2socks

    log_ok "tun2socks ${TUN2SOCKS_VER} установлен"
    step_done "step18_install_tun2socks"
fi

# ── Шаг 19: Генерация ключей REALITY x25519 через Docker ─────────────────────

if is_done "step19_generate_reality_keys"; then
    step_skip "step19_generate_reality_keys"
else
    step "Генерация REALITY x25519 ключей (через Docker/xray)"
    log_info "x25519 keypair нужен для VLESS+XHTTP+REALITY — имитация TLS к легитимному домену."
    log_info "Генерируем keypair для стека reality-xhttp (cdn.jsdelivr.net)."

    # Загружаем актуальные переменные
    set -o allexport; source "$ENV_FILE"; set +o allexport

    XRAY_IMAGE="teddysun/xray:1.8.11"

    # Убедимся что Docker доступен
    if ! command -v docker &>/dev/null; then
        log_warn "Docker недоступен — пропускаем генерацию REALITY ключей."
        log_warn "Запустите шаг 19 вручную после установки Docker."
    else
        log_info "Загрузка образа ${XRAY_IMAGE}..."
        docker pull "${XRAY_IMAGE}" --quiet 2>/dev/null || true

        if [[ -z "${XRAY_XHTTP_PUBLIC_KEY:-}" ]]; then
            XRAY_GRPC_KEYS=$(docker run --rm "${XRAY_IMAGE}" xray x25519 2>/dev/null) \
                || die "Не удалось запустить xray x25519 (XHTTP) в Docker"
            XRAY_XHTTP_PRIVATE_KEY=$(echo "$XRAY_GRPC_KEYS" | grep "Private key:" | awk '{print $NF}')
            XRAY_XHTTP_PUBLIC_KEY=$(echo "$XRAY_GRPC_KEYS" | grep "Public key:" | awk '{print $NF}')
            [[ -n "$XRAY_XHTTP_PRIVATE_KEY" ]] || die "Не удалось извлечь Private key из xray x25519 XHTTP (неожиданный формат вывода)"
            [[ -n "$XRAY_XHTTP_PUBLIC_KEY" ]]  || die "Не удалось извлечь Public key из xray x25519 XHTTP (неожиданный формат вывода)"
            env_set "XRAY_XHTTP_PRIVATE_KEY" "$XRAY_XHTTP_PRIVATE_KEY"
            env_set "XRAY_XHTTP_PUBLIC_KEY"  "$XRAY_XHTTP_PUBLIC_KEY"
            env_set "XRAY_GRPC_PRIVATE_KEY"  "$XRAY_XHTTP_PRIVATE_KEY"
            env_set "XRAY_GRPC_PUBLIC_KEY"   "$XRAY_XHTTP_PUBLIC_KEY"
            log_ok "Ключи REALITY XHTTP (cdn.jsdelivr.net) сгенерированы"
        else
            log_info "XRAY_XHTTP_PUBLIC_KEY уже существует"
        fi

        chmod 600 "$ENV_FILE"
        set -o allexport; source "$ENV_FILE"; set +o allexport
    fi

    step_done "step19_generate_reality_keys"
fi

# ── Шаг 20: Настройка nftables ───────────────────────────────────────────────

if is_done "step20_configure_nftables"; then
    step_skip "step20_configure_nftables"
else
    step "Настройка nftables (правила + nft sets)"
    log_info "nftables: таблица inet vpn с двумя sets:"
    log_info "  blocked_static  — базы РКН, обновляется ежедневно в 03:00"
    log_info "  blocked_dynamic — IP из DNS-ответов (dnsmasq nftset=), TTL 24h"
    log_info "Kill switch: DROP если dst в blocked_sets и oifname != tun* (VPN не поднят)"

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
# ⚠️ flush ruleset уничтожает ВСЕ таблицы включая Docker.
# Безопасно ТОЛЬКО потому что Docker использует iptables-legacy (шаг 15).
# При переходе Docker на nftables-backend — заменить на:
#   delete table inet vpn (если существует)
#   add table inet vpn
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
        # AR1: блокировать QUIC (UDP 443) для dpi_direct — принудить браузер к TCP
        # TCP обрабатывается nfqws, QUIC нет → шейпинг без bypass
        iifname { "wg0", "wg1" } ip daddr @dpi_direct udp dport 443 drop
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
        ct state invalid drop
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

# IPv6 отключён в sysctl, но добавляем DROP как defense-in-depth
table ip6 filter {
    chain input {
        type filter hook input priority 0; policy drop;
    }
    chain forward {
        type filter hook forward priority 0; policy drop;
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

    # Gateway Mode: переге╢нерировать конфиг с LAN-правилами
    if [[ "${SERVER_MODE:-hosted}" == "gateway" ]]; then
        log_info "Gateway Mode: генерация nftables конфига с LAN-правилами..."
        GENERATE_NFT="/opt/vpn/scripts/generate-nftables.sh"
        if [[ ! -f "$GENERATE_NFT" ]]; then
            # Копируем из репозитория (step29 ещё не выполнен)
            REPO_DIR_NFT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
            [[ -f "${REPO_DIR_NFT}/home/scripts/generate-nftables.sh" ]] && \
                cp "${REPO_DIR_NFT}/home/scripts/generate-nftables.sh" "$GENERATE_NFT" && \
                chmod +x "$GENERATE_NFT"
        fi
        if [[ -x "$GENERATE_NFT" ]]; then
            bash "$GENERATE_NFT" || log_warn "generate-nftables.sh завершился с ошибкой — применён базовый конфиг"
            log_ok "Gateway nftables применён"
        else
            log_warn "generate-nftables.sh не найден — применён базовый конфиг"
        fi
    fi

    systemctl enable nftables
    systemctl restart nftables \
        || log_warn "nftables restart завершился с ошибкой — проверьте конфиг: nft -c -f /etc/nftables.conf"

    log_ok "nftables настроен"
    step_done "step20_configure_nftables"
fi

# ── Шаг 21: Проверка firewall — nmap + ss ────────────────────────────────────
# Firewall — единственная защита. Все порты кроме явно разрешённых DROP.
# Этот шаг убеждается что нет неожиданно открытых сервисов.

if is_done "step21_verify_firewall"; then
    step_skip "step21_verify_firewall"
else
    step "Проверка firewall (nmap + ss)"

    # Установить nmap если нет (не добавляем в шаг 10 — нужен только здесь)
    if ! command -v nmap &>/dev/null; then
        log_info "Устанавливаем nmap..."
        apt_quiet "Установка nmap" install -y -qq nmap
    fi

    # Тестируем через LAN IP (не loopback) — пакеты проходят через nftables INPUT
    if [[ -f "$ENV_FILE" ]]; then
        set -o allexport; source "$ENV_FILE" || true; set +o allexport
    fi
    LAN_IP="${HOME_SERVER_IP:-}"
    if [[ -z "$LAN_IP" ]]; then
        LAN_IP=$(ip -4 addr show "${NET_INTERFACE:-eth0}" 2>/dev/null \
            | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1 || true)
    fi
    LAN_IP="${LAN_IP:-127.0.0.1}"
    log_info "LAN IP для сканирования: ${LAN_IP}"

    log_info "nmap TCP (top-100 портов) → $LAN_IP ..."
    # --open: только открытые; -oG: grepable; -T5: максимально быстро; timeout 20s
    open_tcp=$(timeout 20 nmap -sT --top-ports 100 --open -T5 --max-retries 1 -oG - "$LAN_IP" 2>/dev/null \
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

    step_done "step21_verify_firewall"
fi

# ── Шаг 22: Создание конфигов WireGuard-интерфейсов ──────────────────────────

if is_done "step22_wireguard_configs"; then
    step_skip "step22_wireguard_configs"
else
    step "Создание конфигов WireGuard-интерфейсов (wg0 AWG + wg1 WG)"
    log_info "wg0 (AmneziaWG): клиенты AWG, 10.177.1.0/24, порт 51820"
    log_info "wg1 (WireGuard): клиенты WG,  10.177.3.0/24, порт 51821"
    log_info "Пиры добавляются через Telegram-бот (/adddevice) или watchdog API."

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
    # Примечание: wg-quick@wg0 и wg1 запускаются после Tier-2 в phase3 (шаг 46)
    step_done "step22_wireguard_configs"
fi

# ── Шаг 23: Настройка dnsmasq ────────────────────────────────────────────────

if is_done "step23_configure_dnsmasq"; then
    step_skip "step23_configure_dnsmasq"
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

# Яндекс DNS — работает из России напрямую (1.1.1.1/8.8.8.8 заблокированы ISP)
server=77.88.8.8
server=77.88.8.1
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

    # Gateway Mode: добавить прослушивание на LAN-интерфейсе
    if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${LAN_IFACE:-}" ]]; then
        mkdir -p /etc/dnsmasq.d
        cat > /etc/dnsmasq.d/gateway.conf << EOF
# Gateway Mode: DNS для LAN-устройств
# Генерируется автоматически из SERVER_MODE=gateway
interface=${LAN_IFACE}
EOF
        log_ok "dnsmasq: Gateway mode — слушает на ${LAN_IFACE}"
    fi

    systemctl enable dnsmasq
    if ! systemctl restart dnsmasq 2>/dev/null; then
        log_warn "dnsmasq не запустился — используем Яндекс DNS как временный"
        # Обеспечить DNS на время установки (dnsmasq поднимется после wg-интерфейсов)
        printf "nameserver 77.88.8.8\nnameserver 77.88.8.1\n" > /etc/resolv.conf
        log_warn "dnsmasq не запустился — проверьте: journalctl -u dnsmasq"
    fi

    log_ok "dnsmasq настроен"
    step_done "step23_configure_dnsmasq"
fi

# ── Шаг 24: Настройка policy routing и systemd-юнитов ────────────────────────

if is_done "step24_policy_routing_units"; then
    step_skip "step24_policy_routing_units"
else
    step "Настройка policy routing и systemd-юнитов"

    # Скрипт policy routing должен быть в /opt/vpn/scripts/ — туда указывает ExecStart.
    # step29 копирует все скрипты позже, поэтому копируем явно уже здесь.
    STEP24_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mkdir -p /opt/vpn/scripts
    if [[ ! -f /opt/vpn/scripts/vpn-policy-routing.sh ]]; then
        SRC="${STEP24_REPO}/home/scripts/vpn-policy-routing.sh"
        [[ -f "$SRC" ]] && cp "$SRC" /opt/vpn/scripts/vpn-policy-routing.sh
    fi
    chmod +x /opt/vpn/scripts/vpn-policy-routing.sh 2>/dev/null || true

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
After=network-online.target docker.service dnsmasq.service
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
WorkingDirectory=/opt/vpn/watchdog
EnvironmentFile=/opt/vpn/.env
ExecStart=/opt/vpn/watchdog/venv/bin/python3 /opt/vpn/watchdog/watchdog.py
# KillMode=process: не убивает tun2socks и nfqws при рестарте watchdog
KillMode=process
Restart=always
RestartSec=5
WatchdogSec=30
StandardOutput=null
StandardError=append:/var/log/vpn-watchdog.log

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
    /opt/vpn/scripts/tg-send.sh \
        "$(. /opt/vpn/.env 2>/dev/null; echo "${TELEGRAM_ADMIN_CHAT_ID:-}")" \
        "WATCHDOG МЁРТВ" > /dev/null 2>&1 || true
EOF
    fi
    chmod 644 /etc/cron.d/vpn-watchdog-failsafe

    systemctl daemon-reload
    systemctl enable vpn-routes vpn-sets-restore 2>/dev/null || true
    systemctl start vpn-routes 2>/dev/null || true

    log_ok "Policy routing и systemd-юниты настроены"
    step_done "step24_policy_routing_units"
fi

# ── Шаг 25: Установка Watchdog Python venv ───────────────────────────────────

if is_done "step25_watchdog_venv"; then
    step_skip "step25_watchdog_venv"
else
    step "Создание Python venv для Watchdog"

    WATCHDOG_DIR="/opt/vpn/watchdog"
    mkdir -p "$WATCHDOG_DIR"

    # Копируем файлы watchdog из репозитория
    for src in /opt/vpn/home/watchdog /opt/vpn/watchdog-src; do
        if [[ -f "${src}/watchdog.py" ]]; then
            cp "${src}/watchdog.py" "${WATCHDOG_DIR}/watchdog.py"
            [[ -f "${src}/requirements.txt" ]] && \
                cp "${src}/requirements.txt" "${WATCHDOG_DIR}/requirements.txt"
            [[ -d "${src}/wheels" ]] && \
                mkdir -p "${WATCHDOG_DIR}/wheels" && cp -r "${src}/wheels/." "${WATCHDOG_DIR}/wheels/"
            [[ -d "${src}/plugins" ]] && \
                cp -r "${src}/plugins/." "${WATCHDOG_DIR}/plugins/"
            log_ok "watchdog.py скопирован из ${src}"
            break
        fi
    done
    [[ -f "${WATCHDOG_DIR}/watchdog.py" ]] || \
        log_warn "watchdog.py не найден в репозитории — сервис не запустится"

    if [[ ! -d "${WATCHDOG_DIR}/venv" ]]; then
        python3 -m venv "${WATCHDOG_DIR}/venv"
        log_ok "Python venv создан в ${WATCHDOG_DIR}/venv"
    else
        log_info "venv уже существует"
    fi

    # Сбрасываем stale state после переустановки, чтобы watchdog не стартовал
    # с несуществующим стеком от предыдущего прогона.
    _default_watchdog_stack="hysteria2"
    if [[ "${USE_CLOUDFLARE:-n}" == "y" && -n "${CF_CDN_HOSTNAME:-}" ]]; then
        _default_watchdog_stack="cloudflare-cdn"
    fi
    if [[ -f "${WATCHDOG_DIR}/state.json" ]]; then
        python3 - <<PY
import json
from pathlib import Path

state_path = Path("${WATCHDOG_DIR}/state.json")
data = json.loads(state_path.read_text())
data["active_stack"] = "${_default_watchdog_stack}"
data["primary_stack"] = "${_default_watchdog_stack}"
data["degraded_mode"] = False
data["is_first_run"] = True
state_path.write_text(json.dumps(data, indent=2))
PY
        log_ok "watchdog state.json нормализован (${_default_watchdog_stack}, first_run=true)"
    fi

    # Установка зависимостей
    # Предпочитаем локальный wheelhouse. Сеть используем только как fallback.
    _pip_install() {
        local pip="${WATCHDOG_DIR}/venv/bin/pip"
        local wheel_dir="${WATCHDOG_DIR}/wheels"
        if find "$wheel_dir" -maxdepth 1 -type f -name '*.whl' 2>/dev/null | grep -q .; then
            log_info "pip install из локального wheelhouse (${wheel_dir}) ..."
            "$pip" install -q --no-cache-dir --no-index --find-links "$wheel_dir" "$@" && return 0
            log_warn "Локальный wheelhouse неполон — пробуем зеркала"
        fi

        local args=(-q --no-cache-dir --timeout 120 --retries 2 "$@")
        local mirrors=(
            "https://pypi.org/simple/"
            "https://mirror.yandex.ru/mirrors/pypi/simple/"
            "https://pypi.tuna.tsinghua.edu.cn/simple/"
        )
        for mirror in "${mirrors[@]}"; do
            log_info "pip install через $mirror ..."
            if "$pip" install "${args[@]}" --index-url "$mirror" 2>/dev/null; then
                return 0
            fi
        done
        log_warn "Все зеркала PyPI недоступны — пробуем без зеркала"
        "$pip" install "${args[@]}" || return 1
    }

    if [[ -f "${WATCHDOG_DIR}/requirements.txt" ]]; then
        _pip_install -r "${WATCHDOG_DIR}/requirements.txt" \
            || log_warn "pip install не завершился — watchdog может не запуститься, повторите позже"
        log_ok "Зависимости установлены из requirements.txt"
    else
        log_warn "requirements.txt не найден — устанавливаем базовые зависимости"
        _pip_install aiohttp fastapi uvicorn python-telegram-bot python-dotenv \
            psutil requests \
            || log_warn "pip install не завершился — watchdog может не запуститься"
    fi

    # Включение watchdog.service (запускается в phase3 после связки)
    systemctl enable watchdog 2>/dev/null || true

    log_ok "Watchdog Python venv готов"
    step_done "step25_watchdog_venv"
fi

# ── Шаг 26: Настройка fail2ban ───────────────────────────────────────────────

if is_done "step26_configure_fail2ban"; then
    step_skip "step26_configure_fail2ban"
else
    step "Настройка fail2ban (защита SSH)"

    cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 86400
findtime = 300
maxretry = 3
backend  = systemd

[sshd]
enabled  = true
port     = 22
filter   = sshd
mode     = aggressive
maxretry = 3
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban \
        || log_warn "fail2ban не запустился — проверьте: journalctl -u fail2ban"

    log_ok "fail2ban настроен"
    step_done "step26_configure_fail2ban"
fi

# Gateway Mode: добавить домашнюю сеть в fail2ban ignoreip
if [[ "${SERVER_MODE:-hosted}" == "gateway" && -n "${HOME_SUBNET:-}" ]]; then
    F2B_JAIL=/etc/fail2ban/jail.local
    if grep -q "ignoreip" "$F2B_JAIL" 2>/dev/null; then
        # ignoreip уже есть — добавить HOME_SUBNET если его нет
        if ! grep -q "$HOME_SUBNET" "$F2B_JAIL"; then
            sed -i "s|^\(ignoreip\s*=.*\)|\1 $HOME_SUBNET|" "$F2B_JAIL"
            log_ok "fail2ban: добавлена домашняя сеть $HOME_SUBNET в ignoreip"
            systemctl restart fail2ban \
                || log_warn "fail2ban не перезапустился после добавления ignoreip"
        fi
    else
        # ignoreip нет — добавить в секцию [DEFAULT]
        sed -i "/^\[DEFAULT\]/a ignoreip = 127.0.0.1/8 ::1 $HOME_SUBNET" "$F2B_JAIL"
        log_ok "fail2ban: создан ignoreip с домашней сетью $HOME_SUBNET"
        systemctl restart fail2ban \
            || log_warn "fail2ban не перезапустился после добавления ignoreip"
    fi
    chmod 644 "$F2B_JAIL"
fi

# ── Шаг 27: Настройка logrotate + journald ───────────────────────────────────

if is_done "step27_logrotate_journald"; then
    step_skip "step27_logrotate_journald"
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
    step_done "step27_logrotate_journald"
fi

# ── Шаг 28: Настройка unattended-upgrades ────────────────────────────────────

if is_done "step28_unattended_upgrades"; then
    step_skip "step28_unattended_upgrades"
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
    step_done "step28_unattended_upgrades"
fi

# ── Шаг 29: Настройка cron-заданий ───────────────────────────────────────────

if is_done "step29_configure_cron"; then
    step_skip "step29_configure_cron"
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

    # Gateway Mode: обновление IP роутера в nft set (каждые 5 минут)
    if [[ "${SERVER_MODE:-hosted}" == "gateway" ]]; then
        cat > /etc/cron.d/vpn-router-ip << 'EOF'
SHELL=/bin/bash
# Gateway Mode: обновление внешнего IP роутера в nft set router_external_ips
*/5 * * * * root bash /opt/vpn/scripts/update-router-ip.sh >> /var/log/vpn-router-ip.log 2>&1
EOF
        chmod 644 /etc/cron.d/vpn-router-ip
        touch /var/log/vpn-router-ip.log
        chmod 640 /var/log/vpn-router-ip.log
        log_ok "Gateway Mode: cron для обновления IP роутера (каждые 5 минут)"
    fi

    # Создание файлов логов
    for logf in vpn-routes vpn-backup vpn-dns-warmup; do
        touch "/var/log/${logf}.log"
        chmod 640 "/var/log/${logf}.log"
    done

    # Копируем все скрипты из home/scripts/ в /opt/vpn/scripts/
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mkdir -p /opt/vpn/scripts
    if [[ -d "${REPO_DIR}/home/scripts" ]]; then
        cp "${REPO_DIR}/home/scripts/"*.sh /opt/vpn/scripts/ 2>/dev/null || true
        cp "${REPO_DIR}/home/scripts/"*.py /opt/vpn/scripts/ 2>/dev/null || true
        chmod +x /opt/vpn/scripts/*.sh 2>/dev/null || true
        log_ok "Скрипты скопированы в /opt/vpn/scripts/"
    fi

    # Первоначальная загрузка баз РКН (не ждать до 03:00)
    log_info "Первоначальная загрузка баз маршрутов..."
    if python3 /opt/vpn/scripts/update-routes.py >> /var/log/vpn-routes.log 2>&1; then
        log_ok "Базы маршрутов загружены ($(wc -l < /etc/vpn-routes/combined.cidr) записей)"
    else
        log_warn "Загрузка баз маршрутов завершилась с ошибкой — проверьте /var/log/vpn-routes.log"
    fi

    # Прогрев DNS-кэша (заполнить blocked_dynamic через dnsmasq)
    if [[ -x /opt/vpn/scripts/dns-warmup.sh ]]; then
        log_info "Прогрев DNS-кэша..."
        if bash /opt/vpn/scripts/dns-warmup.sh >> /var/log/vpn-dns-warmup.log 2>&1; then
            log_ok "DNS-кэш прогрет"
        else
            log_warn "Прогрев DNS завершился с ошибкой — проверьте /var/log/vpn-dns-warmup.log"
        fi
    fi

    rm -f /etc/cron.d/vpn-docker-phase2 /var/log/vpn-docker-phase2.log 2>/dev/null || true
    log_ok "Отложенная фаза мониторинга отключена: monitoring поднимается сразу"

    log_ok "Cron-задания настроены"
    step_done "step29_configure_cron"
fi

# ── Шаг 30: Подготовка конфигов мониторинга ─────────────────────────────────

if is_done "step30_monitoring_configs"; then
    step_skip "step30_monitoring_configs"
else
    step "Подготовка конфигов мониторинга (Prometheus, Alertmanager, Grafana)"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    # Создаём директории
    mkdir -p /opt/vpn/prometheus/rules /opt/vpn/alertmanager /opt/vpn/grafana/provisioning

    # Копируем конфиги из репозитория
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ -d "${REPO_DIR}/home/prometheus" ]] || die "home/prometheus/ не найден"
    rsync -a "${REPO_DIR}/home/prometheus/" /opt/vpn/prometheus/
    log_ok "prometheus конфиг скопирован"

    [[ -d "${REPO_DIR}/home/alertmanager" ]] || die "home/alertmanager/ не найден"
    rsync -a "${REPO_DIR}/home/alertmanager/" /opt/vpn/alertmanager/
    log_ok "alertmanager конфиг скопирован"

    [[ -d "${REPO_DIR}/home/grafana" ]] || die "home/grafana/ не найден"
    rsync -a "${REPO_DIR}/home/grafana/" /opt/vpn/grafana/
    log_ok "grafana конфиг скопирован"

    # Записываем watchdog-token для Prometheus и Alertmanager
    if [[ -n "${WATCHDOG_API_TOKEN:-}" ]]; then
        echo "${WATCHDOG_API_TOKEN}" > /opt/vpn/prometheus/watchdog-token
        echo "${WATCHDOG_API_TOKEN}" > /opt/vpn/alertmanager/watchdog-token
        chmod 644 /opt/vpn/prometheus/watchdog-token /opt/vpn/alertmanager/watchdog-token
        log_ok "watchdog-token записан"
    else
        die "WATCHDOG_API_TOKEN не задан — monitoring не сможет опрашивать watchdog"
    fi

    step_done "step30_monitoring_configs"
fi

# ── Шаг 31: Запуск Docker Compose на домашнем сервере ────────────────────────

if is_done "step31_docker_compose_home"; then
    step_skip "step31_docker_compose_home"
else
    step "Запуск Docker Compose (VPN + monitoring)"
    log_info "Поднимаем telegram-bot, xray-клиенты, socket-proxy, nginx и monitoring в одном проходе"

    set -o allexport; source "$ENV_FILE"; set +o allexport

    COMPOSE_FILE="/opt/vpn/docker-compose.yml"
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SAVED_IMAGES_DIR="/opt/vpn/docker-images"
    _telegram_bot_ready=0

    # Копируем docker-compose.yml из home/ если его нет в корне /opt/vpn/
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        if [[ -f "${REPO_DIR}/home/docker-compose.yml" ]]; then
            cp "${REPO_DIR}/home/docker-compose.yml" "$COMPOSE_FILE"
            log_ok "docker-compose.yml скопирован из home/"
        fi
    fi

    # Копируем поддиректории из home/ которые нужны docker-compose.yml
    declare -A subdir_key=(
        [telegram-bot]="Dockerfile"
        [prometheus]="prometheus.yml"
        [grafana]="provisioning"
        [alertmanager]="alertmanager.yml"
        [nginx]="grafana.conf"
    )
    for subdir in telegram-bot prometheus grafana alertmanager nginx; do
        key="${subdir_key[$subdir]}"
        src="${REPO_DIR}/home/${subdir}"
        dst="/opt/vpn/${subdir}"
        if [[ -d "$src" && ! -e "${dst}/${key}" ]]; then
            mkdir -p "$dst"
            cp -r "${src}/." "$dst/"
            log_ok "${subdir}/ скопирован из home/"
        fi
    done

    if [[ ! -f "$COMPOSE_FILE" ]]; then
        die "docker-compose.yml не найден в /opt/vpn/"
    else
        cd /opt/vpn

        # Создаём placeholder-файлы для xray конфигов ДО docker compose up.
        # Без этого Docker монтирует несуществующие пути как директории.
        mkdir -p /opt/vpn/xray
        for _xray_cfg in config-xhttp.json config-vision.json config-cdn.json; do
            [[ ! -e "/opt/vpn/xray/${_xray_cfg}" ]] && echo '{}' > "/opt/vpn/xray/${_xray_cfg}"
        done

        # telegram-bot/data монтируется в контейнер как /app/data (от botuser uid=999)
        mkdir -p /opt/vpn/telegram-bot/data
        chown -R 999:999 /opt/vpn/telegram-bot/data
        chmod 750 /opt/vpn/telegram-bot/data

        # Отключаем errexit+pipefail на время docker-операций
        set +e; set +o pipefail

        # ── ФАЗА 1а: загрузка pre-saved образов (install.sh скачал из Releases) ──
        # Если образы есть — docker load быстрее и надёжнее чем зеркала.
        # Если нет — fallback на зеркала (старое поведение).
        _BASE_IMG=""
        _img_count=$(ls "${SAVED_IMAGES_DIR}"/*.tar.gz 2>/dev/null | wc -l || echo 0)

        if [[ "$_img_count" -gt 0 ]]; then
            log_info "Загрузка ${_img_count} Docker-образов из локального кэша (${SAVED_IMAGES_DIR})..."
            if bash /opt/vpn/scripts/docker-load-cache.sh \
                    --dir "$SAVED_IMAGES_DIR" \
                    --label "Локальный Docker image cache" 2>&1; then
                log_ok "Образы загружены из кэша — зеркала не нужны"
            else
                log_warn "Часть локального Docker image cache не загрузилась"
            fi
            _BASE_IMG="python:3.12-slim"  # уже загружен через docker load
        else
            # ── ФАЗА 1б: fallback — зеркала (VPN ещё не поднят) ─────────────────
            log_info "Локальный кэш образов не найден — пробуем Docker-зеркала..."
            log_info "Подсказка: запустите install.sh вместо setup.sh для автозагрузки образов"

            # Временный DNS (BuildKit читает resolv.conf напрямую, dnsmasq ещё не работает)
            _restore_resolv() {
                cp /etc/resolv.conf.docker-bak /etc/resolv.conf 2>/dev/null \
                    || echo "nameserver 127.0.0.1" > /etc/resolv.conf
            }
            cp /etc/resolv.conf /etc/resolv.conf.docker-bak 2>/dev/null || true
            printf "nameserver 77.88.8.8\nnameserver 77.88.8.1\n" > /etc/resolv.conf
            trap '_restore_resolv' EXIT

            # FIX B: br-vpn forward bypass (kill switch не должен дропать контейнеры)
            _nft_bypass_handle=""
            if nft list table inet vpn &>/dev/null 2>&1; then
                _nft_bypass_handle=$(nft -a add rule inet vpn forward iifname "br-vpn" accept 2>/dev/null \
                    | grep -oP 'handle \K[0-9]+' || true)
            fi

            # FIX B2: output chain bypass (Docker daemon → зеркала через output hook)
            _nft_output_bypass_handle=""
            if nft list table inet vpn &>/dev/null 2>&1; then
                _nft_output_bypass_handle=$(nft -a insert rule inet vpn output accept 2>/dev/null \
                    | grep -oP 'handle \K[0-9]+' || true)
            fi

            # Проверка зеркала — реальный pull alpine, не /v2/ ping
            _test_mirror() {
                local mirror="$1"
                timeout 20 docker pull "${mirror}/library/alpine:latest" >/dev/null 2>&1
                local rc=$?
                docker rmi "${mirror}/library/alpine:latest" >/dev/null 2>&1 || true
                return $rc
            }

            _WORKING_MIRROR=""
            if _test_mirror "dockerhub.timeweb.cloud"; then
                _WORKING_MIRROR="dockerhub.timeweb.cloud"
                _BASE_IMG="dockerhub.timeweb.cloud/library/python:3.12-slim"
                log_info "Зеркало: timeweb"
            elif _test_mirror "huecker.io"; then
                _WORKING_MIRROR="huecker.io"
                _BASE_IMG="huecker.io/library/python:3.12-slim"
                log_info "Зеркало: huecker.io"
            elif timeout 20 docker pull alpine:latest >/dev/null 2>&1; then
                docker rmi alpine:latest >/dev/null 2>&1 || true
                _BASE_IMG="python:3.12-slim"
                log_info "Docker Hub доступен напрямую"
            else
                log_warn "Docker Hub и зеркала недоступны — build telegram-bot пропущен"
                log_warn "Требуется локальный Docker image cache, загруженный через install.sh"
            fi

            # Обновляем daemon.json — только рабочее зеркало
            if [[ -n "$_WORKING_MIRROR" ]]; then
                cat > /etc/docker/daemon.json << EOF
{
    "log-driver": "json-file",
    "log-opts": {"max-size": "10m", "max-file": "3"},
    "dns": ["77.88.8.8", "77.88.8.1"],
    "ipv6": false,
    "max-concurrent-downloads": 2,
    "registry-mirrors": ["https://${_WORKING_MIRROR}"]
}
EOF
                systemctl restart docker 2>/dev/null || true
                sleep 3
            fi

            # Pull всех обязательных сервисов, включая monitoring.
            log_info "Pull обязательных образов (по одному, макс. 120 сек)..."
            _pull_failed=0
            REQUIRED_SERVICES=(
                nginx socket-proxy
                xray-client-xhttp xray-client-vision xray-client-cdn
                prometheus alertmanager grafana grafana-renderer node-exporter
            )
            for _svc in "${REQUIRED_SERVICES[@]}"; do
                log_info "  pull: $_svc ..."
                if timeout 120 docker compose --profile monitoring pull "$_svc" >> /tmp/docker-pull.log 2>&1; then
                    log_ok "  $_svc OK"
                else
                    log_warn "  $_svc не скачался"
                    ((_pull_failed++)) || true
                fi
            done
            [[ $_pull_failed -gt 0 ]] && \
                die "Не удалось скачать ${_pull_failed} обязательных образов"
        fi

        # ── Build telegram-bot (python:3.12-slim уже в docker load или зеркале) ──
        if [[ -n "$_BASE_IMG" ]]; then
            log_info "Сборка telegram-bot (base=$_BASE_IMG, макс. 300 сек)..."
            timeout 300 bash -c "DOCKER_BASE_PYTHON='$_BASE_IMG' docker compose build telegram-bot" \
                2>&1 | tee /tmp/docker-build.log
            _BUILD_EXIT=${PIPESTATUS[0]}
            if [[ $_BUILD_EXIT -ne 0 ]]; then
                log_warn "docker compose build завершился с ошибкой (rc=$_BUILD_EXIT)"
                if [[ "$_BASE_IMG" != "python:3.12-slim" ]]; then
                    log_info "Fallback build без зеркала..."
                    timeout 300 bash -c "DOCKER_BASE_PYTHON='python:3.12-slim' docker compose build telegram-bot" \
                        2>&1 | tee /tmp/docker-build-fallback.log
                    _BUILD_EXIT=${PIPESTATUS[0]}
                    [[ $_BUILD_EXIT -ne 0 ]] && die "Fallback build telegram-bot провалился"
                fi
            fi
        fi

        if docker image inspect vpn-telegram-bot:latest >/dev/null 2>&1; then
            _telegram_bot_ready=1
        else
            die "Локальный образ telegram-bot отсутствует после сборки"
        fi

        log_info "Запуск контейнеров (включая профиль monitoring)..."
        timeout 300 docker compose --profile monitoring up -d --no-build --pull missing --remove-orphans \
            2>&1 | tee /tmp/docker-up.log
        _UP_EXIT=${PIPESTATUS[0]}
        [[ $_UP_EXIT -ne 0 ]] && die "docker compose up завершился с ошибкой (код $_UP_EXIT)"

        set -e; set -o pipefail

        # Восстановление: resolv.conf и nftables bypass (только если fallback-путь)
        if declare -f _restore_resolv &>/dev/null; then
            _restore_resolv
            trap - EXIT
        fi
        if [[ -n "${_nft_bypass_handle:-}" ]]; then
            nft delete rule inet vpn forward handle "$_nft_bypass_handle" 2>/dev/null || true
        fi
        if [[ -n "${_nft_output_bypass_handle:-}" ]]; then
            nft delete rule inet vpn output handle "$_nft_output_bypass_handle" 2>/dev/null || true
        fi

        sleep 5
        echo ""
        log_info "Статус Docker-контейнеров:"
        docker compose ps 2>/dev/null || true
        for _required_container in prometheus grafana alertmanager node-exporter telegram-bot; do
            docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${_required_container}$" \
                || die "Обязательный контейнер ${_required_container} не запущен"
        done
    fi

    log_ok "Docker Compose (домашний сервер) завершён"
    step_done "step31_docker_compose_home"
fi

# ── Шаг 32: Установка zapret (nfqws) ─────────────────────────────────────────
if is_done "step32_install_zapret"; then
    step_skip "step32_install_zapret"
else
    step "Установка zapret/nfqws (DPI bypass без туннеля)"

    ZAPRET_INSTALL_SCRIPT=""
    for _candidate in \
        /opt/vpn/watchdog/plugins/zapret/install.sh \
        /opt/vpn/home/watchdog/plugins/zapret/install.sh \
        /opt/vpn/watchdog-src/plugins/zapret/install.sh; do
        if [[ -f "$_candidate" ]]; then
            ZAPRET_INSTALL_SCRIPT="$_candidate"
            break
        fi
    done

    if [[ -n "$ZAPRET_INSTALL_SCRIPT" ]]; then
        if bash "$ZAPRET_INSTALL_SCRIPT"; then
            :
        else
            log_warn "zapret/nfqws установить не удалось — DPI bypass без туннеля будет недоступен"
            log_warn "Запустите вручную позже: bash $ZAPRET_INSTALL_SCRIPT"
        fi
    else
        log_warn "install.sh для zapret не найден — пробуем bundled nfqws напрямую"
        _zapret_arch="$(uname -m)"
        case "$_zapret_arch" in
            x86_64) _zapret_arch="x86_64" ;;
            aarch64) _zapret_arch="aarch64" ;;
        esac
        for _bin in \
            "/opt/vpn/watchdog/plugins/zapret/bin/nfqws-${_zapret_arch}" \
            "/opt/vpn/home/watchdog/plugins/zapret/bin/nfqws-${_zapret_arch}"; do
            if [[ -f "$_bin" ]]; then
                install -D -m 755 "$_bin" /usr/local/bin/nfqws
                modprobe nfnetlink_queue 2>/dev/null || true
                echo "nfnetlink_queue" >> /etc/modules-load.d/zapret.conf 2>/dev/null || true
                log_ok "bundled nfqws установлен напрямую из ${_bin}"
                break
            fi
        done
    fi

    if [[ -x /usr/local/bin/nfqws ]]; then
        log_ok "zapret/nfqws установлен успешно"
    else
        log_warn "zapret/nfqws не установлен — /usr/local/bin/nfqws отсутствует"
    fi

    step_done "step32_install_zapret"
fi

# ── Шаг 33: Адаптивный SSH-прокси и SSH config ───────────────────────────────
if is_done "step33_ssh_proxy"; then
    step_skip "step33_ssh_proxy"
else
    step "Установка адаптивного SSH-прокси (ssh-proxy.sh + SSH config)"

    # netcat-openbsd нужен для nc -X 5 (SOCKS5 ProxyCommand)
    if ! dpkg -l netcat-openbsd 2>/dev/null | grep -q "^ii"; then
        log_info "Установка netcat-openbsd..."
        apt_quiet "Установка netcat-openbsd" install -y -qq netcat-openbsd
    fi
    log_ok "netcat-openbsd установлен"

    # Устанавливаем ssh-proxy.sh
    mkdir -p /opt/vpn/scripts
    if [[ -f "${REPO_DIR}/home/scripts/ssh-proxy.sh" ]]; then
        cp "${REPO_DIR}/home/scripts/ssh-proxy.sh" /opt/vpn/scripts/ssh-proxy.sh
        chmod +x /opt/vpn/scripts/ssh-proxy.sh
        log_ok "ssh-proxy.sh установлен: /opt/vpn/scripts/ssh-proxy.sh"
    else
        log_warn "home/scripts/ssh-proxy.sh не найден в ${REPO_DIR}"
    fi

    # Генерируем ~/.ssh/config из шаблона
    SSH_CONFIG_TEMPLATE="${REPO_DIR}/home/ssh/vps.conf.template"
    if [[ -f "$SSH_CONFIG_TEMPLATE" ]]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        # envsubst подставляет VPS_IP и VPS_SSH_PORT из .env
        envsubst '${VPS_IP} ${VPS_SSH_PORT}' < "$SSH_CONFIG_TEMPLATE" \
            > /root/.ssh/config
        chmod 600 /root/.ssh/config
        log_ok "SSH config сгенерирован: /root/.ssh/config"
        log_info "  Host vps → ${VPS_IP}:${VPS_SSH_PORT:-22} через ssh-proxy.sh"
        log_info "  Host vps-direct → ${VPS_IP}:${VPS_SSH_PORT:-22} напрямую"
    else
        log_warn "Шаблон SSH config не найден: $SSH_CONFIG_TEMPLATE"
    fi

    step_done "step33_ssh_proxy"
fi

# ── Шаг 34: Проверка обязательного monitoring ────────────────────────────────

if is_done "step34_docker_phase2_attempt"; then
    step_skip "step34_docker_phase2_attempt"
else
    step "Проверка обязательного monitoring"
    for _required_container in prometheus grafana alertmanager node-exporter; do
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${_required_container}$"; then
            log_ok "monitoring: ${_required_container}"
        else
            die "monitoring контейнер ${_required_container} не запущен"
        fi
    done

    step_done "step34_docker_phase2_attempt"
fi

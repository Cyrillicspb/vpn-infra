#!/bin/bash
# =============================================================================
# setup.sh — Главный мастер-установщик VPN Infrastructure v4.0
# Запуск: sudo bash setup.sh
# Идемпотентен: уже выполненные шаги пропускаются автоматически
# =============================================================================

set -euo pipefail

# ── Константы и общие функции ─────────────────────────────────────────────────

STEP=0
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$REPO_DIR/common.sh"

# ── Функции, уникальные для setup.sh ─────────────────────────────────────────

ask() {
    local var="$1" prompt="$2" secret="${3:-no}"
    if [[ -n "${!var:-}" ]]; then
        log_info "$var уже задан"
        return
    fi
    local value=""
    if [[ "$secret" == "yes" ]]; then
        read -rsp "  $prompt: " value; echo
    else
        read -rp "  $prompt: " value
    fi
    printf -v "$var" '%s' "$value"
    export "${var?}"
}

# ── Баннер ───────────────────────────────────────────────────────────────────

print_banner() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║      VPN Infrastructure v4.0 — Двухуровневая установка          ║"
    echo "║  Hybrid B+ Split Tunneling | 4 стека | AmneziaWG + WireGuard   ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
}

# ── Фаза 0: Предусловия ──────────────────────────────────────────────────────

phase0() {
    log_info "═══ ФАЗА 0: Предусловия и конфигурация ═══"

    # Шаг 1 — Проверка ОС
    if is_done "step01_os_check"; then
        step_skip "step01_os_check"
    else
        step "Проверка операционной системы"
        if [[ "$(uname -s)" != "Linux" ]]; then
            die "Требуется Linux. Текущая ОС: $(uname -s)"
        fi
        if [[ -f /etc/os-release ]]; then
            # shellcheck disable=SC1091
            source /etc/os-release
            log_info "ОС: ${PRETTY_NAME:-unknown}"
            if [[ "${ID:-}" != "ubuntu" ]] || [[ "${VERSION_ID:-}" != "24.04" ]]; then
                log_warn "Рекомендуется Ubuntu 24.04 LTS. Текущая: ${PRETTY_NAME:-unknown}. Продолжаем..."
            else
                log_ok "Ubuntu 24.04 LTS — подтверждено"
            fi
        else
            log_warn "/etc/os-release не найден. Продолжаем без проверки версии ОС."
        fi
        step_done "step01_os_check"
    fi

    # Шаг 2 — Проверка прав root
    if is_done "step02_root_check"; then
        step_skip "step02_root_check"
    else
        step "Проверка прав суперпользователя"
        if [[ "${EUID}" -ne 0 ]]; then
            die "Запустите скрипт с правами root: sudo bash setup.sh"
        fi
        log_ok "Запущено от root"
        step_done "step02_root_check"
    fi

    # Шаг 3 — Автоопределение сети
    if is_done "step03_network_detect"; then
        step_skip "step03_network_detect"
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }
    else
        step "Автоопределение сетевых параметров"

        # Установить необходимые инструменты сейчас
        apt-get update -qq 2>/dev/null || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            sshpass wireguard-tools curl iproute2 traceroute 2>/dev/null || true

        ETH_IFACE=$(ip route show default 2>/dev/null | awk '/default/ {print $5}' | head -1)
        GATEWAY_IP=$(ip route show default 2>/dev/null | awk '/default/ {print $3}' | head -1)
        HOME_SERVER_IP=$(ip -4 addr show "${ETH_IFACE:-eth0}" 2>/dev/null \
            | awk '/inet / {print $2}' | cut -d/ -f1 | head -1)
        HOME_SUBNET=$(ip -4 addr show "${ETH_IFACE:-eth0}" 2>/dev/null \
            | awk '/inet / {print $2}' | head -1)

        # Внешний IP
        EXTERNAL_IP=""
        EXTERNAL_IP=$(curl -sf --max-time 8 https://api.ipify.org 2>/dev/null) \
            || EXTERNAL_IP=$(curl -sf --max-time 8 https://ifconfig.me 2>/dev/null) \
            || EXTERNAL_IP=$(curl -sf --max-time 8 https://icanhazip.com 2>/dev/null) \
            || true

        [[ -z "${ETH_IFACE:-}" ]]      && die "Не удалось определить сетевой интерфейс. Проверьте подключение к сети."
        [[ -z "${GATEWAY_IP:-}" ]]     && die "Не удалось определить шлюз по умолчанию."
        [[ -z "${HOME_SERVER_IP:-}" ]] && die "Не удалось определить локальный IP сервера."
        [[ -z "${EXTERNAL_IP:-}" ]]    && die "Нет доступа к интернету. Проверьте подключение."

        log_info "Интерфейс:    ${ETH_IFACE}"
        log_info "Шлюз:         ${GATEWAY_IP}"
        log_info "Локальный IP: ${HOME_SERVER_IP}"
        log_info "Подсеть:      ${HOME_SUBNET}"
        log_info "Внешний IP:   ${EXTERNAL_IP}"

        NET_INTERFACE="$ETH_IFACE"
        export ETH_IFACE GATEWAY_IP HOME_SERVER_IP HOME_SUBNET EXTERNAL_IP NET_INTERFACE

        step_done "step03_network_detect"
    fi

    # Шаг 4 — Проверка CGNAT / двойного NAT
    if is_done "step04_cgnat_check"; then
        step_skip "step04_cgnat_check"
    else
        step "Проверка CGNAT и двойного NAT"

        CGNAT_DETECTED=0
        DOUBLE_NAT_DETECTED=0

        # Проверка CGNAT (RFC 6598, диапазон 100.64.0.0/10)
        if [[ "${EXTERNAL_IP:-0.0.0.0}" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]; then
            CGNAT_DETECTED=1
        fi

        # Внешний IP попадает в RFC1918 — однозначный двойной NAT
        if [[ "${EXTERNAL_IP:-0.0.0.0}" =~ ^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.) ]]; then
            DOUBLE_NAT_DETECTED=1
        fi

        # Проверка двойного NAT через traceroute: >=2 RFC1918 хопов до публичной сети
        if [[ $CGNAT_DETECTED -eq 0 ]] && [[ $DOUBLE_NAT_DETECTED -eq 0 ]]; then
            RFC1918_HOPS=0
            while IFS= read -r hop_ip; do
                if [[ "$hop_ip" =~ ^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.) ]]; then
                    ((RFC1918_HOPS++)) || true
                fi
            done < <(traceroute -n -m 3 8.8.8.8 2>/dev/null \
                | awk 'NR>1 && /^[0-9]/ {print $2}')
            [[ $RFC1918_HOPS -ge 2 ]] && DOUBLE_NAT_DETECTED=1
        fi

        if [[ $CGNAT_DETECTED -eq 1 ]] || [[ $DOUBLE_NAT_DETECTED -eq 1 ]]; then
            echo ""
            echo -e "${RED}━━━ ВНИМАНИЕ: CGNAT / Двойной NAT ━━━${NC}"
            echo "Три возможные причины:"
            echo "  1. Провайдер использует CGNAT (диапазон 100.64.0.0/10)"
            echo "  2. Двойной NAT (роутер провайдера + ваш роутер)"
            echo "  3. На роутере провайдера не включён bridge mode"
            echo ""
            echo "Проект не будет работать без реального (белого) IP."
            echo "Решения: попросите у провайдера белый IP или арендуйте дополнительный."
            echo ""
            if [[ -n "${VPN_NONINTERACTIVE:-}" ]]; then
                log_warn "Non-interactive режим: продолжаем несмотря на CGNAT/NAT."
                _cont="y"
            else
                read -rp "  Продолжить установку несмотря на это? [y/N]: " _cont
            fi
            if [[ "${_cont,,}" != "y" ]]; then
                die "Установка прервана пользователем. Устраните проблему CGNAT и повторите."
            fi
            log_warn "Продолжаем несмотря на CGNAT/двойной NAT."
        else
            log_ok "CGNAT не обнаружен. Внешний IP: ${EXTERNAL_IP}"
        fi

        step_done "step04_cgnat_check"
    fi

    # Шаг 5 — Сбор пользовательских данных
    if is_done "step05_collect_inputs"; then
        step_skip "step05_collect_inputs"
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }
    else
        step "Сбор конфигурационных параметров"

        mkdir -p /opt/vpn
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        # step03 мог быть пропущен (повторный запуск), а EXTERNAL_IP ещё не в .env
        if [[ -z "${EXTERNAL_IP:-}" ]]; then
            EXTERNAL_IP=$(curl -sf --max-time 8 https://api.ipify.org 2>/dev/null) \
                || EXTERNAL_IP=$(curl -sf --max-time 8 https://ifconfig.me 2>/dev/null) \
                || EXTERNAL_IP=$(curl -sf --max-time 8 https://icanhazip.com 2>/dev/null) \
                || EXTERNAL_IP=""
        fi

        echo ""
        echo -e "${BOLD}  Введите данные для подключения к VPS:${NC}"
        ask VPS_IP "IP-адрес VPS (например: 1.2.3.4)"
        ask VPS_SSH_PORT "SSH порт VPS (Enter = 22)"
        [[ -z "${VPS_SSH_PORT:-}" ]] && VPS_SSH_PORT="22"
        ask VPS_ROOT_PASSWORD "Пароль root на VPS (для первоначального подключения)" yes

        echo ""
        echo -e "${BOLD}  Введите данные Telegram-бота:${NC}"
        echo "  Получить токен: @BotFather → /newbot"
        echo "  Получить Chat ID: @userinfobot или @getmyid_bot"
        ask TELEGRAM_BOT_TOKEN "Telegram Bot Token (от @BotFather)" yes
        ask TELEGRAM_ADMIN_CHAT_ID "Telegram Admin Chat ID (числовой ID)"

        # Валидация Telegram токена (не блокирует установку — предупреждение)
        if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
            log_info "Проверка Telegram Bot Token..."
            _tg_check=$(curl -sf --max-time 8 \
                --config <(printf 'url = "https://api.telegram.org/bot%s/getMe"' "$TELEGRAM_BOT_TOKEN") \
                2>/dev/null) || true
            if [[ -n "$_tg_check" ]] && echo "$_tg_check" \
                | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
                _bot_name=$(echo "$_tg_check" \
                    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['username'])" 2>/dev/null) || true
                log_ok "Telegram Bot подтверждён: @${_bot_name:-unknown}"
            else
                log_warn "Не удалось проверить Telegram токен (нет интернета или токен неверный)."
                log_warn "Проверьте вручную: https://api.telegram.org/bot<TOKEN>/getMe"
            fi
        fi

        echo ""
        echo -e "${BOLD}  Опциональные компоненты:${NC}"
        ask USE_DDNS "Настроить DDNS? (y/N)"
        if [[ "${USE_DDNS,,}" == "y" ]]; then
            ask DDNS_PROVIDER "Провайдер DDNS (duckdns/noip/cloudflare)"
            ask DDNS_DOMAIN "DDNS домен (например: myhome.duckdns.org)"
            ask DDNS_TOKEN "DDNS токен (для DuckDNS: UUID с сайта duckdns.org, напр. a1b2c3d4-...)" yes
            WG_HOST="${DDNS_DOMAIN}"
        else
            WG_HOST="${EXTERNAL_IP:-}"
            [[ -z "$WG_HOST" ]] && die "Не удалось определить внешний IP. Укажите DDNS или проверьте интернет."
        fi

        # ── CDN-стек (Cloudflare Workers) — опциональный ──────────────────────────
        echo ""
        echo -e "${CYAN}${BOLD}━━━ CDN-стек через Cloudflare Workers (опционально) ━━━${NC}"
        echo ""
        echo "  Самый надёжный стек — трафик через Cloudflare CDN."
        echo "  Заблокировать его = заблокировать весь Cloudflare (тысячи сайтов)."
        echo "  Нужен только бесплатный аккаунт Cloudflare. Домен не требуется."
        echo ""
        echo -e "  ${GREEN}Плюсы:${NC} максимальная устойчивость к блокировкам, бесплатно."
        echo -e "  ${YELLOW}Минусы:${NC} ~10 мин на настройку, чуть медленнее (доп. hop через CDN)."
        echo "  Используется как резервный — только если REALITY-стеки не работают."
        echo ""
        read -rp "  Настроить CDN-стек? (y/N): " USE_CLOUDFLARE
        USE_CLOUDFLARE="${USE_CLOUDFLARE:-n}"

        if [[ "${USE_CLOUDFLARE,,}" == "y" ]]; then
            echo ""
            echo -e "${CYAN}${BOLD}  Шаг A — Регистрация на Cloudflare${NC}"
            echo "  1. https://dash.cloudflare.com/sign-up"
            echo "  2. Email + пароль → «Create Account» → подтвердите email"
            echo "     Бесплатно, карта не нужна."
            echo ""
            read -rp "  Аккаунт Cloudflare готов? (y/N): " _cf_acct
            if [[ "${_cf_acct,,}" != "y" ]]; then
                log_warn "CDN-стек пропущен. Настроить позже: sudo bash setup.sh"
                USE_CLOUDFLARE="n"
            fi
        fi

        if [[ "${USE_CLOUDFLARE,,}" == "y" ]]; then
            echo ""
            echo -e "${CYAN}${BOLD}  Шаг B — Создание Cloudflare Worker${NC}"
            echo "  1. dash.cloudflare.com → «Workers & Pages» → «Create» → «Create Worker»"
            echo "  2. Дайте любое имя, нажмите «Deploy»"
            echo "  3. Нажмите «Edit code», замените ВСЁ на:"
            echo ""
            echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
            echo  "export default {"
            echo  "  async fetch(request) {"
            echo  "    const url = new URL(request.url);"
            echo  "    const target = \`http://${VPS_IP}:8080\${url.pathname}\${url.search}\`;"
            echo  "    const h = new Headers();"
            echo  "    for (const [k,v] of request.headers)"
            echo  "      if (k.toLowerCase() !== 'host') h.set(k,v);"
            echo  "    h.set('Host','${VPS_IP}');"
            echo  "    return fetch(target,{method:request.method,headers:h,body:request.body});"
            echo  "  }"
            echo  "}"
            echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
            echo ""
            echo "  4. «Save & Deploy»"
            echo "  5. Скопируйте URL вида: https://xxx-xxx.ACCOUNT.workers.dev"
            echo ""
            read -rp "  Вставьте URL Worker (например xxx.workers.dev): " CF_CDN_HOSTNAME
            CF_CDN_HOSTNAME="${CF_CDN_HOSTNAME#https://}"
            CF_CDN_HOSTNAME="${CF_CDN_HOSTNAME%/}"
            if [[ -z "$CF_CDN_HOSTNAME" ]]; then
                log_warn "URL не введён — CDN-стек пропущен."
                USE_CLOUDFLARE="n"
            else
                log_ok "Worker: ${CF_CDN_HOSTNAME}"
            fi
        fi

        # Сохранение всех параметров в .env
        env_set "VPS_IP"                 "${VPS_IP}"
        env_set "XRAY_SERVER"            "${VPS_IP}"
        env_set "VPS_SSH_PORT"           "${VPS_SSH_PORT}"
        env_set "TELEGRAM_BOT_TOKEN"     "${TELEGRAM_BOT_TOKEN}"
        env_set "TELEGRAM_ADMIN_CHAT_ID" "${TELEGRAM_ADMIN_CHAT_ID}"
        env_set "WG_HOST"                "${WG_HOST}"
        env_set "USE_DDNS"               "${USE_DDNS:-n}"
        env_set "DDNS_PROVIDER"          "${DDNS_PROVIDER:-}"
        env_set "DDNS_DOMAIN"            "${DDNS_DOMAIN:-}"
        env_set "DDNS_TOKEN"             "${DDNS_TOKEN:-}"
        env_set "USE_CLOUDFLARE"         "${USE_CLOUDFLARE:-n}"
        env_set "CF_TUNNEL_TOKEN"        "${CF_TUNNEL_TOKEN:-}"
        env_set "CF_CDN_HOSTNAME"    "${CF_CDN_HOSTNAME:-}"
        env_set "NET_INTERFACE"          "${NET_INTERFACE:-${ETH_IFACE:-eth0}}"
        env_set "GATEWAY_IP"             "${GATEWAY_IP:-}"
        env_set "HOME_SERVER_IP"         "${HOME_SERVER_IP:-}"
        env_set "HOME_SUBNET"            "${HOME_SUBNET:-}"
        env_set "EXTERNAL_IP"            "${EXTERNAL_IP:-}"

        chmod 600 "$ENV_FILE"
        step_done "step05_collect_inputs"
    fi

    # Шаг 6 — Настройка SSH-ключей и VPS bootstrap
    if is_done "step06_vps_ssh_bootstrap"; then
        step_skip "step06_vps_ssh_bootstrap"
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }
    else
        step "Настройка SSH-доступа к VPS и создание пользователя sysadmin"

        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        # Генерация SSH-ключа если нет
        if [[ ! -f /root/.ssh/vpn_id_ed25519 ]]; then
            mkdir -p /root/.ssh
            chmod 700 /root/.ssh
            ssh-keygen -t ed25519 -f /root/.ssh/vpn_id_ed25519 -N "" \
                -C "vpn-home-server" -q
            log_ok "SSH-ключ сгенерирован: /root/.ssh/vpn_id_ed25519"
        else
            log_info "SSH-ключ уже существует: /root/.ssh/vpn_id_ed25519"
        fi

        # ─── 4-этапный bootstrap: определяем метод подключения к VPS ───────────
        SSH_PORT=""
        BOOTSTRAP_METHOD=""
        PROXY_CMD=""
        NFQWS_BOOTSTRAP_STARTED=false

        # Попытка 1: порт 22 напрямую
        log_info "Попытка 1/4: SSH напрямую порт 22..."
        if timeout 10 bash -c ">/dev/tcp/${VPS_IP}/22" 2>/dev/null; then
            SSH_PORT="22"; BOOTSTRAP_METHOD="direct"
            log_ok "VPS доступен на порту 22"

        # Попытка 2: порт 443 напрямую (SSH уже настроен на 443 при повторном запуске)
        elif timeout 10 bash -c ">/dev/tcp/${VPS_IP}/443" 2>/dev/null; then
            SSH_PORT="443"; BOOTSTRAP_METHOD="direct"
            log_ok "VPS доступен на порту 443"

        else
            # Попытка 3: DPI bypass через nfqws
            log_warn "Прямой SSH недоступен. Попытка 3/4: DPI bypass через nfqws..."

            _install_nfqws_minimal() {
                if [[ -x /usr/local/bin/nfqws ]]; then
                    log_info "nfqws уже установлен, используем существующий"; return 0
                fi
                local arch_dir tmp_dir release_url bin
                case "$(uname -m)" in
                    x86_64)  arch_dir="x86_64" ;;
                    aarch64) arch_dir="aarch64" ;;
                    *) log_warn "Неподдерживаемая архитектура: $(uname -m)"; return 1 ;;
                esac
                tmp_dir=$(mktemp -d)
                release_url=""
                if curl -sSfL --connect-timeout 15 \
                        "https://api.github.com/repos/bol-van/zapret/releases/latest" \
                        -o "$tmp_dir/r.json" 2>/dev/null; then
                    release_url=$(python3 -c "
import json
d=json.load(open('$tmp_dir/r.json'))
for a in d.get('assets',[]):
    if a['name'].endswith('.tar.gz'): print(a['browser_download_url']); break
" 2>/dev/null || true)
                fi
                [[ -z "$release_url" ]] && \
                    release_url="https://github.com/bol-van/zapret/archive/refs/heads/master.tar.gz"
                if ! curl -sSfL --connect-timeout 60 "$release_url" \
                        -o "$tmp_dir/z.tar.gz" 2>/dev/null; then
                    rm -rf "$tmp_dir"; log_warn "Не удалось загрузить zapret"; return 1
                fi
                tar -xzf "$tmp_dir/z.tar.gz" -C "$tmp_dir" 2>/dev/null
                bin=$(find "$tmp_dir" -path "*binaries/${arch_dir}/nfqws" -type f 2>/dev/null | head -1)
                [[ -z "$bin" ]] && \
                    bin=$(find "$tmp_dir" -name "nfqws" -type f 2>/dev/null | head -1)
                if [[ -z "$bin" ]]; then
                    rm -rf "$tmp_dir"; log_warn "nfqws бинарник не найден в архиве"; return 1
                fi
                cp "$bin" /usr/local/bin/nfqws && chmod +x /usr/local/bin/nfqws
                rm -rf "$tmp_dir"
                log_ok "nfqws установлен"
            }

            if _install_nfqws_minimal; then
                modprobe nfnetlink_queue 2>/dev/null || true
                # Временные nftables правила: отправляем SSH/HTTPS к VPS через nfqws
                nft add table inet vpn_bootstrap 2>/dev/null || true
                nft add chain inet vpn_bootstrap output \
                    '{ type filter hook output priority mangle; }' 2>/dev/null || true
                nft add rule inet vpn_bootstrap output \
                    ip daddr "${VPS_IP}" tcp dport "{22,443}" \
                    queue num 200 bypass 2>/dev/null || true
                /usr/local/bin/nfqws --daemon --qnum=200 \
                    --filter-tcp=22,443 \
                    --dpi-desync=fakedsplit --dpi-desync-autottl=2 \
                    2>/dev/null && NFQWS_BOOTSTRAP_STARTED=true || true
                sleep 2
                if timeout 10 bash -c ">/dev/tcp/${VPS_IP}/22" 2>/dev/null; then
                    SSH_PORT="22"; BOOTSTRAP_METHOD="nfqws"
                    log_ok "VPS доступен через nfqws на порту 22"
                elif timeout 10 bash -c ">/dev/tcp/${VPS_IP}/443" 2>/dev/null; then
                    SSH_PORT="443"; BOOTSTRAP_METHOD="nfqws"
                    log_ok "VPS доступен через nfqws на порту 443"
                else
                    log_warn "nfqws не помог — переходим к socat bootstrap"
                fi
            fi

            # Попытка 4: socat bootstrap через веб-консоль VPS
            if [[ -z "$BOOTSTRAP_METHOD" ]]; then
                log_warn "Попытка 4/4: Bootstrap через веб-консоль VPS..."
                apt-get install -y -qq socat 2>/dev/null || \
                    die "Не удалось установить socat. Проверьте доступ к интернету."
                echo ""
                echo -e "${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo -e "${BOLD}${YELLOW}  SSH к VPS заблокирован провайдером${NC}"
                echo -e "${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo ""
                echo "  Нужно выполнить одну команду в веб-консоли VPS."
                echo ""
                echo "  1. Зайдите в панель управления VPS-провайдера"
                echo "  2. Откройте Console / VNC / noVNC"
                echo "  3. Войдите как root"
                echo "  4. Выполните команду:"
                echo ""
                echo -e "  ${CYAN}${BOLD}bash <(curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/bootstrap-vps.sh)${NC}"
                echo ""
                echo "  Или вручную (если GitHub недоступен):"
                echo -e "  ${CYAN}apt install -y socat openssl"
                echo "  openssl req -x509 -newkey rsa:2048 -keyout /tmp/k.pem -out /tmp/c.pem \\"
                echo "    -days 1 -nodes -subj '/CN=vpn' 2>/dev/null"
                echo "  cat /tmp/c.pem /tmp/k.pem > /tmp/vpn-bootstrap.pem && rm /tmp/k.pem /tmp/c.pem"
                echo -e "  socat OPENSSL-LISTEN:443,reuseaddr,fork,cert=/tmp/vpn-bootstrap.pem,verify=0 TCP:127.0.0.1:22 &${NC}"
                echo ""
                if [[ -z "${VPN_NONINTERACTIVE:-}" ]]; then
                    read -r -p "  Нажмите Enter когда команда выполнена на VPS..." _bs_dummy
                fi
                echo ""

                log_info "Проверка socat bootstrap (порт 443 VPS)..."
                _bs_retry=0
                while ! timeout 10 bash -c ">/dev/tcp/${VPS_IP}/443" 2>/dev/null; do
                    _bs_retry=$(( _bs_retry + 1 ))
                    [[ $_bs_retry -ge 6 ]] && die \
                        "Порт 443 VPS не отвечает. Убедитесь что команда bootstrap выполнена."
                    log_warn "Порт 443 не отвечает, ждём... (${_bs_retry}/6)"
                    sleep 5
                done

                # Socat создаёт TLS-туннель 443 → SSH :22 на VPS
                SSH_PORT="22"
                PROXY_CMD="socat - OPENSSL-CONNECT:${VPS_IP}:443,verify=0"
                BOOTSTRAP_METHOD="socat"
                log_ok "Socat bootstrap активен"
            fi
        fi

        [[ -z "$BOOTSTRAP_METHOD" ]] && die "Не удалось подключиться к VPS"
        env_set "VPS_SSH_PORT" "${SSH_PORT}"
        VPS_SSH_PORT="${SSH_PORT}"
        log_ok "Метод подключения: ${BOOTSTRAP_METHOD}, порт: ${SSH_PORT}"

        # Временная функция для подключения как root
        # Динамически читает PROXY_CMD и SSH_PORT (меняются при переключении метода)
        _vps_root_exec() {
            local _extra=()
            [[ -n "${PROXY_CMD:-}" ]] && _extra+=(-o "ProxyCommand=${PROXY_CMD}")
            ssh -p "${SSH_PORT}" -i /root/.ssh/vpn_id_ed25519 \
                -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
                "${_extra[@]}" \
                "root@${VPS_IP}" "$@"
        }

        # Копирование ключа на VPS через sshpass
        log_info "Копирование SSH-ключа на VPS..."
        SSH_KEY_TEST_OPTS=(-p "${SSH_PORT}" -i /root/.ssh/vpn_id_ed25519
                           -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes)
        [[ -n "${PROXY_CMD:-}" ]] && SSH_KEY_TEST_OPTS+=(-o "ProxyCommand=${PROXY_CMD}")
        if ssh "${SSH_KEY_TEST_OPTS[@]}" "root@${VPS_IP}" "echo ok" 2>/dev/null; then
            log_ok "SSH-ключ уже установлен на VPS, пропускаем ssh-copy-id"
        else
            [[ -z "${VPS_ROOT_PASSWORD:-}" ]] && \
                die "VPS_ROOT_PASSWORD не задан. Добавьте в /opt/vpn/.env и повторите."
            SSH_COPY_OPTS=(-i /root/.ssh/vpn_id_ed25519.pub -p "${SSH_PORT}"
                           -o StrictHostKeyChecking=no)
            [[ -n "${PROXY_CMD:-}" ]] && SSH_COPY_OPTS+=(-o "ProxyCommand=${PROXY_CMD}")
            sshpass -p "${VPS_ROOT_PASSWORD}" ssh-copy-id \
                "${SSH_COPY_OPTS[@]}" "root@${VPS_IP}" 2>/dev/null \
                || die "Не удалось скопировать SSH-ключ на VPS. Проверьте пароль root."
            log_ok "SSH-ключ скопирован на VPS"
        fi

        # Создание пользователя sysadmin
        log_info "Создание пользователя sysadmin на VPS..."
        _vps_root_exec "id sysadmin &>/dev/null || ( \
            useradd -m -s /bin/bash sysadmin && \
            usermod -aG sudo sysadmin )"
        _vps_root_exec "mkdir -p /home/sysadmin/.ssh && \
            cp /root/.ssh/authorized_keys /home/sysadmin/.ssh/authorized_keys \
                2>/dev/null || true && \
            chown -R sysadmin:sysadmin /home/sysadmin/.ssh && \
            chmod 700 /home/sysadmin/.ssh && \
            chmod 600 /home/sysadmin/.ssh/authorized_keys 2>/dev/null || true"
        _vps_root_exec \
            "echo 'sysadmin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/sysadmin && \
             chmod 440 /etc/sudoers.d/sysadmin"

        # Отключение root SSH и парольной аутентификации
        log_info "Настройка SSH на VPS..."
        _vps_root_exec \
            "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config; \
             grep -q '^PermitRootLogin' /etc/ssh/sshd_config \
                 || echo 'PermitRootLogin no' >> /etc/ssh/sshd_config; \
             sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config; \
             grep -q '^PasswordAuthentication' /etc/ssh/sshd_config \
                 || echo 'PasswordAuthentication no' >> /etc/ssh/sshd_config" 2>/dev/null || true

        if [[ "$BOOTSTRAP_METHOD" == "socat" ]]; then
            # Socat bootstrap: добавляем SSH порт 443 на VPS, затем убираем socat.
            # nohup чтобы команда пережила разрыв соединения при kill socat.
            log_info "Переводим VPS SSH на порт 443, убираем socat..."
            _vps_root_exec \
                "grep -q '^Port 443' /etc/ssh/sshd_config \
                     || echo 'Port 443' >> /etc/ssh/sshd_config; \
                 nohup bash -c 'sleep 3 && \
                     systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; \
                     sleep 1 && pkill -f \"socat.*OPENSSL-LISTEN:443\" 2>/dev/null; \
                     rm -f /tmp/vpn-bootstrap.pem' \
                     >/dev/null 2>&1 &" 2>/dev/null || true
            # Соединение разорвётся через ~3 сек (socat убит), ждём прямого SSH:443
            log_info "Ожидание прямого SSH на порту 443..."
            sleep 6
            _bs_wait=0
            while ! timeout 10 bash -c ">/dev/tcp/${VPS_IP}/443" 2>/dev/null; do
                _bs_wait=$(( _bs_wait + 1 ))
                [[ $_bs_wait -ge 12 ]] && die \
                    "SSH порт 443 не ответил после перезагрузки (ждали 36 сек)"
                sleep 3
            done
            SSH_PORT="443"; PROXY_CMD=""
            env_set "VPS_SSH_PORT" "443"; VPS_SSH_PORT="443"
            log_ok "Переключились на прямой SSH:443 (socat bootstrap завершён)"
        else
            _vps_root_exec \
                "systemctl reload ssh 2>/dev/null || \
                 systemctl reload sshd 2>/dev/null || true" 2>/dev/null || true
        fi

        # Cleanup: удаляем временные nfqws bootstrap правила
        if [[ "$NFQWS_BOOTSTRAP_STARTED" == "true" ]]; then
            log_info "Очистка временных nfqws bootstrap правил..."
            pkill -f "nfqws.*qnum=200" 2>/dev/null || true
            nft delete table inet vpn_bootstrap 2>/dev/null || true
            log_ok "Временные nfqws правила удалены"
        fi

        # Пароль root больше не нужен — очищаем из .env
        env_set "VPS_ROOT_PASSWORD" ""
        unset VPS_ROOT_PASSWORD
        log_info "VPS_ROOT_PASSWORD очищен из .env"

        log_ok "VPS настроен: sysadmin создан, SSH защищён (метод: ${BOOTSTRAP_METHOD})"
        step_done "step06_vps_ssh_bootstrap"
    fi

    # Определить функции vps_exec / vps_copy (нужны в последующих фазах)
    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }
    vps_exec() {
        ssh -p "${VPS_SSH_PORT:-22}" -i /root/.ssh/vpn_id_ed25519 \
            -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            "sysadmin@${VPS_IP}" "$@"
    }
    vps_copy() {
        scp -P "${VPS_SSH_PORT:-22}" -i /root/.ssh/vpn_id_ed25519 \
            -o StrictHostKeyChecking=no "$@"
    }

    # Шаг 7 — Генерация секретов
    if is_done "step07_generate_secrets"; then
        step_skip "step07_generate_secrets"
    else
        step "Генерация криптографических секретов"

        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        # AmneziaWG ключи
        if [[ -z "${AWG_SERVER_PRIVATE_KEY:-}" ]]; then
            AWG_SERVER_PRIVATE_KEY=$(wg genkey)
            AWG_SERVER_PUBLIC_KEY=$(echo "$AWG_SERVER_PRIVATE_KEY" | wg pubkey)
            env_set "AWG_SERVER_PRIVATE_KEY" "$AWG_SERVER_PRIVATE_KEY"
            env_set "AWG_SERVER_PUBLIC_KEY"  "$AWG_SERVER_PUBLIC_KEY"
            log_ok "Ключи AmneziaWG сгенерированы"
        else
            log_info "Ключи AmneziaWG уже существуют"
        fi

        # WireGuard ключи
        if [[ -z "${WG_SERVER_PRIVATE_KEY:-}" ]]; then
            WG_SERVER_PRIVATE_KEY=$(wg genkey)
            WG_SERVER_PUBLIC_KEY=$(echo "$WG_SERVER_PRIVATE_KEY" | wg pubkey)
            env_set "WG_SERVER_PRIVATE_KEY" "$WG_SERVER_PRIVATE_KEY"
            env_set "WG_SERVER_PUBLIC_KEY"  "$WG_SERVER_PUBLIC_KEY"
            log_ok "Ключи WireGuard сгенерированы"
        else
            log_info "Ключи WireGuard уже существуют"
        fi

        # AWG junk-параметры (анти-DPI обфускация)
        if [[ -z "${AWG_H1:-}" ]]; then
            AWG_H1=$(shuf -i 1-4294967295 -n 1)
            AWG_H2=$(shuf -i 1-4294967295 -n 1)
            AWG_H3=$(shuf -i 1-4294967295 -n 1)
            AWG_H4=$(shuf -i 1-4294967295 -n 1)
            env_set "AWG_H1" "$AWG_H1"
            env_set "AWG_H2" "$AWG_H2"
            env_set "AWG_H3" "$AWG_H3"
            env_set "AWG_H4" "$AWG_H4"
            log_ok "AWG junk-параметры H1-H4 сгенерированы"
        fi

        # Xray UUID
        [[ -z "${XRAY_UUID:-}" ]] && {
            XRAY_UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
            env_set "XRAY_UUID" "$XRAY_UUID"
        }
        [[ -z "${XRAY_GRPC_UUID:-}" ]] && {
            XRAY_GRPC_UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
            env_set "XRAY_GRPC_UUID" "$XRAY_GRPC_UUID"
        }

        # Hysteria2 секреты
        [[ -z "${HYSTERIA2_AUTH:-}" ]] && {
            HYSTERIA2_AUTH=$(openssl rand -hex 32)
            env_set "HYSTERIA2_AUTH" "$HYSTERIA2_AUTH"
        }
        [[ -z "${HYSTERIA2_OBFS_PASSWORD:-}" ]] && {
            HYSTERIA2_OBFS_PASSWORD=$(openssl rand -hex 16)
            env_set "HYSTERIA2_OBFS_PASSWORD" "$HYSTERIA2_OBFS_PASSWORD"
        }

        # XHTTP пароли (Xray 26.x splithttp — обязательны, иначе "empty password" error)
        [[ -z "${XHTTP_MS_PASSWORD:-}" ]] && {
            XHTTP_MS_PASSWORD=$(openssl rand -hex 16)
            env_set "XHTTP_MS_PASSWORD" "$XHTTP_MS_PASSWORD"
        }
        [[ -z "${XHTTP_CDN_PASSWORD:-}" ]] && {
            XHTTP_CDN_PASSWORD=$(openssl rand -hex 16)
            env_set "XHTTP_CDN_PASSWORD" "$XHTTP_CDN_PASSWORD"
        }

        # UUID для CDN-стека (VLESS+WS через Cloudflare Worker)
        [[ -z "${CF_CDN_UUID:-}" ]] && {
            CF_CDN_UUID=$(uuidgen | tr '[:upper:]' '[:lower:]')
            env_set "CF_CDN_UUID" "$CF_CDN_UUID"
        }

        # Прочие секреты
        [[ -z "${WATCHDOG_API_TOKEN:-}" ]] && {
            WATCHDOG_API_TOKEN=$(openssl rand -hex 32)
            env_set "WATCHDOG_API_TOKEN" "$WATCHDOG_API_TOKEN"
        }
        [[ -z "${BACKUP_GPG_PASSPHRASE:-}" ]] && {
            BACKUP_GPG_PASSPHRASE=$(openssl rand -base64 32 | tr -d '/')
            env_set "BACKUP_GPG_PASSPHRASE" "$BACKUP_GPG_PASSPHRASE"
        }
        [[ -z "${GRAFANA_PASSWORD:-}" ]] && {
            GRAFANA_PASSWORD=$(openssl rand -hex 16)
            env_set "GRAFANA_PASSWORD" "$GRAFANA_PASSWORD"
        }
        [[ -z "${XRAY_PANEL_PASSWORD:-}" ]] && {
            XRAY_PANEL_PASSWORD=$(openssl rand -hex 16)
            env_set "XRAY_PANEL_PASSWORD" "$XRAY_PANEL_PASSWORD"
        }
        [[ -z "${DB_ENCRYPTION_KEY:-}" ]] && {
            DB_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
            env_set "DB_ENCRYPTION_KEY" "$DB_ENCRYPTION_KEY"
        }

        # Параметры с умолчаниями
        env_set "WG_AWG_PORT"             "${WG_AWG_PORT:-51820}"
        env_set "WG_WG_PORT"              "${WG_WG_PORT:-51821}"
        env_set "WG_MTU"                  "${WG_MTU:-1320}"
        env_set "DOCKER_SUBNET"           "${DOCKER_SUBNET:-172.20.0.0/24}"
        env_set "VPS_TUNNEL_IP"           "${VPS_TUNNEL_IP:-10.177.2.2}"
        env_set "HOME_TUNNEL_IP"          "${HOME_TUNNEL_IP:-10.177.2.1}"
        env_set "AWG_SUBNET"              "${AWG_SUBNET:-10.177.1.0/24}"
        env_set "WG_SUBNET"               "${WG_SUBNET:-10.177.3.0/24}"
        env_set "DEVICE_LIMIT_PER_CLIENT" "${DEVICE_LIMIT_PER_CLIENT:-5}"
        env_set "FSM_TIMEOUT_MINUTES"     "${FSM_TIMEOUT_MINUTES:-10}"
        env_set "XRAY_SOCKS_PORT"         "${XRAY_SOCKS_PORT:-1080}"
        env_set "XRAY_GRPC_SOCKS_PORT"    "${XRAY_GRPC_SOCKS_PORT:-1081}"
        env_set "BACKUP_VPS_HOST"         "${VPS_IP:-}"
        env_set "BACKUP_VPS_USER"         "sysadmin"

        chmod 600 "$ENV_FILE"
        log_ok "Все секреты сгенерированы и сохранены в ${ENV_FILE}"
        # Примечание: XRAY_PUBLIC_KEY и XRAY_GRPC_PUBLIC_KEY генерируются
        # в шаге 19 (install-home.sh) с помощью Docker/xray x25519
        step_done "step07_generate_secrets"
    fi

    # Шаг 8 — Создание структуры каталогов /opt/vpn
    if is_done "step08_create_dir_structure"; then
        step_skip "step08_create_dir_structure"
    else
        step "Создание структуры каталогов /opt/vpn"

        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        mkdir -p /opt/vpn
        mkdir -p /opt/vpn/telegram-bot/data
        mkdir -p /opt/vpn/backups
        mkdir -p /opt/vpn/scripts
        mkdir -p /opt/vpn/watchdog/plugins
        mkdir -p /opt/vpn/.deploy-snapshot

        # Если setup.sh запущен из /tmp или нет директории home/ — клонируем полный репозиторий.
        # Это происходит при запуске через установщики Windows/macOS (они копируют только 3 скрипта).
        if [[ ! -d "${REPO_DIR}/home" ]]; then
            log_info "Директория home/ не найдена в ${REPO_DIR}."
            log_info "Клонируем полный репозиторий с GitHub..."
            GITHUB_REPO_URL="${GITHUB_REPO_URL:-https://github.com/Cyrillicspb/vpn-infra.git}"
            # Клонируем во временную директорию — /opt/vpn уже существует (создан в шаге 5)
            local _clone_tmp="/tmp/vpn-repo-clone-$$"
            if git clone "$GITHUB_REPO_URL" "$_clone_tmp" 2>/dev/null; then
                cp -r "${_clone_tmp}/." /opt/vpn/
                rm -rf "$_clone_tmp"
                REPO_DIR="/opt/vpn"
                log_ok "Репозиторий клонирован в /opt/vpn"
            else
                rm -rf "$_clone_tmp" 2>/dev/null || true
                log_warn "Клонирование из GitHub не удалось. Установка продолжится,"
                log_warn "но некоторые файлы конфигурации могут отсутствовать."
                log_warn "Если GitHub заблокирован — задайте GITHUB_REPO_URL=<альтернативный_URL>"
            fi
        fi

        # Проверяем что home/ теперь есть — без него установка бессмысленна
        if [[ ! -d "${REPO_DIR}/home" ]]; then
            die "Директория ${REPO_DIR}/home не найдена. Клонирование репозитория не удалось. Проверьте интернет-соединение или укажите GITHUB_REPO_URL."
        fi

        # Копирование файлов из репозитория
        if command -v rsync &>/dev/null; then
            rsync -a --exclude='.git' --exclude='.deploy-snapshot' \
                "${REPO_DIR}/" /opt/vpn/ 2>/dev/null || true
        else
            cp -r "${REPO_DIR}/." /opt/vpn/ 2>/dev/null || true
        fi

        # Создание каталога vpn-routes
        mkdir -p /etc/vpn-routes
        if [[ ! -f /etc/vpn-routes/manual-vpn.txt ]]; then
            # Предзаполняем Telegram API — нужен для алертов watchdog с хоста
            cat > /etc/vpn-routes/manual-vpn.txt << 'EOF'
# Telegram API — требуется для отправки алертов watchdog напрямую с хоста
api.telegram.org
EOF
        fi
        [[ -f /etc/vpn-routes/manual-direct.txt ]] || touch /etc/vpn-routes/manual-direct.txt
        [[ -f /etc/vpn-routes/combined.cidr ]]     || touch /etc/vpn-routes/combined.cidr

        chmod 600 "$ENV_FILE"
        chown -R root:root /opt/vpn 2>/dev/null || true
        chmod 700 /opt/vpn

        log_ok "Структура /opt/vpn создана"
        step_done "step08_create_dir_structure"
    fi
}

# ── Фаза 1: Домашний сервер ──────────────────────────────────────────────────

phase1() {
    log_info "═══ ФАЗА 1: Домашний сервер ═══"
    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    if [[ ! -f "${REPO_DIR}/install-home.sh" ]]; then
        die "Файл install-home.sh не найден в ${REPO_DIR}"
    fi

    # При INSTALL_CLAUDE_CODE=true клонируем репозиторий в /opt/vpn ДО начала
    # установки — чтобы конфиги брались из git и работал deploy.sh (git pull).
    if [[ "${INSTALL_CLAUDE_CODE:-false}" == "true" ]]; then
        if [[ ! -d /opt/vpn/.git ]]; then
            log_info "INSTALL_CLAUDE_CODE=true: клонируем репозиторий в /opt/vpn..."
            mkdir -p /opt/vpn
            # VPS-зеркало предпочтительнее GitHub (может быть заблокирован)
            cloned=false
            if [[ -n "${VPS_IP:-}" ]]; then
                vps_mirror="ssh://sysadmin@${VPS_IP}/opt/vpn/vpn-repo.git"
                log_info "Пробуем VPS-зеркало: $vps_mirror"
                if git clone "$vps_mirror" /opt/vpn 2>/dev/null; then
                    log_ok "Клонировано из VPS-зеркала."
                    cloned=true
                else
                    log_warn "VPS-зеркало недоступно. Пробуем GitHub..."
                fi
            fi
            if [[ "$cloned" == false ]]; then
                github_url="${GITHUB_REPO_URL:-https://github.com/your-org/vpn-infra.git}"
                log_info "Клонируем из GitHub: $github_url"
                git clone "$github_url" /opt/vpn || \
                    log_warn "git clone не удался — /opt/vpn будет создан из REPO_DIR"
            fi
        else
            log_info "Репозиторий уже клонирован в /opt/vpn. git pull..."
            git -C /opt/vpn pull --ff-only 2>/dev/null || \
                log_warn "git pull не удался — используем текущую версию"
        fi
    fi

    STEP=8 bash "${REPO_DIR}/install-home.sh"

    # Claude Code — установка инструмента (Node.js + npm package)
    if [[ "${INSTALL_CLAUDE_CODE:-false}" == "true" ]]; then
        if is_done "step_install_claude_code"; then
            log_info "Пропуск (уже выполнено): Claude Code"
        else
            log_info "Установка Claude Code..."
            bash /opt/vpn/scripts/install-claude-code.sh --skip-clone && \
                echo "step_install_claude_code" >> "$STATE_FILE" || \
                log_warn "install-claude-code.sh завершился с ошибкой (некритично)"
        fi
    fi
}

# ── Фаза 2: VPS ─────────────────────────────────────────────────────────────

phase2() {
    log_info "═══ ФАЗА 2: VPS ═══"
    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    if [[ ! -f "${REPO_DIR}/install-vps.sh" ]]; then
        die "Файл install-vps.sh не найден в ${REPO_DIR}"
    fi

    STEP=31 bash "${REPO_DIR}/install-vps.sh"
}

# ── Фаза 3: Связка ──────────────────────────────────────────────────────────

phase3() {
    log_info "═══ ФАЗА 3: Связка домашнего сервера и VPS ═══"

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    vps_exec() {
        ssh -p "${VPS_SSH_PORT:-22}" -i /root/.ssh/vpn_id_ed25519 \
            -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            "sysadmin@${VPS_IP}" "$@"
    }
    vps_copy() {
        scp -P "${VPS_SSH_PORT:-22}" -i /root/.ssh/vpn_id_ed25519 \
            -o StrictHostKeyChecking=no "$@"
    }

    # Шаг 45 — Подготовка SSH Tier-2 туннеля
    if is_done "step45_exchange_keys"; then
        step_skip "step45_exchange_keys"
    else
        step "Подготовка SSH Tier-2 туннеля (PermitTunnel на VPS)"
        log_info "Tier-2 туннель работает через SSH tun (autossh -w), не WireGuard."
        log_info "Не требует отдельного UDP-порта — использует TCP 22/443."

        # Включаем PermitTunnel на VPS (нужен для ssh -w)
        vps_exec "sudo sed -i '/^#*PermitTunnel/d' /etc/ssh/sshd_config && \
            echo 'PermitTunnel yes' | sudo tee -a /etc/ssh/sshd_config > /dev/null && \
            sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd 2>/dev/null"
        log_ok "PermitTunnel yes добавлен в sshd_config на VPS, sshd перезапущен"

        # Pre-создаём persistent tun0 на VPS.
        # На некоторых ядрах sshd не может создать tun0 через ioctl с нуля,
        # но успешно подключается к уже существующему persistent-устройству.
        vps_exec "sudo ip tuntap add dev tun0 mode tun 2>/dev/null || true"
        log_ok "Persistent tun0 создан на VPS (sshd подключится к существующему)"

        # Устанавливаем autossh на домашнем сервере
        apt-get install -y -qq autossh 2>/dev/null || true
        log_ok "autossh установлен на домашнем сервере"

        step_done "step45_exchange_keys"
    fi

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    # Шаг 46 — Запуск autossh-tier2 (SSH tun туннель 10.177.2.0/30)
    if is_done "step46_tier2_tunnel"; then
        step_skip "step46_tier2_tunnel"
    else
        step "Настройка SSH Tier-2 туннеля autossh-tier2 (10.177.2.0/30)"
        log_info "autossh -w 0:0 подключается к persistent tun0 на VPS."
        log_info "IP: домашний сервер 10.177.2.1, VPS 10.177.2.2 — те же, что и раньше."
        log_info "Транспорт: TCP ${VPS_SSH_PORT:-22} (не UDP) — не блокируется ISP."

        # Останавливаем старый wg-tier2 если работает (предыдущая установка)
        systemctl stop wg-quick@wg-tier2 2>/dev/null || true
        systemctl disable wg-quick@wg-tier2 2>/dev/null || true
        wg-quick down wg-tier2 2>/dev/null || true    # wg-quick down убирает интерфейс и маршруты
        ip link del wg-tier2 2>/dev/null || true      # fallback если wg-quick down не сработал
        log_ok "Старый wg-tier2 остановлен (если был)"

        # Останавливаем wg-tier2 на VPS тоже
        vps_exec "sudo systemctl stop wg-quick@wg-tier2 2>/dev/null; sudo wg-quick down wg-tier2 2>/dev/null; sudo ip link del wg-tier2 2>/dev/null; true" || true

        # Скрипт подключения.
        # Встроенный фоновый монитор назначает IP на tun0 после каждого переподключения.
        # Remote-команда создаёт persistent tun0 на VPS если не существует, назначает IP.
        mkdir -p /opt/vpn/scripts
        cat > /opt/vpn/scripts/tier2-connect.sh << 'CONNEOF'
#!/bin/bash
# SSH Tier-2 tunnel: tun0, 10.177.2.1 (home) <-> 10.177.2.2 (VPS)
# VPS_IP и VPS_SSH_PORT берутся из EnvironmentFile=/opt/vpn/.env

# Фоновый монитор: назначает IP на локальный tun0 при каждом появлении
(while true; do
    if ip link show tun0 &>/dev/null; then
        ip addr replace 10.177.2.1/30 dev tun0 2>/dev/null || true
        ip link set tun0 up 2>/dev/null || true
    fi
    sleep 3
done) &
MONITOR_PID=$!
echo $MONITOR_PID > /run/tier2-monitor.pid

exec autossh -M 0 \
    -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o "ProxyCommand=ncat --proxy 127.0.0.1:1089 --proxy-type socks5 %h %p" \
    -w 0:0 \
    -i /root/.ssh/vpn_id_ed25519 \
    -p "${VPS_SSH_PORT:-22}" \
    "sysadmin@${VPS_IP}" \
    'sudo ip tuntap add dev tun0 mode tun 2>/dev/null; sudo ip link set tun0 up; sudo ip addr replace 10.177.2.2/30 dev tun0; sudo ip route replace 10.177.2.1/32 dev tun0; sleep infinity'
CONNEOF
        chmod +x /opt/vpn/scripts/tier2-connect.sh

        # Systemd unit для autossh-tier2 (без ExecStartPost — IP назначает монитор внутри скрипта)
        cat > /etc/systemd/system/autossh-tier2.service << UNITEOF
[Unit]
Description=SSH Tier-2 Tunnel to VPS (10.177.2.0/30)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=AUTOSSH_GATETIME=0
EnvironmentFile=/opt/vpn/.env
ExecStart=/opt/vpn/scripts/tier2-connect.sh
ExecStop=/bin/bash -c 'kill $(cat /run/tier2-monitor.pid 2>/dev/null) 2>/dev/null; ip link set tun0 down 2>/dev/null; true'
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNITEOF

        systemctl daemon-reload
        systemctl enable autossh-tier2
        systemctl restart autossh-tier2

        # AmneziaWG: создаём директорию и симлинк конфига (wg0 — клиентский, не tier2)
        mkdir -p /etc/amnezia/amneziawg
        ln -sf /etc/wireguard/wg0.conf /etc/amnezia/amneziawg/wg0.conf
        systemctl mask wg-quick@wg0 2>/dev/null || true
        systemctl enable awg-quick@wg0 2>/dev/null || true
        systemctl restart awg-quick@wg0 2>/dev/null \
            || log_warn "awg-quick@wg0 не запустился — продолжаем"

        # WireGuard wg1 (клиенты WG, порт 51821)
        systemctl enable --now wg-quick@wg1 2>/dev/null \
            || log_warn "wg-quick@wg1 не запустился — продолжаем"

        # Ждём поднятия туннеля (autossh + tun0 конфигурация)
        sleep 12
        if ping -c 3 -W 3 10.177.2.2 &>/dev/null; then
            log_ok "Tier-2 SSH туннель работает: ping 10.177.2.2 успешен"
            # tun0 поднят — перезапускаем dnsmasq на VPS чтобы он забиндился на 10.177.2.2
            vps_exec "sudo systemctl restart dnsmasq" \
                && log_ok "dnsmasq на VPS перезапущен (теперь слушает на 10.177.2.2)" \
                || log_warn "Не удалось перезапустить dnsmasq на VPS — DNS может не работать"
        else
            log_warn "Ping 10.177.2.2 не прошёл. Туннель может ещё подниматься — проверьте: systemctl status autossh-tier2"
        fi

        step_done "step46_tier2_tunnel"
    fi

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    # Шаг 46b — Синхронизация ключей Xray REALITY (shortId + publicKey) из VPS
    if is_done "step46b_sync_xray_keys"; then
        step_skip "step46b_sync_xray_keys"
    else
        step "Синхронизация ключей Xray REALITY из VPS в .env"
        log_info "xray-setup.sh генерирует shortIds случайно — читаем из VPS 3x-ui DB"
        log_info "Деривируем publicKey из privateKey через xray x25519"

        # Пишем Python-скрипт в temp файл (без проблем с экранированием)
        TMPSCRIPT=$(mktemp /tmp/xray_keys_XXXXXX.py)
        cat > "$TMPSCRIPT" << 'PYEOF'
import sqlite3, json
db = "/opt/vpn/3x-ui/db/x-ui.db"
try:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT port,stream_settings FROM inbounds WHERE protocol='vless'").fetchall()
    for port, stream_settings in rows:
        try:
            r = json.loads(stream_settings).get("realitySettings",{})
            ids = r.get("shortIds",[""])
            print("{}:{}".format(port, ids[0] if ids else ""))
        except:
            pass
except Exception as e:
    print("ERROR:{}".format(e))
PYEOF

        vps_copy "$TMPSCRIPT" "sysadmin@${VPS_IP}:/tmp/xray_keys.py"
        KEY_DATA=$(vps_exec "sudo python3 /tmp/xray_keys.py; rm -f /tmp/xray_keys.py" 2>/dev/null || echo "")
        rm -f "$TMPSCRIPT"

        if echo "$KEY_DATA" | grep -q "^ERROR:"; then
            log_warn "Ошибка чтения VPS DB: $(echo "$KEY_DATA" | grep "^ERROR:")"
        fi

        REALITY_SHORT_ID=$(echo "$KEY_DATA" | grep "^2087:" | cut -d: -f2)
        GRPC_SHORT_ID=$(echo "$KEY_DATA" | grep "^2083:" | cut -d: -f2)

        if [[ -n "$REALITY_SHORT_ID" ]]; then
            env_set "XRAY_SHORT_ID" "$REALITY_SHORT_ID"
            log_ok "XRAY_SHORT_ID=$REALITY_SHORT_ID (из VPS DB порт 2087)"
        else
            log_warn "shortId для порта 2087 не найден — используем пустой"
        fi

        if [[ -n "$GRPC_SHORT_ID" ]]; then
            env_set "XRAY_GRPC_SHORT_ID" "$GRPC_SHORT_ID"
            log_ok "XRAY_GRPC_SHORT_ID=$GRPC_SHORT_ID (из VPS DB порт 2083)"
        else
            log_warn "shortId для порта 2083 не найден — используем пустой"
        fi

        # Деривируем publicKey из privateKey через xray x25519 в Docker-контейнере
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        # Деривируем publicKey через Python cryptography (не зависит от версии xray бинаря)
        python3 -c "from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey" \
            2>/dev/null || pip3 install -q cryptography 2>/dev/null || true

        derive_x25519_pub() {
            local priv_key="$1"
            python3 - "$priv_key" << 'PYEOF' 2>/dev/null || true
import sys, base64
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
pk = sys.argv[1]
padded = pk + "=" * (4 - len(pk) % 4)
priv = X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(padded))
pub = priv.public_key().public_bytes_raw()
print(base64.urlsafe_b64encode(pub).decode().rstrip("="))
PYEOF
        }

        if [[ -n "${XRAY_PRIVATE_KEY:-}" ]]; then
            NEW_PUB=$(derive_x25519_pub "${XRAY_PRIVATE_KEY}")
            if [[ -n "$NEW_PUB" ]]; then
                env_set "XRAY_PUBLIC_KEY" "$NEW_PUB"
                log_ok "XRAY_PUBLIC_KEY обновлён из XRAY_PRIVATE_KEY"
            else
                log_warn "Не удалось дериватизировать XRAY_PUBLIC_KEY — проверьте XRAY_PRIVATE_KEY в .env"
            fi
        else
            log_warn "XRAY_PRIVATE_KEY не задан — пропускаем деривацию XRAY_PUBLIC_KEY"
        fi

        if [[ -n "${XRAY_GRPC_PRIVATE_KEY:-}" ]]; then
            NEW_GRPC_PUB=$(derive_x25519_pub "${XRAY_GRPC_PRIVATE_KEY}")
            if [[ -n "$NEW_GRPC_PUB" ]]; then
                env_set "XRAY_GRPC_PUBLIC_KEY" "$NEW_GRPC_PUB"
                log_ok "XRAY_GRPC_PUBLIC_KEY обновлён из XRAY_GRPC_PRIVATE_KEY"
            else
                log_warn "Не удалось дериватизировать XRAY_GRPC_PUBLIC_KEY — проверьте XRAY_GRPC_PRIVATE_KEY в .env"
            fi
        else
            log_warn "XRAY_GRPC_PRIVATE_KEY не задан — пропускаем деривацию XRAY_GRPC_PUBLIC_KEY"
        fi

        step_done "step46b_sync_xray_keys"
    fi

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    # Шаг 47 — Генерация конфигов Xray-клиента
    if is_done "step47_xray_client_configs"; then
        step_skip "step47_xray_client_configs"
    else
        step "Генерация конфигов Xray-клиента (VLESS+XHTTP+REALITY)"
        log_info "config-reality.json → SOCKS :1080 → VPS:2087 (XHTTP, microsoft.com)"
        log_info "config-grpc.json    → SOCKS :1081 → VPS:2083 (XHTTP, cdn.jsdelivr.net)"
        log_info "tun2socks создаёт tun-устройство, маршрутизируя трафик fwmark 0x1 в SOCKS."

        XRAY_PUB="${XRAY_PUBLIC_KEY:-}"
        XRAY_GRPC_PUB="${XRAY_GRPC_PUBLIC_KEY:-}"
        XRAY_SID="${XRAY_SHORT_ID:-}"
        XRAY_GRPC_SID="${XRAY_GRPC_SHORT_ID:-}"

        if [[ -z "$XRAY_PUB" ]]; then
            log_warn "XRAY_PUBLIC_KEY не найден — пропускаем генерацию Xray-конфигов."
            log_warn "Шаг 18 (install-home.sh) должен сгенерировать ключи через Docker."
        else
            mkdir -p /opt/vpn/xray

            # Конфиг VLESS+XHTTP+REALITY (stack 3: microsoft.com, порт 2087)
            # XHTTP (splithttp) — Xray 26.x, без flow vision (несовместим с splithttp)
            # password в splithttpSettings — обязателен для Xray 26.x (иначе "empty password" error)
            cat > /opt/vpn/xray/config-reality.json << EOF
{
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": ${XRAY_SOCKS_PORT:-1080},
        "protocol": "socks",
        "settings": {"udp": true}
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": "${VPS_IP}",
                "port": 2087,
                "users": [{
                    "id": "${XRAY_UUID}",
                    "encryption": "none",
                    "flow": ""
                }]
            }]
        },
        "streamSettings": {
            "network": "splithttp",
            "security": "reality",
            "realitySettings": {
                "fingerprint": "chrome",
                "serverName": "microsoft.com",
                "publicKey": "${XRAY_PUB}",
                "shortId": "${XRAY_SID}"
            },
            "splithttpSettings": {
                "path": "/",
                "host": "microsoft.com",
                "password": "${XHTTP_MS_PASSWORD:-}"
            }
        }
    }]
}
EOF

            # Конфиг VLESS+XHTTP+REALITY (stack 2: cdn.jsdelivr.net, порт 2083)
            # SNI cdn.jsdelivr.net — более устойчив к блокировкам чем microsoft.com
            cat > /opt/vpn/xray/config-grpc.json << EOF
{
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": ${XRAY_GRPC_SOCKS_PORT:-1081},
        "protocol": "socks",
        "settings": {"udp": true}
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": "${VPS_IP}",
                "port": 2083,
                "users": [{
                    "id": "${XRAY_GRPC_UUID}",
                    "encryption": "none",
                    "flow": ""
                }]
            }]
        },
        "streamSettings": {
            "network": "splithttp",
            "security": "reality",
            "realitySettings": {
                "fingerprint": "chrome",
                "serverName": "cdn.jsdelivr.net",
                "publicKey": "${XRAY_GRPC_PUB}",
                "shortId": "${XRAY_GRPC_SID}"
            },
            "splithttpSettings": {
                "path": "/",
                "host": "cdn.jsdelivr.net",
                "password": "${XHTTP_CDN_PASSWORD:-}"
            }
        }
    }]
}
EOF
            log_ok "Конфиги Xray-клиента созданы (XHTTP/splithttp)"

            # CDN-стек: config-cdn.json (если настроен CF Worker)
            if [[ -n "${CF_CDN_HOSTNAME:-}" ]]; then
                CF_CDN_UUID="${CF_CDN_UUID:-$(python3 -c "import uuid; print(uuid.uuid4())")}"
                env_set "CF_CDN_UUID" "${CF_CDN_UUID}"
                cat > /opt/vpn/xray/config-cdn.json << CDNEOF
{
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": 1082,
        "protocol": "socks",
        "settings": {"udp": true}
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": "${CF_CDN_HOSTNAME}",
                "port": 443,
                "users": [{"id": "${CF_CDN_UUID}", "encryption": "none", "flow": ""}]
            }]
        },
        "streamSettings": {
            "network": "splithttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "${CF_CDN_HOSTNAME}",
                "alpn": ["h2", "http/1.1"],
                "allowInsecure": false
            },
            "splithttpSettings": {
                "path": "/vpn-cdn",
                "host": "${CF_CDN_HOSTNAME}",
                "xPaddingBytes": "100-1000"
            }
        },
        "tag": "vless-xhttp-cdn-out"
    }]
}
CDNEOF
                log_ok "Конфиг CDN-стека создан: /opt/vpn/xray/config-cdn.json"
                # Перезапуск xray-client-cdn с новым конфигом
                if command -v docker &>/dev/null && docker ps &>/dev/null 2>&1; then
                    docker compose -f /opt/vpn/docker-compose.yml \
                        restart xray-client-cdn 2>/dev/null || true
                fi
            fi

            # Перезапуск основных Xray-контейнеров если Docker запущен
            if command -v docker &>/dev/null && docker ps &>/dev/null 2>&1; then
                docker compose -f /opt/vpn/docker-compose.yml \
                    restart xray-client xray-client-2 2>/dev/null || true
            fi
        fi

        step_done "step47_xray_client_configs"
    fi

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    # Шаг 48 — Конфиг Hysteria2
    if is_done "step48_hysteria2_config"; then
        step_skip "step48_hysteria2_config"
    else
        step "Генерация конфига Hysteria2-клиента"

        # Перезагружаем .env — HYSTERIA2_CERT_HASH был добавлен в install-vps.sh
        [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

        # TLS: self-signed cert — используем pinSHA256 если хеш известен
        local _tls_section
        if [[ -n "${HYSTERIA2_CERT_HASH:-}" ]]; then
            _tls_section="tls:
  insecure: false
  pinSHA256: ${HYSTERIA2_CERT_HASH}"
        else
            _tls_section="tls:
  insecure: false"
            log_warn "HYSTERIA2_CERT_HASH не задан — TLS включён без pin-верификации"
        fi

        mkdir -p /etc/hysteria
        cat > /etc/hysteria/config.yaml << EOF
# Hysteria2 клиент — stack 4 (QUIC + Salamander)
server: ${VPS_IP}:443

${_tls_section}

auth: ${HYSTERIA2_AUTH}

obfs:
  type: salamander
  salamander:
    password: ${HYSTERIA2_OBFS_PASSWORD}

bandwidth:
  up: 50 mbps
  down: 200 mbps

quic:
  keepAlivePeriod: 20s

socks5:
  listen: 127.0.0.1:1083

log:
  level: warn
EOF
        chmod 600 /etc/hysteria/config.yaml
        log_ok "Конфиг Hysteria2 создан: /etc/hysteria/config.yaml"
        step_done "step48_hysteria2_config"
    fi

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    # Шаг 49 — Запуск всех VPN-сервисов
    if is_done "step49_start_services"; then
        step_skip "step49_start_services"
    else
        step "Запуск VPN-сервисов"

        systemctl daemon-reload 2>/dev/null || true

        # Запуск в правильном порядке (согласно CLAUDE.md)
        for svc in nftables vpn-sets-restore dnsmasq hysteria2 watchdog; do
            if systemctl list-unit-files "${svc}.service" &>/dev/null 2>&1; then
                systemctl start "$svc" 2>/dev/null \
                    && log_ok "  ${svc}: запущен" \
                    || log_warn "  ${svc}: не запустился (проверьте journalctl -u ${svc})"
            else
                log_warn "  Юнит ${svc}.service не найден — пропускаем"
            fi
        done

        # Docker Compose
        if command -v docker &>/dev/null && [[ -f /opt/vpn/docker-compose.yml ]]; then
            log_info "Запуск Docker-контейнеров..."
            docker compose -f /opt/vpn/docker-compose.yml up -d 2>/dev/null || true
        fi

        sleep 10
        echo ""
        log_info "Статус сервисов:"
        for svc in nftables dnsmasq hysteria2 watchdog awg-quick@wg0; do
            if systemctl is-active "$svc" &>/dev/null 2>&1; then
                log_ok "  ${svc}: активен"
            else
                log_warn "  ${svc}: неактивен"
            fi
        done

        step_done "step49_start_services"
    fi
}

# ── Фаза 4: Smoke-тесты ──────────────────────────────────────────────────────

phase4() {
    log_info "═══ ФАЗА 4: Проверка работоспособности (smoke-тесты) ═══"

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    PASS=0
    FAIL=0
    FAILED_TESTS=()

    run_test() {
        local name="$1" cmd="$2" hint="$3"
        # Используем subshell чтобы exit 0/exit 1 внутри команды не завершали setup.sh
        if (eval "$cmd") &>/dev/null 2>&1; then
            log_ok "  PASS: $name"
            ((PASS++)) || true
        else
            log_error "  FAIL: $name"
            FAILED_TESTS+=("${name}  →  ${hint}")
            ((FAIL++)) || true
        fi
    }

    # Шаг 50 — DNS (dnsmasq форвардит youtube.com через VPS DNS 10.177.2.2)
    # Требует работающего tier-2 туннеля. Тест запускается первым но с retry.
    step "Тест DNS (dnsmasq)"
    log_info "Проверяем: dig @127.0.0.1 youtube.com — должен вернуть IP (через VPS DNS)"
    run_test "DNS резолвинг через 127.0.0.1" \
        "for _i in \$(seq 1 6); do
             dig @127.0.0.1 youtube.com +short +time=5 2>/dev/null | grep -qE '^[0-9]' && exit 0
             sleep 5
         done; exit 1" \
        "systemctl status dnsmasq; journalctl -u dnsmasq -n 30"

    # Шаг 51 — VPN туннель Tier-2 (autossh-tier2 через SOCKS5 watchdog)
    # Туннель поднимается после старта watchdog + активации первого VPN-стека.
    # Ждём до 60 сек.
    step "Тест VPN-туннеля Tier-2"
    run_test "Ping Tier-2 (10.177.2.2)" \
        "for _i in \$(seq 1 12); do
             ping -c 1 -W 2 10.177.2.2 &>/dev/null && exit 0
             sleep 5
         done; exit 1" \
        "systemctl status autossh-tier2; journalctl -u autossh-tier2 -n 20; systemctl status watchdog"

    # Шаг 52 — Watchdog API
    step "Тест Watchdog HTTP API"
    log_info "Watchdog управляет стеками, мониторит туннели и отправляет алерты в Telegram"
    run_test "GET /status на :8080" \
        "curl -sf --max-time 5 \
            -H 'Authorization: Bearer ${WATCHDOG_API_TOKEN:-tok}' \
            http://localhost:8080/status" \
        "systemctl status watchdog; journalctl -u watchdog -n 30"

    # Шаг 53 — Policy routing
    step "Тест split tunneling (policy routing)"
    # Проверяем ip rule fwmark 0x1 → lookup 200, а НЕ маршрут в table 200.
    # Маршрут default dev tun-X появляется только после старта watchdog и первого стека,
    # а ip rule создаётся сразу при старте vpn-routes.service.
    run_test "Policy routing: fwmark 0x1 → lookup 200" \
        "ip rule show | grep -qE 'fwmark 0x1 lookup (200|marked)'" \
        "systemctl status vpn-routes; systemctl restart vpn-routes; ip rule show"

    # Шаг 55 — Мониторинг (домашний сервер)
    step "Тест мониторинга (домашний сервер)"
    run_test "Prometheus healthy" \
        "PROM_IP=\$(docker inspect prometheus --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null); curl -sf --max-time 5 http://\${PROM_IP}:9090/-/healthy" \
        "docker compose -f /opt/vpn/docker-compose.yml logs --tail=20 prometheus"
    run_test "Grafana healthy" \
        "GRAFANA_IP=\$(docker inspect grafana --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null); curl -sf --max-time 5 http://\${GRAFANA_IP}:3000/api/health | grep -q ok" \
        "docker compose -f /opt/vpn/docker-compose.yml logs --tail=20 grafana"
    run_test "Alertmanager healthy" \
        "AM_IP=\$(docker inspect alertmanager --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null); curl -sf --max-time 5 http://\${AM_IP}:9093/-/healthy" \
        "docker compose -f /opt/vpn/docker-compose.yml logs --tail=20 alertmanager"

    # Шаг 56 — DKMS AmneziaWG
    step "Тест DKMS модуля AmneziaWG"
    run_test "Модуль AmneziaWG загружен" \
        "lsmod 2>/dev/null | grep -qi amneziawg \
            || dkms status 2>/dev/null | grep -qi amneziawg" \
        "dkms status; apt install amneziawg-dkms"

    # Шаг 57 — Telegram Bot (после watchdog + активного стека + прогрева DNS)
    # Telegram заблокирован в России — доступен только через VPN-туннель.
    # Ждём до 60 сек пока watchdog поднимет первый стек и DNS прогреется.
    step "Тест Telegram-бота (через VPN-туннель)"
    run_test "Telegram getMe API (через тун)" \
        "for i in \$(seq 1 12); do
             if curl -sf --max-time 10 \
                 'https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN:-x}/getMe' \
                 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); exit(0 if d.get(\"ok\") else 1)'; then
                 exit 0
             fi
             sleep 5
         done
         exit 1" \
        "Telegram недоступен через тун. Проверьте: ip route show table 200; systemctl status watchdog; journalctl -u watchdog -n 30"

    # Итоги
    TOTAL=$((PASS + FAIL))
    echo ""
    echo -e "${BOLD}━━━ Результаты: ${PASS}/${TOTAL} тестов прошли ━━━${NC}"

    if [[ ${#FAILED_TESTS[@]} -gt 0 ]]; then
        echo ""
        echo -e "${YELLOW}Не прошедшие тесты и подсказки по исправлению:${NC}"
        for t in "${FAILED_TESTS[@]}"; do
            echo -e "  ${RED}✗${NC} $t"
        done
        echo ""
        log_warn "После устранения проблем: sudo bash setup.sh (выполненные шаги пропустятся)"
    else
        log_ok "Все ${TOTAL} тестов прошли успешно!"
    fi
}

# ── Фаза 5: Ручные шаги ─────────────────────────────────────────────────────

phase5() {
    log_info "═══ ФАЗА 5: Ручные действия ═══"

    [[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

    step "Инструкции по завершению настройки"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║             НЕОБХОДИМЫЕ РУЧНЫЕ ДЕЙСТВИЯ                         ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
    echo -e "${RED}${BOLD}  ⚠  Без этих шагов VPN не будет работать для клиентов!${NC}"
    echo ""

    echo -e "${BOLD}━━━ ШАГ A: Проброс портов на роутере ━━━${NC}"
    echo "   Войдите в веб-панель роутера (обычно http://192.168.1.1)"
    echo "   Найдите раздел: Port Forwarding / Virtual Server / NAT"
    echo "   Добавьте два правила (протокол UDP):"
    echo ""
    echo "   ┌────────────────────────────────────────────────────────────┐"
    echo "   │  Внешний порт  →  Внутренний адрес : Порт                 │"
    echo "   │  UDP 51820     →  ${HOME_SERVER_IP:-<LAN-IP-сервера>} : 51820  (AmneziaWG)   │"
    echo "   │  UDP 51821     →  ${HOME_SERVER_IP:-<LAN-IP-сервера>} : 51821  (WireGuard)   │"
    echo "   └────────────────────────────────────────────────────────────┘"
    echo ""
    echo "   Проверка (с телефона через мобильный интернет):"
    echo "   nc -vzu ${WG_HOST:-<внешний-IP>} 51820   # должен ответить"
    echo ""

    echo -e "${BOLD}━━━ ШАГ B: Первый запуск Telegram-бота ━━━${NC}"
    echo "   a) Откройте Telegram, найдите бота по имени (от @BotFather)"
    echo "   b) Нажмите /start — первый пользователь автоматически станет"
    echo "      администратором (без invite-кода)"
    echo "   c) Следуйте инструкциям: выберите протокол → назовите устройство"
    echo "   d) Получите конфиг и импортируйте в WireGuard / AmneziaWG"
    echo ""

    echo -e "${BOLD}━━━ ШАГ C: mTLS сертификат для Grafana/панели (опционально) ━━━${NC}"
    echo "   mTLS CA уже создан на VPS: /opt/vpn/nginx/mtls/ca.crt"
    echo "   Для доступа к Grafana (https://VPS:8443/grafana/) нужен клиентский cert."
    echo "   Через бот: /renew-cert — получить .p12 файл для браузера"
    echo "   Grafana: https://${VPS_IP:-<VPS_IP>}:8443/grafana/"
    echo "   Логин: admin / Пароль: ${GRAFANA_PASSWORD:-<см. /opt/vpn/.env>}"
    echo ""

    if [[ "${USE_CLOUDFLARE:-n}" == "y" && -n "${CF_CDN_HOSTNAME:-}" ]]; then
        echo -e "${BOLD}━━━ ШАГ D: CDN-стек (Cloudflare Worker настроен) ━━━${NC}"
        echo "   Worker: https://${CF_CDN_HOSTNAME}"
        echo "   Стек автоматически активируется watchdog при блокировке XHTTP."
        echo "   Для ручного переключения: /switch cdn (через Telegram-бот)"
        echo ""
    fi

    echo -e "${BOLD}━━━ КОМАНДЫ ДИАГНОСТИКИ ━━━${NC}"
    echo "   wg show                                  — пиры WireGuard"
    echo "   systemctl status watchdog                — агент"
    echo "   journalctl -u watchdog -f                — логи агента"
    echo "   nft list set inet vpn blocked_static | wc -l"
    echo "                                            — заблокированных IP"
    echo "   docker compose -f /opt/vpn/docker-compose.yml ps"
    echo "                                            — контейнеры"
    echo "   curl -sf http://localhost:8080/status -H 'Authorization: Bearer \$(grep '^WATCHDOG_API_TOKEN=' /opt/vpn/.env | cut -d= -f2-)'"
    echo "                                            — статус watchdog"
    echo ""
    echo -e "${BOLD}━━━ SSH ДОСТУП ━━━${NC}"
    echo "   Домашний сервер: ssh sysadmin@${HOME_SERVER_IP:-<IP>}"
    echo "   VPS (через туннель): ssh -i /root/.ssh/vpn_id_ed25519 sysadmin@10.177.2.2"
    echo ""
    echo "   Документация: https://github.com/Cyrillicspb/vpn-infra/blob/master/docs/"
    echo ""
    echo "╚══════════════════════════════════════════════════════════════════╝"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    mkdir -p "$(dirname "$STATE_FILE")"
    touch "$STATE_FILE"
    chmod 600 "$STATE_FILE"

    print_banner

    phase0
    phase1
    phase2
    STEP=44
    phase3
    phase4
    phase5

    # ── Финальная комплексная проверка + отчёт в Telegram ────────────────────
    echo ""
    log_info "═══ ФИНАЛЬНАЯ ПРОВЕРКА ═══"
    POST_CHECK="/opt/vpn/scripts/post-install-check.sh"
    if [[ -f "$POST_CHECK" ]]; then
        bash "$POST_CHECK" || true
    else
        log_warn "post-install-check.sh не найден — скопируйте из репозитория"
    fi

    echo ""
    log_ok "Установка завершена!"
    echo ""
    echo -e "${GREEN}${BOLD}VPN-инфраструктура v4.0 установлена.${NC}"
    echo "  Конфигурация: ${ENV_FILE}"
    echo "  Логи агента:  journalctl -u watchdog -f"
    echo "  Управление:   Telegram-бот (команда /help)"
    echo "  Повтор проверки: sudo bash /opt/vpn/scripts/post-install-check.sh"
    echo ""
}

main "$@"

#!/usr/bin/env bash
# dev/reset-home.sh — полный сброс домашнего сервера к состоянию "чистая Ubuntu с SSH"
#
# Назначение: когда установка vpn-infra на домашнем сервере сломалась и нужно
# начать заново без переустановки ОС.
#
# Запуск через VNC-консоль или локально:
#   curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/dev/reset-home.sh | sudo bash
#
# Если GitHub заблокирован — скрипт уже на диске:
#   sudo bash /opt/vpn/dev/reset-home.sh
#
# Требования: запускать от root. Идемпотентен.

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Запустите от root: sudo bash $0${NC}"
    exit 1
fi

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════╗"
echo "║    Home Server Reset — vpn-infra dev     ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"
warn "Этот скрипт сбросит домашний сервер к состоянию 'чистая Ubuntu с SSH'."
warn "Docker engine, физическая сеть, /opt/vpn и пароль root НЕ трогаются."
echo ""

# ── 1. WireGuard / AmneziaWG — остановить ────────────────────────────────────
step "1. WireGuard / AmneziaWG"
set +e

for wg_iface in wg0 wg1; do
    if ip link show "$wg_iface" &>/dev/null; then
        info "Останавливаем ${wg_iface}..."
        # Пробуем awg-quick (AmneziaWG), потом wg-quick (WireGuard)
        awg-quick down "$wg_iface" 2>/dev/null \
            || wg-quick  down "$wg_iface" 2>/dev/null \
            || ip link del "$wg_iface" 2>/dev/null \
            || true
        ok "${wg_iface} остановлен"
    else
        info "${wg_iface}: не поднят — пропускаем"
    fi
done

for svc in "awg-quick@wg0" "awg-quick@wg1" "wg-quick@wg0" "wg-quick@wg1"; do
    systemctl stop    "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done
ok "WireGuard-сервисы отключены"

set -e

# ── 2. SSH — сбросить к дефолту ──────────────────────────────────────────────
step "2. SSH"
set +e

SSHD_CFG=/etc/ssh/sshd_config

info "Разрешаем вход по паролю и root login..."

if grep -q '^PasswordAuthentication' "$SSHD_CFG"; then
    sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' "$SSHD_CFG"
else
    echo "PasswordAuthentication yes" >> "$SSHD_CFG"
fi

if grep -q '^PermitRootLogin' "$SSHD_CFG"; then
    sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' "$SSHD_CFG"
else
    echo "PermitRootLogin yes" >> "$SSHD_CFG"
fi

sed -i '/^AllowUsers/d'   "$SSHD_CFG"
sed -i '/^MaxAuthTries/d' "$SSHD_CFG"

info "Удаляем authorized_keys..."
rm -f /root/.ssh/authorized_keys
rm -f /home/sysadmin/.ssh/authorized_keys 2>/dev/null || true

info "Перезапускаем sshd..."
systemctl restart sshd && ok "sshd перезапущен" || warn "sshd restart завершился с ошибкой"

set -e

# ── 3. fail2ban — разбанить и сбросить ───────────────────────────────────────
step "3. fail2ban"
set +e

info "Разбаниваем всех..."
fail2ban-client unban --all 2>/dev/null && ok "fail2ban: все IP разбанены" || warn "fail2ban-client недоступен — пропускаем"

info "Удаляем кастомные jail..."
rm -f /etc/fail2ban/jail.local
rm -f /etc/fail2ban/jail.d/vpn-*.conf 2>/dev/null || true

systemctl restart fail2ban 2>/dev/null && ok "fail2ban перезапущен" || warn "fail2ban не запущен — пропускаем"

set -e

# ── 4. nftables — сбросить к policy accept ───────────────────────────────────
step "4. nftables"
set +e

info "Сбрасываем ruleset..."
nft flush ruleset 2>/dev/null || true

info "Записываем минимальный конфиг (policy accept)..."
cat > /etc/nftables.conf << 'NFTEOF'
#!/usr/sbin/nft -f
table inet filter {
    chain input {
        type filter hook input priority 0; policy accept;
    }
    chain forward {
        type filter hook forward priority 0; policy accept;
    }
    chain output {
        type filter hook output priority 0; policy accept;
    }
}
NFTEOF

nft -f /etc/nftables.conf && ok "nftables: policy accept применён" || warn "nft -f завершился с ошибкой"
systemctl restart nftables 2>/dev/null && ok "nftables перезапущен" || warn "nftables.service недоступен — пропускаем"

set -e

# ── 5. Docker — остановить контейнеры, удалить br-vpn ────────────────────────
step "5. Docker"
set +e

COMPOSE_FILE=/opt/vpn/docker-compose.yml
if [[ -f "$COMPOSE_FILE" ]]; then
    info "Останавливаем docker compose..."
    docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null && ok "docker compose down" || warn "docker compose down завершился с ошибкой"
else
    warn "docker-compose.yml не найден — пропускаем compose down"
fi

if command -v docker &>/dev/null; then
    info "Очищаем все образы и контейнеры..."
    docker system prune -af 2>/dev/null && ok "docker system prune выполнен" || warn "docker system prune завершился с ошибкой"
    info "Останавливаем docker daemon..."
    systemctl stop docker 2>/dev/null && ok "docker остановлен" || warn "docker stop завершился с ошибкой"
else
    warn "Docker не установлен — пропускаем"
fi

info "Удаляем Docker bridge br-vpn..."
if ip link show br-vpn &>/dev/null; then
    ip link set br-vpn down 2>/dev/null || true
    ip link del  br-vpn 2>/dev/null \
        && ok "br-vpn удалён" \
        || warn "br-vpn удалить не удалось — будет удалён при следующем docker restart"
else
    info "br-vpn не существует — пропускаем"
fi

set -e

# ── 6. dnsmasq — остановить, сбросить resolv.conf ────────────────────────────
step "6. dnsmasq / resolv.conf"
set +e

info "Останавливаем dnsmasq..."
systemctl stop    dnsmasq 2>/dev/null && ok "dnsmasq остановлен" || warn "dnsmasq не запущен"
systemctl disable dnsmasq 2>/dev/null || true

info "Сбрасываем resolv.conf к дефолту..."
cat > /etc/resolv.conf << 'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
EOF
ok "resolv.conf: 1.1.1.1, 8.8.8.8"

# Убираем immutable flag если был выставлен (dnsmasq иногда защищает файл)
chattr -i /etc/resolv.conf 2>/dev/null || true

set -e

# ── 7. Policy routing — убрать ip rules и таблицы 100/200/201 ────────────────
step "7. Policy routing"
set +e

info "Удаляем кастомные ip rules (fwmark 0x1, 0x2, from 10.177.x.x)..."
ip rule show 2>/dev/null | awk '{print $1}' | while read -r prio_colon; do
    prio="${prio_colon%:}"
    # Удаляем только правила не из диапазона system (0, 32766, 32767)
    if [[ "$prio" =~ ^[0-9]+$ ]] && [[ "$prio" -lt 32766 ]] && [[ "$prio" -gt 0 ]]; then
        ip rule del pref "$prio" 2>/dev/null || true
    fi
done

# Очищаем таблицы маршрутизации vpn-infra
for tbl in 100 200 201; do
    ip route flush table "$tbl" 2>/dev/null || true
done
ok "ip rules и кастомные таблицы очищены"

set -e

# ── 8. Systemd-сервисы vpn-infra — остановить и удалить ──────────────────────
step "8. Systemd-сервисы vpn-infra"
set +e

VPN_SERVICES=(
    hysteria2
    watchdog
    autossh-vpn
    vpn-routes
    vpn-sets-restore
    vpn-postboot
    awg-quick@wg0
    awg-quick@wg1
    wg-quick@wg0
    wg-quick@wg1
)

for svc in "${VPN_SERVICES[@]}"; do
    svc_name="${svc//\@/_}"  # для имени файла (awg-quick@wg0 → awg-quick@wg0.service)
    if systemctl cat "${svc}.service" &>/dev/null 2>&1; then
        info "Останавливаем и удаляем ${svc}..."
        systemctl stop    "${svc}" 2>/dev/null || true
        systemctl disable "${svc}" 2>/dev/null || true
        # Удаляем только файлы в /etc/systemd/system (не дефолтные юниты)
        rm -f "/etc/systemd/system/${svc}.service"
        ok "${svc} удалён"
    else
        info "${svc}: не установлен — пропускаем"
    fi
done

systemctl daemon-reload
ok "systemd daemon-reload выполнен"

set -e

# ── 9. Cron — удалить vpn-задания ────────────────────────────────────────────
step "9. Cron"
set +e

if crontab -l 2>/dev/null | grep -q '/opt/vpn'; then
    info "Удаляем cron-задания vpn-infra..."
    crontab -l 2>/dev/null | grep -v '/opt/vpn' | crontab - 2>/dev/null
    ok "vpn-infra cron-задания удалены"
else
    info "vpn-infra cron-заданий не найдено"
fi

set -e

# ── 10. vpn-infra setup state — сбросить ─────────────────────────────────────
step "10. Setup state"
set +e

info "Сбрасываем .setup-state и .env.bak..."
rm -f /opt/vpn/.setup-state
rm -f /opt/vpn/.env.bak
ok "setup state очищен (/opt/vpn остался)"

set -e

# ── Итог ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       Home Server Reset Complete         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}SSH:${NC}         password auth enabled, root login enabled"
echo -e "  ${GREEN}nftables:${NC}    policy accept (все порты открыты)"
echo -e "  ${GREEN}Docker:${NC}      контейнеры и образы удалены, br-vpn удалён"
echo -e "  ${GREEN}dnsmasq:${NC}     остановлен, resolv.conf → 1.1.1.1"
echo -e "  ${GREEN}WireGuard:${NC}   wg0/wg1 остановлены"
echo -e "  ${GREEN}routing:${NC}     кастомные ip rules и таблицы очищены"
echo -e "  ${GREEN}fail2ban:${NC}    все IP разбанены, кастомные jail удалены"
echo -e "  ${GREEN}vpn-infra:${NC}   сервисы остановлены, setup state очищен"
echo ""
echo -e "  ${YELLOW}НЕ тронуто:${NC} Docker engine, физическая сеть, /opt/vpn, пароль root"
echo ""
echo -e "  ${CYAN}Теперь запустите setup.sh заново.${NC}"
echo ""

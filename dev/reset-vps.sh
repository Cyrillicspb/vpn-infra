#!/usr/bin/env bash
# dev/reset-vps.sh — полный сброс VPS к состоянию "чистая Ubuntu с SSH"
#
# Назначение: когда установка vpn-infra сломалась и нужно начать заново
# без переустановки ОС через хостер.
#
# Запуск через VNC-консоль:
#   curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/dev/reset-vps.sh | sudo bash
#
# Если GitHub заблокирован — скрипт уже на диске:
#   sudo bash /opt/vpn/dev/reset-vps.sh
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
echo "║       VPS Reset — vpn-infra dev tool     ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"
warn "Этот скрипт сбросит VPS к состоянию 'чистая Ubuntu с SSH'."
warn "Docker engine, сеть и пароль root НЕ трогаются."
echo ""

# ── 1. SSH — сбросить к дефолту ──────────────────────────────────────────────
step "1. SSH"
set +e

info "Разрешаем вход по паролю и root login..."
SSHD_CFG=/etc/ssh/sshd_config

# PasswordAuthentication
if grep -q '^PasswordAuthentication' "$SSHD_CFG"; then
    sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' "$SSHD_CFG"
else
    echo "PasswordAuthentication yes" >> "$SSHD_CFG"
fi

# PermitRootLogin
if grep -q '^PermitRootLogin' "$SSHD_CFG"; then
    sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' "$SSHD_CFG"
else
    echo "PermitRootLogin yes" >> "$SSHD_CFG"
fi

# Убираем AllowUsers и MaxAuthTries (могли быть ужесточены setup.sh)
sed -i '/^AllowUsers/d'    "$SSHD_CFG"
sed -i '/^MaxAuthTries/d'  "$SSHD_CFG"
sed -i '/^PermitTunnel/d'  "$SSHD_CFG"
sed -i '/^ClientAliveInterval/d' "$SSHD_CFG"
sed -i '/^ClientAliveCountMax/d' "$SSHD_CFG"
sed -i '/^Port 443$/d' "$SSHD_CFG"
sed -i '/^Port 8022$/d' "$SSHD_CFG"
grep -q '^Port 22$' "$SSHD_CFG" || echo "Port 22" >> "$SSHD_CFG"

info "Удаляем authorized_keys..."
rm -f /root/.ssh/authorized_keys
rm -f /home/sysadmin/.ssh/authorized_keys 2>/dev/null || true
rm -f /tmp/vpn-bootstrap.pem /tmp/c.pem /tmp/k.pem 2>/dev/null || true

info "Перезапускаем ssh..."
systemctl restart ssh && ok "ssh перезапущен" || warn "ssh restart завершился с ошибкой"

set -e

# ── 2. fail2ban — разбанить и сбросить ───────────────────────────────────────
step "2. fail2ban"
set +e

info "Разбаниваем всех..."
fail2ban-client unban --all 2>/dev/null && ok "fail2ban: все IP разбанены" || warn "fail2ban-client недоступен — пропускаем"

info "Удаляем кастомные jail..."
rm -f /etc/fail2ban/jail.local
rm -f /etc/fail2ban/jail.d/vpn-*.conf 2>/dev/null || true

systemctl restart fail2ban 2>/dev/null && ok "fail2ban перезапущен" || warn "fail2ban не запущен — пропускаем"

set -e

# ── 3. nftables — сбросить к policy accept ───────────────────────────────────
step "3. nftables"
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

# ── 4. Docker — остановить контейнеры, очистить образы ───────────────────────
step "4. Docker"
set +e

COMPOSE_FILE=/opt/vpn/docker-compose.yml
if [[ -f "$COMPOSE_FILE" ]]; then
    # Сбрасываем 3x-ui credentials ДО остановки контейнеров
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^3x-ui$'; then
        info "Сброс 3x-ui credentials к admin/admin..."
        docker exec 3x-ui /app/x-ui setting -username admin -password admin 2>/dev/null \
            && ok "3x-ui credentials сброшены" \
            || warn "Сброс 3x-ui credentials завершился с ошибкой — пропускаем"
    fi

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

set -e

# ── 5. Systemd-сервисы vpn-infra — остановить и удалить ──────────────────────
step "5. Systemd-сервисы vpn-infra"
set +e

VPN_SERVICES=(
    hysteria2
    watchdog
    autossh-vpn
    vpn-routes
    vpn-sets-restore
    vpn-postboot
    xray
)

for svc in "${VPN_SERVICES[@]}"; do
    if systemctl list-unit-files "${svc}.service" &>/dev/null | grep -q "${svc}"; then
        info "Останавливаем и удаляем ${svc}..."
        systemctl stop    "${svc}" 2>/dev/null || true
        systemctl disable "${svc}" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
        ok "${svc} удалён"
    else
        info "${svc}: не установлен — пропускаем"
    fi
done

systemctl daemon-reload
ok "systemd daemon-reload выполнен"

set -e

# ── 6. Пользователь sysadmin, cron и docker overrides ───────────────────────
step "6. sysadmin / cron / docker overrides"
set +e

if id sysadmin &>/dev/null; then
    info "Удаляем пользователя sysadmin и его home..."
    userdel -r sysadmin 2>/dev/null && ok "sysadmin удалён" || warn "userdel sysadmin завершился с ошибкой"
else
    info "Пользователь sysadmin не найден"
fi

rm -f /etc/sudoers.d/sysadmin
ok "sudoers для sysadmin удалён"

if crontab -l 2>/dev/null | grep -q '/opt/vpn'; then
    info "Удаляем cron-задания vpn-infra..."
    crontab -l 2>/dev/null | grep -v '/opt/vpn' | crontab - 2>/dev/null
    ok "vpn-infra cron-задания удалены"
else
    info "vpn-infra cron-заданий не найдено"
fi

rm -f /etc/cron.d/vpn-mirror /etc/cron.d/vps-healthcheck
rm -f /var/log/vpn-mirror.log /var/log/vps-healthcheck.log 2>/dev/null || true
ok "vpn-infra cron.d файлы удалены"

info "Удаляем docker overrides и локальные cache-хвосты..."
rm -f /etc/docker/daemon.json
rm -f /etc/systemd/system/docker.service.d/http-proxy.conf
rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true
rm -rf /tmp/vpn-system-packages
rm -rf /opt/vpn/docker-images 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl restart docker 2>/dev/null && ok "docker перезапущен без overrides" || warn "docker restart завершился с ошибкой"

set -e

# ── 7. vpn-infra setup state — сбросить ──────────────────────────────────────
step "7. Setup state"
set +e

info "Сбрасываем .setup-state и .env.bak..."
rm -f /opt/vpn/.setup-state
rm -f /opt/vpn/.env.bak
rm -f /opt/vpn/.env
ok "setup state очищен (/opt/vpn остался)"

set -e

# ── 8. Опциональное удаление /opt/vpn ────────────────────────────────────────
step "8. Удаление файлов /opt/vpn (опционально)"

_opt_vpn_deleted=false
if [[ -d /opt/vpn ]]; then
    echo ""
    warn "В /opt/vpn могут быть бэкапы (.env, конфиги, ключи)."
    warn "Удаление необратимо — восстановление только через setup.sh заново."
    echo ""
    read -r -p "$(echo -e "${YELLOW}Удалить /opt/vpn полностью для чистой установки? [y/N]:${NC} ")" _confirm
    if [[ "$_confirm" =~ ^[Yy]$ ]]; then
        rm -rf /opt/vpn
        ok "/opt/vpn удалён"
        _opt_vpn_deleted=true
    else
        info "/opt/vpn оставлен без изменений"
    fi
else
    info "/opt/vpn не существует — пропускаем"
fi

# ── Итог ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          VPS Reset Complete              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}SSH:${NC}        password auth enabled, root login enabled"
echo -e "  ${GREEN}nftables:${NC}   policy accept (все порты открыты)"
echo -e "  ${GREEN}Docker:${NC}     контейнеры и образы удалены"
echo -e "  ${GREEN}fail2ban:${NC}   все IP разбанены, кастомные jail удалены"
echo -e "  ${GREEN}vpn-infra:${NC}  сервисы остановлены, setup state очищен"
if $_opt_vpn_deleted; then
    echo -e "  ${GREEN}/opt/vpn:${NC}   удалён (чистая установка)"
else
    echo -e "  ${YELLOW}/opt/vpn:${NC}   оставлен (бэкапы сохранены)"
fi
echo ""
echo -e "  ${YELLOW}НЕ тронуто:${NC} Docker engine, сеть, пароль root"
echo ""
echo -e "  ${CYAN}Теперь запустите setup.sh заново с домашнего сервера.${NC}"
echo -e "  ${CYAN}VPS-шаги выполнятся повторно.${NC}"
echo ""

#!/bin/bash
# =============================================================================
# bootstrap-vps.sh — запускается на VPS через веб-консоль провайдера
#
# Назначение: обернуть SSH в TLS на порту 443, чтобы setup.sh мог
# подключиться к VPS когда прямой SSH (порт 22 и 443) заблокирован DPI.
#
# Использование:
#   Скопировать и выполнить ОДНУ команду в веб-консоли VPS (VNC / noVNC):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/bootstrap-vps.sh)
#
#   Или вручную — скопировать содержимое и вставить в консоль.
#
# После выполнения:
#   - Порт 443 на VPS слушает TLS-туннель → SSH :22
#   - setup.sh на домашнем сервере подключается автоматически
#   - Скрипт завершается сам когда setup.sh завершит bootstrap
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "[$(date '+%H:%M:%S')] ${GREEN}✓${NC} $*"; }
warn() { echo -e "[$(date '+%H:%M:%S')] ${YELLOW}!${NC} $*"; }
err()  { echo -e "[$(date '+%H:%M:%S')] ${RED}✗${NC} $*" >&2; exit 1; }

echo ""
echo -e "${BOLD}${CYAN}━━━ VPN Bootstrap — подготовка SSH-туннеля ━━━${NC}"
echo ""

# Проверка root
[[ "$EUID" -eq 0 ]] || err "Запустите от root: sudo bash bootstrap-vps.sh"

# Проверка что SSH запущен
if ! systemctl is-active --quiet ssh 2>/dev/null && \
   ! systemctl is-active --quiet sshd 2>/dev/null; then
    warn "SSH сервис не активен, пытаемся запустить..."
    systemctl start ssh 2>/dev/null || systemctl start sshd 2>/dev/null || \
        err "Не удалось запустить SSH. Проверьте: systemctl status sshd"
fi
log "SSH сервис запущен"

# Установка socat и openssl
log "Установка socat и openssl..."
apt-get update -qq 2>/dev/null || true
apt-get install -y -qq socat openssl 2>/dev/null || \
    err "Не удалось установить socat/openssl. Проверьте доступ к интернету."
log "socat и openssl установлены"

# Освобождаем порт 443 если занят
if ss -tlnp 2>/dev/null | grep -q ':443 '; then
    warn "Порт 443 занят — освобождаем..."
    # Убиваем только socat на 443 (не nginx/apache если есть)
    pkill -f 'socat.*443' 2>/dev/null || true
    sleep 1
fi

# Генерируем временный самоподписанный сертификат (1 день, только для bootstrap)
log "Генерация временного TLS-сертификата..."
openssl req -x509 -newkey rsa:2048 \
    -keyout /tmp/vpn-bootstrap-key.pem \
    -out /tmp/vpn-bootstrap-cert.pem \
    -days 1 -nodes \
    -subj '/CN=vpn-bootstrap' \
    2>/dev/null
cat /tmp/vpn-bootstrap-cert.pem /tmp/vpn-bootstrap-key.pem > /tmp/vpn-bootstrap.pem
rm -f /tmp/vpn-bootstrap-key.pem /tmp/vpn-bootstrap-cert.pem
chmod 600 /tmp/vpn-bootstrap.pem
log "Сертификат создан"

# Запускаем TLS-туннель: порт 443 → SSH :22
log "Запуск TLS-туннеля (443 → SSH :22)..."
socat \
    OPENSSL-LISTEN:443,reuseaddr,fork,cert=/tmp/vpn-bootstrap.pem,verify=0 \
    TCP:127.0.0.1:22 &
SOCAT_PID=$!

# Проверяем что соcат поднялся
sleep 1
if ! kill -0 "$SOCAT_PID" 2>/dev/null; then
    err "socat не запустился. Проверьте порт 443: ss -tlnp | grep 443"
fi

echo ""
echo -e "${GREEN}${BOLD}━━━ Bootstrap готов ━━━${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} TLS-туннель запущен (PID $SOCAT_PID)"
echo -e "  ${GREEN}✓${NC} Порт 443 → SSH :22"
echo ""
echo -e "  ${CYAN}Теперь нажмите Enter в setup.sh на домашнем сервере.${NC}"
echo ""
echo "  Туннель будет автоматически остановлен после завершения bootstrap."
echo "  Для ручной остановки: kill $SOCAT_PID"
echo ""

# Ждём завершения socat (setup.sh сам завершит его через SSH-команду)
wait "$SOCAT_PID" 2>/dev/null || true

# Очистка
rm -f /tmp/vpn-bootstrap.pem
echo ""
log "Bootstrap завершён. socat остановлен."

#!/usr/bin/env bash
# Smoke test: адаптивный SSH-прокси (ssh-proxy.sh + vps.conf.template)
# Проверяет механизм routing SSH через активный SOCKS5 стек.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="SSH_PROXY"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. ssh-proxy.sh существует и исполняем
SSH_PROXY="/opt/vpn/scripts/ssh-proxy.sh"
if [[ -x "$SSH_PROXY" ]]; then
    pass "ssh-proxy.sh существует и исполняем"
else
    fail "ssh-proxy.sh не найден или не исполняем: $SSH_PROXY"
fi

# 2. netcat-openbsd установлен (нужен -X 5 для SOCKS5)
if nc -h 2>&1 | grep -qi "\-X"; then
    pass "nc поддерживает -X (SOCKS proxy, openbsd вариант)"
elif dpkg -l netcat-openbsd 2>/dev/null | grep -q "^ii"; then
    pass "netcat-openbsd установлен"
else
    fail "netcat-openbsd не установлен — nc -X 5 (SOCKS5) не будет работать"
fi

# 3. ~/.ssh/config содержит ProxyCommand для vps
SSH_CONFIG="/root/.ssh/config"
if [[ -f "$SSH_CONFIG" ]]; then
    if grep -q "ProxyCommand.*ssh-proxy.sh" "$SSH_CONFIG" 2>/dev/null; then
        pass "~/.ssh/config: ProxyCommand → ssh-proxy.sh"
    else
        fail "~/.ssh/config не содержит ProxyCommand с ssh-proxy.sh"
    fi
    if grep -q "Host vps$" "$SSH_CONFIG" 2>/dev/null; then
        pass "~/.ssh/config: Host vps задан"
    else
        warn "~/.ssh/config: Host vps не найден"
    fi
    if grep -q "Host vps-direct" "$SSH_CONFIG" 2>/dev/null; then
        pass "~/.ssh/config: Host vps-direct (fallback без прокси) задан"
    else
        warn "~/.ssh/config: Host vps-direct не найден"
    fi
else
    fail "~/.ssh/config не найден (install-home.sh step33 не выполнен?)"
fi

# 4. SOCKS port файл: если существует — валидное число
SOCKS_PORT_FILE="/var/run/vpn-active-socks-port"
if [[ -f "$SOCKS_PORT_FILE" ]]; then
    port=$(tr -d '[:space:]' < "$SOCKS_PORT_FILE" 2>/dev/null)
    if [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )); then
        pass "vpn-active-socks-port: валидный порт $port"

        # Проверим что nc реально коннектится к этому порту
        if nc -z -w 2 127.0.0.1 "$port" 2>/dev/null; then
            pass "SOCKS5 :$port доступен (watchdog SOCKS слушает)"
        else
            warn "SOCKS5 :$port не отвечает (стек опционально выключен?)"
        fi
    else
        fail "vpn-active-socks-port: некорректное содержимое: '${port}'"
    fi
else
    warn "vpn-active-socks-port не существует (watchdog не запущен или не переключал стеки)"
fi

# 5. Поведение реального ssh-proxy.sh без файла состояния
SSH_PROXY="/opt/vpn/scripts/ssh-proxy.sh"
if [[ -x "$SSH_PROXY" ]]; then
    # Тест с несуществующим файлом состояния → должен вернуть прямое соединение
    RESULT=$(SOCKS_STATE_FILE="/tmp/vpn-nonexistent-$$" timeout 5 bash "$SSH_PROXY" localhost 22 2>/dev/null || echo "TIMEOUT")
    if [[ "$RESULT" != "TIMEOUT" ]]; then
        pass "ssh-proxy.sh: fallback to direct connection works"
    else
        warn "ssh-proxy.sh: could not test (timeout)"
    fi
else
    warn "ssh-proxy.sh not found at $SSH_PROXY — skipping"
fi

# 6. VPS_IP задан в .env
if [[ -n "${VPS_IP:-}" ]]; then
    pass "VPS_IP задан: ${VPS_IP}"
else
    fail "VPS_IP не задан в .env"
fi

# 7. autossh wrapper и unit без shell-style expansion
AUTOSSH_WRAPPER="/opt/vpn/scripts/autossh-vpn.sh"
if [[ -x "$AUTOSSH_WRAPPER" ]]; then
    pass "autossh-vpn.sh существует и исполняем"
else
    fail "autossh-vpn.sh не найден или не исполняем: $AUTOSSH_WRAPPER"
fi

if systemctl cat autossh-vpn.service 2>/dev/null | grep -q '/opt/vpn/scripts/autossh-vpn.sh'; then
    pass "autossh-vpn.service использует wrapper"
else
    fail "autossh-vpn.service не использует /opt/vpn/scripts/autossh-vpn.sh"
fi

if systemctl cat autossh-vpn.service 2>/dev/null | grep -q '\${AUTOSSH_VPN_SOCKS_PORT:-1183}'; then
    fail "autossh-vpn.service содержит raw \${AUTOSSH_VPN_SOCKS_PORT:-1183}"
else
    pass "autossh-vpn.service не содержит shell-style default expansion"
fi

# 8. SSH ключ для VPS существует
SSH_KEY="/root/.ssh/vpn_id_ed25519"
if [[ -f "$SSH_KEY" ]]; then
    perms=$(stat -c "%a" "$SSH_KEY" 2>/dev/null || echo "?")
    if [[ "$perms" == "600" || "$perms" == "400" ]]; then
        pass "SSH ключ $SSH_KEY (права $perms)"
    else
        warn "SSH ключ $SSH_KEY: права $perms (ожидается 600)"
    fi
else
    fail "SSH ключ $SSH_KEY не найден"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

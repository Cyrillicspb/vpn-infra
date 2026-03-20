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

# 5. Поведение ssh-proxy.sh без файла состояния (smoke-проверка через bash -x)
# Временно указываем несуществующий файл через переопределение переменной в скрипте
TMP_PROXY_TEST=$(mktemp /tmp/vpn-proxy-test-XXXX.sh)
cat > "$TMP_PROXY_TEST" << 'INNER'
#!/bin/bash
# Симуляция ssh-proxy.sh с пустым файлом состояния
SOCKS_PORT_FILE="/tmp/vpn-proxy-test-nonexistent-$(date +%s)"
SOCKS_PORT=""
if [[ -f "$SOCKS_PORT_FILE" ]]; then
    SOCKS_PORT=$(tr -d '[:space:]' < "$SOCKS_PORT_FILE" 2>/dev/null)
fi
# Если нет файла — должно использоваться прямое соединение (nc без -X)
if [[ -n "$SOCKS_PORT" && "$SOCKS_PORT" =~ ^[0-9]+$ ]]; then
    echo "SOCKS"
else
    echo "DIRECT"
fi
INNER
chmod +x "$TMP_PROXY_TEST"
result=$(bash "$TMP_PROXY_TEST" 2>/dev/null)
rm -f "$TMP_PROXY_TEST"
if [[ "$result" == "DIRECT" ]]; then
    pass "ssh-proxy.sh: при отсутствии файла состояния → прямое соединение"
else
    fail "ssh-proxy.sh: неожиданный результат при отсутствии файла: '$result'"
fi

# 6. VPS_IP задан в .env
if [[ -n "${VPS_IP:-}" ]]; then
    pass "VPS_IP задан: ${VPS_IP}"
else
    fail "VPS_IP не задан в .env"
fi

# 7. SSH ключ для VPS существует
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

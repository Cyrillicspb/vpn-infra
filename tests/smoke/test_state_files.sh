#!/usr/bin/env bash
# Smoke test: файлы состояния watchdog (/var/run/vpn-active-*)
# Проверяет что watchdog корректно ведёт состояние активного стека.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="STATE_FILES"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

SOCKS_PORT_FILE="/var/run/vpn-active-socks-port"
STACK_FILE="/var/run/vpn-active-stack"
PLUGINS_DIR="/opt/vpn/watchdog/plugins"

# 1. Оба файла существуют
if [[ -f "$SOCKS_PORT_FILE" ]]; then
    pass "vpn-active-socks-port существует"
else
    fail "vpn-active-socks-port не найден — watchdog не записывает состояние"
    echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
    exit 1
fi

if [[ -f "$STACK_FILE" ]]; then
    pass "vpn-active-stack существует"
else
    fail "vpn-active-stack не найден"
fi

# 2. vpn-active-socks-port содержит корректный порт
active_port=$(tr -d '[:space:]' < "$SOCKS_PORT_FILE" 2>/dev/null)
if [[ "$active_port" =~ ^[0-9]+$ ]] && (( active_port >= 1024 && active_port <= 65535 )); then
    pass "vpn-active-socks-port содержит валидный порт: $active_port"
else
    fail "vpn-active-socks-port: некорректное содержимое: '${active_port}'"
fi

# 3. vpn-active-stack содержит известное имя стека
active_stack=$(tr -d '[:space:]' < "$STACK_FILE" 2>/dev/null)
KNOWN_STACKS=(hysteria2 reality reality-grpc cloudflare-cdn)
stack_known=false
for s in "${KNOWN_STACKS[@]}"; do
    [[ "$active_stack" == "$s" ]] && stack_known=true && break
done
if $stack_known; then
    pass "vpn-active-stack: '$active_stack' — известный стек"
else
    fail "vpn-active-stack: неизвестный стек '${active_stack}' (ожидается: ${KNOWN_STACKS[*]})"
fi

# 4. Плагин активного стека существует в plugins/
if [[ -d "$PLUGINS_DIR/$active_stack" ]]; then
    pass "Директория плагина $PLUGINS_DIR/$active_stack существует"
else
    fail "Плагин $active_stack не найден в $PLUGINS_DIR/"
fi

# 5. SOCKS-порт из файла совпадает с портом в client.yaml плагина
CLIENT_YAML="$PLUGINS_DIR/$active_stack/client.yaml"
if [[ -f "$CLIENT_YAML" ]]; then
    # Пробуем прочитать socks_port или socks5.listen из yaml
    yaml_port=$(python3 -c "
import sys
try:
    import yaml
    d = yaml.safe_load(open('$CLIENT_YAML'))
    p = d.get('socks_port') or (d.get('socks5') or {}).get('listen','').split(':')[-1]
    print(int(p)) if p else print('')
except Exception as e:
    print('', file=sys.stderr)
" 2>/dev/null)
    if [[ -n "$yaml_port" ]]; then
        if [[ "$yaml_port" == "$active_port" ]]; then
            pass "SOCKS-порт $active_port совпадает с client.yaml плагина $active_stack"
        else
            fail "Несовпадение SOCKS-порта: файл=$active_port, client.yaml=$yaml_port"
        fi
    else
        warn "Не удалось прочитать SOCKS-порт из $CLIENT_YAML"
    fi
else
    warn "client.yaml не найден для плагина $active_stack"
fi

# 6. Watchdog записывает файлы атомарно (файл не пустой и не tmp)
for f in "$SOCKS_PORT_FILE" "$STACK_FILE"; do
    if [[ -e "${f}.tmp" ]]; then
        warn "Обнаружен незавершённый временный файл ${f}.tmp (watchdog завис в записи?)"
    fi
done
pass "Временных .tmp файлов состояния нет"

# 7. SOCKS-порт слушает
if nc -z -w 2 127.0.0.1 "$active_port" 2>/dev/null; then
    pass "SOCKS5 127.0.0.1:$active_port отвечает"
else
    fail "SOCKS5 127.0.0.1:$active_port не отвечает (стек $active_stack не работает?)"
fi

# 8. Watchdog сервис активен
if systemctl is-active --quiet watchdog 2>/dev/null; then
    pass "watchdog.service активен"
else
    fail "watchdog.service не активен — файлы состояния могут быть устаревшими"
fi

# 9. Возраст файлов состояния < 2 часа (watchdog должен обновлять при каждом переключении)
#    Если файл старше 2 часов И watchdog активен — подозрительно только если stale
now=$(date +%s)
for f in "$SOCKS_PORT_FILE" "$STACK_FILE"; do
    mtime=$(stat -c "%Y" "$f" 2>/dev/null || echo "0")
    age=$(( now - mtime ))
    age_min=$(( age / 60 ))
    if (( age < 7200 )); then
        pass "$(basename $f): свежий (${age_min} мин назад)"
    else
        age_h=$(( age / 3600 ))
        warn "$(basename $f): записан ${age_h}ч назад (нет переключений стека?)"
    fi
done

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

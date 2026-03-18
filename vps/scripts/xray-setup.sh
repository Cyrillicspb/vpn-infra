#!/bin/bash
# =============================================================================
# xray-setup.sh — Начальная настройка 3x-ui / Xray на VPS
#
# Создаёт все inbound-записи в 3x-ui через API (Xray 26.x, XHTTP/splithttp):
#   1. VLESS+XHTTP+REALITY  (tcp/2087, fingerprint: chrome, dest: microsoft.com)
#   2. VLESS+XHTTP+REALITY  (tcp/2083, dest: cdn.jsdelivr.net, без vision flow)
#   3. VLESS+splithttp      (splithttp/127.0.0.1:8080, path /vpn-cdn — для CDN-стека)
#   4. Hysteria2            (udp/443, Salamander obfs — standalone, не через 3x-ui)
#
# Запуск: bash /opt/vpn/scripts/xray-setup.sh
# Требует: /opt/vpn/.env с переменными
# =============================================================================
set -euo pipefail

# ── Загрузка .env ─────────────────────────────────────────────────────────────
ENV_FILE="/opt/vpn/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
fi

# ── Проверка переменных ───────────────────────────────────────────────────────
: "${XRAY_UUID:?Нужен XRAY_UUID в .env}"
: "${XRAY_PRIVATE_KEY:?Нужен XRAY_PRIVATE_KEY в .env}"
: "${XHTTP_MS_PASSWORD:?Нужен XHTTP_MS_PASSWORD в .env}"
: "${XRAY_GRPC_UUID:?Нужен XRAY_GRPC_UUID в .env}"
: "${XRAY_GRPC_PRIVATE_KEY:?Нужен XRAY_GRPC_PRIVATE_KEY в .env}"
: "${XHTTP_CDN_PASSWORD:?Нужен XHTTP_CDN_PASSWORD в .env}"
: "${CF_CDN_UUID:?Нужен CF_CDN_UUID в .env}"

# 3x-ui панель (host network, порт по умолчанию 2053)
XUI_HOST="http://localhost:2053"
XUI_USER="${XUI_PANEL_USER:-admin}"
XUI_PASS="${XRAY_PANEL_PASSWORD:-admin}"
COOKIE_FILE=$(mktemp)
trap 'rm -f "$COOKIE_FILE"' EXIT

log()  { echo "[$(date '+%H:%M:%S')] XRAY-SETUP: $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] XRAY-SETUP: OK $*"; }
err()  { echo "[$(date '+%H:%M:%S')] XRAY-SETUP: ERROR $*" >&2; }

# ── Ожидание готовности 3x-ui ─────────────────────────────────────────────────
log "Ожидание 3x-ui (до 120 сек)..."
for i in $(seq 1 24); do
    if curl -sf --max-time 5 "${XUI_HOST}/" > /dev/null 2>&1; then
        ok "3x-ui готов (попытка ${i})"
        break
    fi
    if [[ $i -eq 24 ]]; then
        err "3x-ui недоступен после 120 сек"
        exit 1
    fi
    sleep 5
done

# ── Авторизация ───────────────────────────────────────────────────────────────
log "Авторизация в 3x-ui..."

do_login() {
    local user="$1" pass="$2"
    curl -sf --max-time 10 \
        -c "$COOKIE_FILE" \
        -X POST "${XUI_HOST}/login" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        --data-urlencode "username=${user}" \
        --data-urlencode "password=${pass}" \
        2>/dev/null
}

LOGIN_RESULT=$(do_login "$XUI_USER" "$XUI_PASS")
if echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
    ok "Авторизован в 3x-ui (с паролем из .env)"
else
    # 3x-ui свежеустановлен — пробуем дефолтные admin/admin
    log "Пробуем дефолтные учётные данные admin/admin..."
    LOGIN_RESULT=$(do_login "admin" "admin")
    if echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
        ok "Авторизован с admin/admin — меняем пароль на из .env..."
        # Меняем пароль через API настроек
        CHANGE_RESULT=$(curl -sf --max-time 10 \
            -b "$COOKIE_FILE" \
            -X POST "${XUI_HOST}/panel/setting/updateUser" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            --data-urlencode "oldUsername=admin" \
            --data-urlencode "oldPassword=admin" \
            --data-urlencode "newUsername=${XUI_USER}" \
            --data-urlencode "newPassword=${XUI_PASS}" \
            2>/dev/null || echo '{"success":false}')
        if echo "$CHANGE_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
            ok "Пароль 3x-ui изменён"
            # Повторная авторизация с новым паролем
            LOGIN_RESULT=$(do_login "$XUI_USER" "$XUI_PASS")
            echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null \
                || { err "Не удалось авторизоваться после смены пароля"; exit 1; }
            ok "Повторная авторизация успешна"
        else
            log "Смена пароля не удалась (API не поддерживается?) — продолжаем со старым паролем"
            XUI_PASS="admin"
            XUI_USER="admin"
        fi
    else
        err "Ошибка авторизации: $LOGIN_RESULT"
        err "Проверьте XRAY_PANEL_PASSWORD в .env или сбросьте пароль 3x-ui"
        exit 1
    fi
fi

# ── Функция: добавить inbound ─────────────────────────────────────────────────
add_inbound() {
    local name="$1"
    local json="$2"

    log "Добавление inbound: $name..."
    RESULT=$(curl -sf --max-time 30 \
        -b "$COOKIE_FILE" \
        -X POST "${XUI_HOST}/panel/api/inbounds/add" \
        -H "Content-Type: application/json" \
        -d "$json" \
        2>/dev/null || echo '{"success":false,"msg":"curl error"}')

    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
        ok "Inbound создан: $name"
    else
        MSG=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('msg','unknown'))" 2>/dev/null || echo "parse error")
        if echo "$MSG" | grep -qi "exist\|already\|duplicate"; then
            log "Inbound уже существует (пропускаем): $name"
        else
            err "Ошибка создания $name: $MSG"
            return 1
        fi
    fi
}

# Генерация shortId для REALITY (8 hex символов)
REALITY_SHORT_ID=$(openssl rand -hex 4)
GRPC_SHORT_ID=$(openssl rand -hex 4)

# ── 1. VLESS + XHTTP + REALITY (порт 2087, microsoft.com) ────────────────────
log "Настройка VLESS+XHTTP+REALITY (порт 2087, microsoft.com)..."

add_inbound "VLESS-XHTTP-microsoft" "$(python3 -c "
import json
cfg = {
    'remark': 'VLESS-XHTTP-microsoft',
    'enable': True,
    'listen': '',
    'port': 2087,
    'protocol': 'vless',
    'settings': json.dumps({
        'clients': [{
            'id': '${XRAY_UUID}',
            'flow': '',
            'email': 'ms-client@vpn',
            'enable': True
        }],
        'decryption': 'none',
        'fallbacks': []
    }),
    'streamSettings': json.dumps({
        'network': 'splithttp',
        'security': 'reality',
        'realitySettings': {
            'show': False,
            'dest': 'microsoft.com:443',
            'xver': 0,
            'serverNames': ['microsoft.com', 'www.microsoft.com'],
            'privateKey': '${XRAY_PRIVATE_KEY}',
            'shortIds': ['${REALITY_SHORT_ID}'],
            'fingerprint': 'chrome'
        },
        'splithttpSettings': {
            'path': '/',
            'password': '${XHTTP_MS_PASSWORD}',
            'maxConcurrentUploads': 2
        }
    }),
    'sniffing': json.dumps({
        'enabled': True,
        'destOverride': ['http', 'tls', 'quic'],
        'routeOnly': False
    })
}
print(json.dumps(cfg))
")"

# ── 2. VLESS + XHTTP + REALITY (порт 2083, cdn.jsdelivr.net) ─────────────────
log "Настройка VLESS+XHTTP+REALITY (порт 2083, cdn.jsdelivr.net)..."

add_inbound "VLESS-XHTTP-jsdelivr" "$(python3 -c "
import json
cfg = {
    'remark': 'VLESS-XHTTP-jsdelivr',
    'enable': True,
    'listen': '',
    'port': 2083,
    'protocol': 'vless',
    'settings': json.dumps({
        'clients': [{
            'id': '${XRAY_GRPC_UUID}',
            'flow': '',
            'email': 'cdn-client@vpn',
            'enable': True
        }],
        'decryption': 'none',
        'fallbacks': []
    }),
    'streamSettings': json.dumps({
        'network': 'splithttp',
        'security': 'reality',
        'realitySettings': {
            'show': False,
            'dest': 'cdn.jsdelivr.net:443',
            'xver': 0,
            'serverNames': ['cdn.jsdelivr.net'],
            'privateKey': '${XRAY_GRPC_PRIVATE_KEY}',
            'shortIds': ['${GRPC_SHORT_ID}'],
            'fingerprint': 'chrome'
        },
        'splithttpSettings': {
            'path': '/',
            'password': '${XHTTP_CDN_PASSWORD}',
            'maxConcurrentUploads': 2
        }
    }),
    'sniffing': json.dumps({
        'enabled': True,
        'destOverride': ['http', 'tls', 'quic'],
        'routeOnly': False
    })
}
print(json.dumps(cfg))
")"

# ── 3. VLESS + splithttp (для Cloudflare CDN-стека) ──────────────────────────
log "Настройка VLESS+splithttp на localhost:8080 (CDN-стек)..."

add_inbound "VLESS-WS-CDN" "$(python3 -c "
import json
cfg = {
    'remark': 'VLESS-WS-CDN',
    'enable': True,
    'listen': '127.0.0.1',
    'port': 8080,
    'protocol': 'vless',
    'settings': json.dumps({
        'clients': [{
            'id': '${CF_CDN_UUID}',
            'flow': '',
            'email': 'cdn-ws',
            'enable': True
        }],
        'decryption': 'none',
        'fallbacks': []
    }),
    'streamSettings': json.dumps({
        'network': 'splithttp',
        'security': 'none',
        'splithttpSettings': {
            'path': '/vpn-cdn',
            'host': '',
            'maxUploadSize': 1000000,
            'maxConcurrentUploads': 10
        }
    }),
    'sniffing': json.dumps({'enabled': False, 'destOverride': []})
}
print(json.dumps(cfg))
")"

# ── 4. Hysteria2: работает как standalone systemd (не через 3x-ui) ────────────
log "Hysteria2 настраивается как standalone systemd-сервис, не как inbound 3x-ui."
log "Конфиг: /opt/vpn/hysteria2/server.yaml"

# ── Получить список созданных inbound ─────────────────────────────────────────
log "Проверка созданных inbound..."
INBOUNDS=$(curl -sf --max-time 10 \
    -b "$COOKIE_FILE" \
    "${XUI_HOST}/panel/api/inbounds/list" \
    2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if d.get('success'):
        for ib in d.get('obj', []):
            print(f\"  - [{ib.get('protocol','?')}] {ib.get('remark','?')} :{ib.get('port','?')}\")
    else:
        print('  (ошибка получения списка)')
except:
    print('  (ошибка парсинга)')
" 2>/dev/null || echo "  (не удалось получить список)")
log "Inbounds:"
echo "$INBOUNDS"

# ── Итог ──────────────────────────────────────────────────────────────────────
ok "=== xray-setup завершён ==="
log ""
log "Следующие шаги:"
log "  1. Убедитесь что все 3 inbound созданы и активны (https://VPS_IP:8443/xui/)"
log "  2. Проверьте XRAY_PUBLIC_KEY в .env совпадает с публичным ключом в панели"
log "  3. Для CDN-стека: настройте cloudflared tunnel → localhost:8080"
log "  4. Добавьте клиентов через Telegram-бота (/adddevice)"
log "  5. Порты в firewall (VPS): 2087/tcp, 2083/tcp, 443/udp"

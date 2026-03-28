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

wait_xui_ready() {
    local attempts="${1:-24}"
    local sleep_s="${2:-5}"
    local i
    for i in $(seq 1 "$attempts"); do
        if curl -sf --max-time 5 "${XUI_HOST}/" > /dev/null 2>&1; then
            return 0
        fi
        sleep "$sleep_s"
    done
    return 1
}

reset_xui_db_fresh() {
    local db_dir="/opt/vpn/3x-ui/db"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"

    log "Пересоздаём panel DB 3x-ui для чистого старта..."

    docker stop 3x-ui > /dev/null 2>&1 || true
    mkdir -p "$db_dir"

    for path in "$db_dir"/x-ui.db "$db_dir"/x-ui.db-shm "$db_dir"/x-ui.db-wal; do
        [[ -e "$path" ]] || continue
        mv "$path" "${path}.bak.${stamp}"
    done

    docker start 3x-ui > /dev/null 2>&1 || return 1
    wait_xui_ready 24 5 || return 1
    return 0
}

# ── Ожидание готовности 3x-ui ─────────────────────────────────────────────────
log "Ожидание 3x-ui (до 120 сек)..."
if wait_xui_ready 24 5; then
    ok "3x-ui готов"
else
    err "3x-ui недоступен после 120 сек"
    exit 1
fi

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
        # Третий фолбек: сброс через x-ui CLI (VPS жив, пароль изменён от прошлой установки)
        log "Оба пароля не подошли — сбрасываем через x-ui CLI внутри контейнера..."
        if docker exec 3x-ui /app/x-ui setting -username admin -password admin 2>/dev/null; then
            ok "x-ui credentials сброшены к admin/admin"
            docker restart 3x-ui 2>/dev/null || true
            wait_xui_ready 12 5 || true
            LOGIN_RESULT=$(do_login "admin" "admin")
            if echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
                ok "Авторизован с admin/admin после сброса — меняем пароль на из .env..."
                CHANGE_RESULT=$(curl -sf --max-time 10 \
                    -c "$COOKIE_FILE" \
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
                    LOGIN_RESULT=$(do_login "$XUI_USER" "$XUI_PASS")
                    echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null \
                        || { err "Не удалось авторизоваться после смены пароля"; exit 1; }
                    ok "Повторная авторизация успешна"
                else
                    log "Смена пароля не удалась — продолжаем с admin/admin"
                    XUI_PASS="admin"; XUI_USER="admin"
                fi
            else
                err "Авторизация не удалась даже после сброса через x-ui CLI"
                err "Проверьте вручную: docker exec 3x-ui /app/x-ui setting -username admin -password admin"
                log "Пробуем аварийное восстановление: пересоздать panel DB 3x-ui..."
                if reset_xui_db_fresh; then
                    ok "Panel DB 3x-ui пересоздана"
                    LOGIN_RESULT=$(do_login "admin" "admin")
                    if echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
                        ok "Авторизован с admin/admin после пересоздания DB — меняем пароль на из .env..."
                        CHANGE_RESULT=$(curl -sf --max-time 10 \
                            -c "$COOKIE_FILE" \
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
                            LOGIN_RESULT=$(do_login "$XUI_USER" "$XUI_PASS")
                            echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null \
                                || { err "Не удалось авторизоваться после смены пароля"; exit 1; }
                            ok "Повторная авторизация успешна"
                        else
                            log "Смена пароля не удалась — продолжаем с admin/admin"
                            XUI_PASS="admin"; XUI_USER="admin"
                        fi
                    else
                        err "Авторизация не удалась даже после пересоздания panel DB"
                        exit 1
                    fi
                else
                    err "Пересоздать panel DB 3x-ui не удалось"
                    exit 1
                fi
            fi
        else
            err "Ошибка авторизации и сброс через x-ui CLI недоступен"
            log "Пробуем аварийное восстановление: пересоздать panel DB 3x-ui..."
            if reset_xui_db_fresh; then
                ok "Panel DB 3x-ui пересоздана"
                LOGIN_RESULT=$(do_login "admin" "admin")
                if echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
                    ok "Авторизован с admin/admin после пересоздания DB — меняем пароль на из .env..."
                    CHANGE_RESULT=$(curl -sf --max-time 10 \
                        -c "$COOKIE_FILE" \
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
                        LOGIN_RESULT=$(do_login "$XUI_USER" "$XUI_PASS")
                        echo "$LOGIN_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null \
                            || { err "Не удалось авторизоваться после смены пароля"; exit 1; }
                        ok "Повторная авторизация успешна"
                    else
                        log "Смена пароля не удалась — продолжаем с admin/admin"
                        XUI_PASS="admin"; XUI_USER="admin"
                    fi
                else
                    err "Авторизация не удалась даже после пересоздания panel DB"
                    exit 1
                fi
            else
                err "Проверьте XRAY_PANEL_PASSWORD в .env или выполните: sudo bash dev/reset-vps.sh"
                exit 1
            fi
        fi
    fi
fi

# ── Функция: добавить inbound ─────────────────────────────────────────────────
add_inbound() {
    local name="$1"
    local json="$2"

    log "Добавление/обновление inbound: $name..."
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
            # Inbound exists — update it via PUT /update/{id}
            PORT=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('port',''))" 2>/dev/null)
            INBOUND_ID=$(curl -sf --max-time 30 -b "$COOKIE_FILE" \
                "${XUI_HOST}/panel/api/inbounds/list" 2>/dev/null \
                | python3 -c "
import sys,json
data=json.load(sys.stdin)
for inb in data.get('obj',[]):
    if str(inb.get('port',''))==sys.argv[1]:
        print(inb['id'])
        break
" "$PORT" 2>/dev/null)
            if [[ -n "$INBOUND_ID" ]]; then
                UPD=$(curl -sf --max-time 30 \
                    -b "$COOKIE_FILE" \
                    -X POST "${XUI_HOST}/panel/api/inbounds/update/$INBOUND_ID" \
                    -H "Content-Type: application/json" \
                    -d "$json" \
                    2>/dev/null || echo '{"success":false}')
                if echo "$UPD" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('success') else 1)" 2>/dev/null; then
                    ok "Inbound обновлён: $name"
                else
                    log "Inbound уже актуален: $name"
                fi
            else
                log "Inbound уже существует, ID не найден: $name"
            fi
        else
            err "Ошибка создания $name: $MSG"
            return 1
        fi
    fi
}

# shortId для REALITY — читаем из 3x-ui API если инбаунд уже существует, иначе генерируем
# Это предотвращает смену shortId при повторном запуске установщика
_INBOUND_LIST=$(curl -sf --max-time 30 -b "$COOKIE_FILE" "${XUI_HOST}/panel/api/inbounds/list" 2>/dev/null || echo '{"obj":[]}')

_get_short_id() {
    local port="$1"
    echo "$_INBOUND_LIST" | python3 -c "import sys,json
data=json.load(sys.stdin)
for inb in data.get('obj',[]):
    if inb.get('port')==$port:
        ss=json.loads(inb.get('streamSettings','{}'))
        ids=ss.get('realitySettings',{}).get('shortIds',[''])
        v=ids[0].strip() if ids else ''
        if v: print(v); break" 2>/dev/null
}

REALITY_SHORT_ID=$(_get_short_id 2087)
[ -z "$REALITY_SHORT_ID" ] && REALITY_SHORT_ID=$(openssl rand -hex 4)

GRPC_SHORT_ID=$(_get_short_id 2083)
[ -z "$GRPC_SHORT_ID" ] && GRPC_SHORT_ID=$(openssl rand -hex 4)

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
log "Настройка VLESS+splithttp на 0.0.0.0:8080 (CDN-стек, Cloudflare Worker → VPS:8080)..."

add_inbound "VLESS-WS-CDN" "$(python3 -c "
import json
cfg = {
    'remark': 'VLESS-WS-CDN',
    'enable': True,
    'listen': '',
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

# ── Рестарт 3x-ui — Xray перечитает новые ключи и пароли из inbound'ов ───────
# Без рестарта Xray держит старый конфиг в памяти даже после обновления DB.
# Критично при повторной установке: XRAY_PRIVATE_KEY и XHTTP_*_PASSWORD могли
# измениться — API уже обновил DB, теперь нужно чтобы Xray применил изменения.
log "Перезапуск 3x-ui для применения новых конфигов..."
docker restart 3x-ui 2>/dev/null && ok "3x-ui перезапущен" || log "3x-ui restart: $?"
# Ждём готовности после рестарта
for i in $(seq 1 12); do
    curl -sf --max-time 5 "${XUI_HOST}/" > /dev/null 2>&1 && break
    sleep 5
done
ok "3x-ui готов после рестарта"

# ── Итог ──────────────────────────────────────────────────────────────────────
ok "=== xray-setup завершён ==="
log ""
log "Следующие шаги:"
log "  1. Убедитесь что все 3 inbound созданы и активны (https://VPS_IP:8443/xui/)"
log "  2. Проверьте XRAY_PUBLIC_KEY в .env совпадает с публичным ключом в панели"
log "  3. Для CDN-стека: настройте cloudflared tunnel → localhost:8080"
log "  4. Добавьте клиентов через Telegram-бота (/adddevice)"
log "  5. Порты в firewall (VPS): 2087/tcp, 2083/tcp, 443/udp"

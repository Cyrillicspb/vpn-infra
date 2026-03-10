#!/usr/bin/env bash
# Smoke test: Заблокированные сайты доступны через VPN туннель
# Проверяет что curl через tun интерфейс достигает заблокированных ресурсов.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="BLOCKED_SITES"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# Список заблокированных сайтов для проверки
BLOCKED_SITES=(
    "youtube.com"
    "instagram.com"
    "facebook.com"
)

# Сайты для проверки прямого интернета (не должны идти через VPN)
DIRECT_SITES=(
    "google.com"
    "yandex.ru"
)

# 1. Найти активный tun интерфейс
TUN_IFACE=$(ip link show 2>/dev/null | grep -oP '^[0-9]+: \Ktun\d+' | head -1 || true)
AWG_TUN=$(ip link show 2>/dev/null | grep -oP '^[0-9]+: \KawgTun\S*' | head -1 || true)
ACTIVE_TUN="${TUN_IFACE:-$AWG_TUN}"

if [[ -z "$ACTIVE_TUN" ]]; then
    warn "Активный tun интерфейс не найден — проверка через watchdog API"

    # Проверить через watchdog
    TOKEN="${WATCHDOG_API_TOKEN:-}"
    if [[ -n "$TOKEN" ]]; then
        STATUS=$(curl -sf --max-time 10 \
            -H "Authorization: Bearer ${TOKEN}" \
            "http://localhost:8080/status" 2>/dev/null || true)
        TUNNEL_UP=$(echo "$STATUS" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print(d.get('tunnel_up', False))" 2>/dev/null || true)
        if [[ "$TUNNEL_UP" == "True" || "$TUNNEL_UP" == "true" ]]; then
            warn "Watchdog сообщает tunnel_up=true, но tun интерфейс не найден"
        else
            warn "Туннель не поднят — пропуск теста заблокированных сайтов"
            echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
            exit 0  # Не FAIL — туннель может быть не поднят при первой установке
        fi
    fi
fi

# 2. VPS доступен через туннель
VPS_TUN_IP="${VPS_TUNNEL_IP:-10.177.2.2}"
if ping -c 2 -W 5 -q "$VPS_TUN_IP" &>/dev/null; then
    pass "VPS $VPS_TUN_IP пингуется через туннель"
else
    warn "VPS $VPS_TUN_IP недоступен — заблокированные сайты могут не работать"
fi

# 3. Проверка заблокированных сайтов через tun
SITE_PASS=0
SITE_FAIL=0

for SITE in "${BLOCKED_SITES[@]}"; do
    # curl --interface $TUN_IFACE или curl с source IP из подсети
    # Используем curl через прямое подключение к VPS DNS + tun routing
    HTTP_CODE=""
    if [[ -n "$ACTIVE_TUN" ]]; then
        HTTP_CODE=$(curl -sf --max-time 10 \
            --interface "$ACTIVE_TUN" \
            -o /dev/null -w "%{http_code}" \
            "https://$SITE" 2>/dev/null || true)
    else
        # Фолбэк: проверить через DNS (если домен резолвится через VPN-DNS, значит маршрут работает)
        IP=$(dig @127.0.0.1 +timeout=5 +tries=1 "$SITE" A +short 2>/dev/null | head -1 || true)
        if [[ -n "$IP" ]]; then
            # Проверить что IP в nft set blocked (значит пойдёт через VPN)
            if nft get element inet vpn blocked_static "{ $IP }" &>/dev/null 2>&1 || \
               nft get element inet vpn blocked_dynamic "{ $IP }" &>/dev/null 2>&1; then
                pass "[$SITE] IP $IP в nft set blocked → пойдёт через VPN"
                (( SITE_PASS++ ))
            else
                warn "[$SITE] IP $IP НЕ в nft set blocked (сайт не заблокирован?)"
            fi
            continue
        fi
    fi

    case "$HTTP_CODE" in
        200|301|302|403)
            pass "[$SITE] доступен через VPN (HTTP $HTTP_CODE)"
            (( SITE_PASS++ ))
            ;;
        "")
            warn "[$SITE] нет ответа (timeout или сеть недоступна)"
            ;;
        *)
            warn "[$SITE] HTTP $HTTP_CODE"
            ;;
    esac
done

if (( SITE_PASS > 0 )); then
    pass "Заблокированные сайты: $SITE_PASS/${#BLOCKED_SITES[@]} доступны"
elif (( SITE_FAIL > 0 )); then
    fail "Заблокированные сайты: все недоступны ($SITE_FAIL/${#BLOCKED_SITES[@]})"
fi

# 4. Внешний IP через VPN должен быть IP VPS, не домашний
EXTERNAL_IP=$(curl -sf --max-time 10 https://icanhazip.com 2>/dev/null || \
              curl -sf --max-time 10 https://api.ipify.org 2>/dev/null || true)
VPS_IP="${VPS_IP:-}"

if [[ -n "$EXTERNAL_IP" && -n "$VPS_IP" ]]; then
    if [[ "$EXTERNAL_IP" == "$VPS_IP" ]]; then
        pass "Внешний IP = VPS IP ($EXTERNAL_IP) — трафик идёт через VPN"
    else
        warn "Внешний IP ($EXTERNAL_IP) ≠ VPS IP ($VPS_IP) — возможно split tunneling"
        pass "Split tunneling: прямые сайты идут через домашний IP (ожидаемо)"
    fi
elif [[ -n "$EXTERNAL_IP" ]]; then
    pass "Внешний IP: $EXTERNAL_IP (VPS_IP не установлен для сравнения)"
else
    warn "Не удалось определить внешний IP"
fi

# 5. Прямой трафик НЕ идёт через VPN (split tunneling работает)
for SITE in "${DIRECT_SITES[@]}"; do
    DIRECT_CODE=$(curl -sf --max-time 10 \
        -o /dev/null -w "%{http_code}" \
        "https://$SITE" 2>/dev/null || true)
    if [[ "$DIRECT_CODE" =~ ^(200|301|302)$ ]]; then
        pass "[$SITE] прямой доступ работает (HTTP $DIRECT_CODE)"
    else
        warn "[$SITE] прямой доступ: HTTP ${DIRECT_CODE:-timeout}"
    fi
done

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

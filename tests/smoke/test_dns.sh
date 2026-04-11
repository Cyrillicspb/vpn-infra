#!/usr/bin/env bash
# Smoke test: DNS (dnsmasq)
# Проверяет что dnsmasq работает, разрешает домены через VPN-DNS и через upstream.
set -uo pipefail

source /opt/vpn/.env 2>/dev/null || true

PASS=0; FAIL=0; WARN=0
TEST_NAME="DNS"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# 1. dnsmasq запущен
if systemctl is-active --quiet dnsmasq; then
    pass "dnsmasq.service активен"
else
    fail "dnsmasq.service не запущен"
fi

# 2. dnsmasq слушает на порту 53
if ss -ulnp 2>/dev/null | grep -q ':53 '; then
    pass "dnsmasq слушает на порту 53"
else
    fail "Порт 53 не занят (dnsmasq не слушает)"
fi

# 3. Резолв незаблокированного домена
if dig @127.0.0.1 +timeout=3 +tries=1 google.com A &>/dev/null; then
    GOOGLE_IP=$(dig @127.0.0.1 +timeout=3 +tries=1 google.com A +short 2>/dev/null | head -1)
    pass "Резолв google.com через dnsmasq: $GOOGLE_IP"
else
    fail "Резолв google.com через dnsmasq не работает"
fi

# 4. Резолв заблокированного домена через VPN-DNS
YOUTUBE_IP=$(dig @127.0.0.1 +timeout=5 +tries=1 youtube.com A +short 2>/dev/null | head -1)
if [[ -n "$YOUTUBE_IP" ]]; then
    pass "Резолв youtube.com вернул IP: $YOUTUBE_IP"
else
    warn "Резолв youtube.com не вернул IP (VPS недоступен или домен не в списке)"
fi

# 5. dnsmasq.conf существует и содержит nftset директивы
# Конфиг копируется установщиком в /etc/dnsmasq.conf
DNSMASQ_CONF="/etc/dnsmasq.conf"
[[ ! -f "$DNSMASQ_CONF" ]] && DNSMASQ_CONF="/opt/vpn/dnsmasq/dnsmasq.conf"
if [[ -f "$DNSMASQ_CONF" ]]; then
    pass "Конфиг dnsmasq существует"
    if grep -rqP 'nftset=/[^/]+/4#inet#[a-z_]+#(blocked_dynamic|dpi_direct)' /etc/dnsmasq.d/ 2>/dev/null || \
       grep -qP 'nftset=/[^/]+/4#inet#[a-z_]+#(blocked_dynamic|dpi_direct)' "$DNSMASQ_CONF" 2>/dev/null; then
        pass "dnsmasq nftset directives configured (correct format)"
    else
        fail "nftset directives missing or invalid format in dnsmasq config"
    fi
else
    fail "Конфиг $DNSMASQ_CONF не найден"
fi

# 6. vpn-domains.conf существует и непустой
VPN_DOMAINS=""
for candidate in /etc/dnsmasq.d/vpn-domains.conf /opt/vpn/dnsmasq/dnsmasq.d/vpn-domains.conf; do
    if [[ -f "$candidate" ]]; then
        VPN_DOMAINS="$candidate"
        break
    fi
done
if [[ -n "$VPN_DOMAINS" ]]; then
    DOMAIN_COUNT=$(grep -c "^server=" "$VPN_DOMAINS" 2>/dev/null || echo 0)
    if (( DOMAIN_COUNT > 0 )); then
        pass "vpn-domains.conf содержит $DOMAIN_COUNT записей ($VPN_DOMAINS)"
    else
        warn "vpn-domains.conf пуст (нет server= записей)"
    fi
else
    warn "vpn-domains.conf не найден (создаётся при первом обновлении маршрутов)"
fi

# 7. DNS сервера указывает на 127.0.0.1, systemd-resolved отключён,
# dnsmasq не пытается регистрироваться через resolvconf/resolve1
if grep -q "^nameserver 127.0.0.1" /etc/resolv.conf 2>/dev/null; then
    pass "DNS сервера указывает на 127.0.0.1 (/etc/resolv.conf)"
else
    warn "DNS может не идти через dnsmasq (проверьте /etc/resolv.conf)"
fi

if systemctl is-enabled systemd-resolved 2>/dev/null | grep -q '^masked$'; then
    pass "systemd-resolved masked"
elif ! systemctl is-enabled systemd-resolved 2>/dev/null | grep -q '^enabled$'; then
    pass "systemd-resolved disabled"
else
    fail "systemd-resolved всё ещё enabled (конфликтует с dnsmasq)"
fi

if grep -q '^IGNORE_RESOLVCONF=yes$' /etc/default/dnsmasq 2>/dev/null; then
    pass "dnsmasq ignores resolvconf upstream integration"
else
    fail "dnsmasq не настроен с IGNORE_RESOLVCONF=yes"
fi

if grep -q '^DNSMASQ_EXCEPT=\"lo\"$' /etc/default/dnsmasq 2>/dev/null; then
    pass "dnsmasq исключён из resolvconf system-resolver registration"
else
    fail "dnsmasq не настроен с DNSMASQ_EXCEPT=\"lo\""
fi

# 8. Скрипт прогрева DNS кэша существует
if [[ -f "/opt/vpn/scripts/dns-warmup.sh" ]]; then
    pass "Скрипт прогрева DNS кэша существует"
else
    warn "dns-warmup.sh не найден"
fi

# 8.1 В gateway mode LAN DNS должен принудительно редиректиться в локальный dnsmasq
if [[ "${SERVER_MODE:-}" == "gateway" ]]; then
    LAN_DNS_IFACE="${LAN_IFACE:-}"
    if [[ -z "$LAN_DNS_IFACE" ]]; then
        warn "Gateway Mode: LAN_IFACE не задан, пропуск проверки DNS redirect"
    elif nft list ruleset 2>/dev/null | grep -F 'udp dport 53 redirect to :53' | grep -Fq "iifname \"${LAN_DNS_IFACE}\""; then
        pass "Gateway Mode: LAN UDP DNS redirect → local dnsmasq"
    else
        fail "Gateway Mode: отсутствует UDP DNS redirect для LAN"
    fi
    if [[ -z "$LAN_DNS_IFACE" ]]; then
        :
    elif nft list ruleset 2>/dev/null | grep -F 'tcp dport 53 redirect to :53' | grep -Fq "iifname \"${LAN_DNS_IFACE}\""; then
        pass "Gateway Mode: LAN TCP DNS redirect → local dnsmasq"
    else
        fail "Gateway Mode: отсутствует TCP DNS redirect для LAN"
    fi
fi

CRITICAL_BLOCKED_DOMAINS_FILE="/opt/vpn/home/config/critical-blocked-domains.txt"
if [[ -f "$CRITICAL_BLOCKED_DOMAINS_FILE" ]]; then
    pass "critical blocked domains list существует"
    if grep -qx "api.telegram.org" "$CRITICAL_BLOCKED_DOMAINS_FILE"; then
        pass "critical blocked domains: api.telegram.org присутствует"
    else
        fail "critical blocked domains: api.telegram.org отсутствует"
    fi
else
    fail "critical blocked domains list не найден"
fi

TG_IP="$(dig @127.0.0.1 api.telegram.org +short +time=5 +tries=1 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)"
if [[ -n "$TG_IP" ]]; then
    pass "api.telegram.org резолвится в $TG_IP"
    if nft get element inet vpn blocked_dynamic "{ $TG_IP }" &>/dev/null 2>&1 || \
       nft get element inet vpn blocked_static "{ $TG_IP }" &>/dev/null 2>&1; then
        pass "api.telegram.org IP в blocked nft set"
    else
        fail "api.telegram.org IP не попал в blocked nft set"
    fi
else
    fail "api.telegram.org не резолвится через dnsmasq"
fi

# 9. dnsmasq НЕ логирует DNS-запросы (privacy)
if ! grep -qE '^[[:space:]]*log-queries' "$DNSMASQ_CONF" 2>/dev/null; then
    pass "dnsmasq: log-queries disabled (privacy)"
else
    warn "dnsmasq: log-queries enabled — DNS-запросы логируются (нарушение приватности)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

#!/usr/bin/env bash
# Smoke test: DNS (dnsmasq)
# Проверяет что dnsmasq работает, разрешает домены через VPN-DNS и через upstream.
set -uo pipefail

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
    if grep -q "nftset=" "$DNSMASQ_CONF" 2>/dev/null; then
        pass "nftset= директивы присутствуют в конфиге"
    else
        fail "nftset= директивы отсутствуют в dnsmasq.conf"
    fi
else
    fail "Конфиг $DNSMASQ_CONF не найден"
fi

# 6. vpn-domains.conf существует и непустой
VPN_DOMAINS="/opt/vpn/dnsmasq/dnsmasq.d/vpn-domains.conf"
if [[ -f "$VPN_DOMAINS" ]]; then
    DOMAIN_COUNT=$(grep -c "^server=" "$VPN_DOMAINS" 2>/dev/null || echo 0)
    if (( DOMAIN_COUNT > 0 )); then
        pass "vpn-domains.conf содержит $DOMAIN_COUNT записей"
    else
        warn "vpn-domains.conf пуст (нет server= записей)"
    fi
else
    warn "vpn-domains.conf не найден (создаётся при первом обновлении маршрутов)"
fi

# 7. DNS сервера указывает на 127.0.0.1
if grep -q "^nameserver 127.0.0.1" /etc/resolv.conf 2>/dev/null; then
    pass "DNS сервера указывает на 127.0.0.1 (/etc/resolv.conf)"
elif resolvectl status 2>/dev/null | grep -q "127.0.0.1"; then
    pass "DNS сервера указывает на 127.0.0.1 (systemd-resolved)"
else
    warn "DNS может не идти через dnsmasq (проверьте /etc/resolv.conf)"
fi

# 8. Скрипт прогрева DNS кэша существует
if [[ -f "/opt/vpn/scripts/dns-warmup.sh" ]]; then
    pass "Скрипт прогрева DNS кэша существует"
else
    warn "dns-warmup.sh не найден"
fi

# 9. dnsmasq НЕ логирует DNS-запросы (privacy)
if grep -q "log-queries" "$DNSMASQ_CONF" 2>/dev/null; then
    if grep -q "#log-queries" "$DNSMASQ_CONF" 2>/dev/null; then
        pass "log-queries закомментирован (приватность соблюдена)"
    else
        warn "log-queries включён — DNS-запросы логируются (нарушение приватности)"
    fi
else
    pass "log-queries не установлен (приватность: DNS-запросы не логируются)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

#!/bin/bash
# Тест: DNS резолвинг через dnsmasq
set -euo pipefail

# dnsmasq запущен?
systemctl is-active --quiet dnsmasq || { echo "dnsmasq не запущен"; exit 1; }

# Резолвинг работает?
result=$(dig @127.0.0.1 google.com +short +time=5 2>/dev/null)
[[ -n "$result" ]] || { echo "DNS не резолвит google.com"; exit 1; }

# Резолвинг заблокированного (если есть в конфиге)
result2=$(dig @127.0.0.1 youtube.com +short +time=5 2>/dev/null)
[[ -n "$result2" ]] || echo "WARN: youtube.com не резолвится (возможно не настроен nftset)"

echo "DNS: OK (google.com → $result)"
exit 0

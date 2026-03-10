#!/bin/bash
# Тест: Kill switch — nftables правила присутствуют
set -euo pipefail

# Проверяем правила forward chain
RULES=$(nft list chain inet filter forward 2>/dev/null)

# Kill switch правило: src VPN + blocked → только через tun
echo "$RULES" | grep -q "blocked_static\|blocked_dynamic" || {
    echo "Kill switch правила для blocked sets не найдены в forward chain"
    exit 1
}

echo "$RULES" | grep -qE "DROP|drop" || {
    echo "WARN: DROP правила не найдены в forward chain"
}

# Проверяем mangle chain (fwmark)
MANGLE=$(nft list chain inet mangle prerouting 2>/dev/null)
echo "$MANGLE" | grep -q "0x1\|mark set" || {
    echo "fwmark правила не найдены в mangle prerouting"
    exit 1
}

# Проверяем policy routing
ip rule show | grep -q "fwmark 0x1 lookup 200" || {
    echo "ip rule fwmark 0x1 → table 200 не настроен"
    exit 1
}

echo "Kill switch: OK"
exit 0

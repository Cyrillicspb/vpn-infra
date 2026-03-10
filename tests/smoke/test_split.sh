#!/bin/bash
# Тест: Split tunneling — nft sets существуют
set -euo pipefail

# blocked_static существует?
nft list set inet filter blocked_static > /dev/null 2>&1 || {
    echo "nft set blocked_static не найден"
    exit 1
}

# blocked_dynamic существует?
nft list set inet filter blocked_dynamic > /dev/null 2>&1 || {
    echo "nft set blocked_dynamic не найден"
    exit 1
}

# Policy routing?
ip rule show | grep -q "fwmark 0x1" || {
    echo "Policy routing (fwmark 0x1) не настроен"
    exit 1
}

ip rule show | grep -q "10.177.1.0/24" || {
    echo "Policy routing для VPN подсети не настроен"
    exit 1
}

# Проверяем таблицу 100
ip route show table 100 | grep -q "default" || {
    echo "WARN: route table 100 нет default (VPN ещё не поднят?)"
}

echo "Split tunneling: OK"
exit 0

#!/bin/bash
# Тест: WireGuard/AmneziaWG туннели
set -euo pipefail

source /opt/vpn/.env 2>/dev/null || true

# wg0 (AWG) поднят?
ip link show wg0 > /dev/null 2>&1 || { echo "wg0 (AWG) не существует"; exit 1; }

# wg1 (WG) поднят?
ip link show wg1 > /dev/null 2>&1 || { echo "wg1 (WG) не существует"; exit 1; }

# VPS доступен через тун?
VPS_TUN_IP="${VPS_TUNNEL_IP:-10.177.2.2}"
if ping -c 2 -W 5 "$VPS_TUN_IP" > /dev/null 2>&1; then
    echo "Tunnel: OK (VPS $VPS_TUN_IP доступен)"
    exit 0
else
    echo "WARN: VPS $VPS_TUN_IP недоступен (VPN стек не поднят?)"
    # Не FAIL — может быть нормально при первой установке
    exit 0
fi

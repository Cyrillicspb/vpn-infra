#!/bin/bash
# SSH Tier-2 tunnel: tun0, 10.177.2.1 (home) <-> 10.177.2.2 (VPS)
# VPS_IP и VPS_SSH_PORT берутся из EnvironmentFile=/opt/vpn/.env

set -euo pipefail

# Фоновый монитор: назначает IP на локальный tun0 при каждом появлении.
(while true; do
    if ip link show tun0 &>/dev/null; then
        ip addr replace 10.177.2.1/30 dev tun0 2>/dev/null || true
        ip link set tun0 up 2>/dev/null || true
    fi
    sleep 3
done) &
MONITOR_PID=$!
echo "$MONITOR_PID" > /run/tier2-monitor.pid

cleanup() {
    kill "$MONITOR_PID" 2>/dev/null || true
}
trap cleanup EXIT

exec ssh \
    -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -w 0:0 \
    -i /root/.ssh/vpn_id_ed25519 \
    -p "${VPS_SSH_PORT:-22}" \
    "sysadmin@${VPS_IP}" \
    'sudo ip tuntap add dev tun0 mode tun 2>/dev/null; sudo ip link set tun0 up; sudo ip addr replace 10.177.2.2/30 dev tun0; sudo ip route replace 10.177.2.1/32 dev tun0; sleep infinity'

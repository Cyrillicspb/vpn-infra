#!/bin/bash
# SSH Tier-2 tunnel: tun0, 10.177.2.1 (home) <-> 10.177.2.2 (VPS)
# По умолчанию использует /opt/vpn/.env, но при наличии
# /run/vpn-active-backend.env переключается на текущий active backend.

set -euo pipefail

if [[ -f /opt/vpn/.env ]]; then
    # shellcheck disable=SC1091
    source /opt/vpn/.env
fi

if [[ -f /run/vpn-active-backend.env ]]; then
    # shellcheck disable=SC1091
    source /run/vpn-active-backend.env
fi

TIER2_VPS_USER="${TIER2_VPS_USER:-root}"

cleanup_local_tun() {
    ip link del tun0 2>/dev/null || true
    ip tuntap del dev tun0 mode tun 2>/dev/null || true
}

cleanup_remote_tun() {
    ssh \
        -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=3 \
        -o ConnectTimeout=15 \
        -i /root/.ssh/vpn_id_ed25519 \
        -p "${VPS_SSH_PORT:-22}" \
        "${TIER2_VPS_USER}@${VPS_IP}" \
        'sudo ip link delete tun0 2>/dev/null || sudo ip tuntap del dev tun0 mode tun 2>/dev/null || true'
}

cleanup_local_tun
cleanup_remote_tun || true

# Фоновый монитор: назначает IP на локальный tun0 при каждом появлении.
(while true; do
    if ip link show tun0 &>/dev/null; then
        ip addr replace 10.177.2.1/30 dev tun0 2>/dev/null || true
        ip link set tun0 up 2>/dev/null || true
        ip route replace 10.177.2.0/30 dev tun0 2>/dev/null || true
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
    "${TIER2_VPS_USER}@${VPS_IP}" \
    'sudo ip link set tun0 up; sudo ip addr replace 10.177.2.2/30 dev tun0; sudo ip route replace 10.177.2.1/32 dev tun0; sleep infinity'

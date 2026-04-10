#!/usr/bin/env bash
# autossh-vpn.sh — deterministic wrapper for fallback management SOCKS.
# systemd does not support shell-style ${VAR:-default} expansion inside ExecStart,
# so we resolve env/defaults here before exec'ing autossh.

set -euo pipefail

ENV_FILE="/opt/vpn/.env"
ACTIVE_BACKEND_ENV="/run/vpn-active-backend.env"

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

if [[ -f "$ACTIVE_BACKEND_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$ACTIVE_BACKEND_ENV"
fi

AUTOSSH_GATETIME="${AUTOSSH_GATETIME:-0}"
AUTOSSH_PORT="${AUTOSSH_PORT:-0}"
AUTOSSH_LOGFILE="${AUTOSSH_LOGFILE:-/var/log/vpn-autossh.log}"
AUTOSSH_VPN_SOCKS_PORT="${AUTOSSH_VPN_SOCKS_PORT:-1183}"
VPS_SSH_PORT="${VPS_SSH_PORT:-22}"
VPS_IP="${VPS_IP:-}"

export AUTOSSH_GATETIME AUTOSSH_PORT AUTOSSH_LOGFILE AUTOSSH_VPN_SOCKS_PORT

if [[ -z "$VPS_IP" ]]; then
    echo "autossh-vpn: VPS_IP is not set" >&2
    exit 64
fi

if [[ ! "$AUTOSSH_VPN_SOCKS_PORT" =~ ^[0-9]+$ ]]; then
    echo "autossh-vpn: invalid AUTOSSH_VPN_SOCKS_PORT=$AUTOSSH_VPN_SOCKS_PORT" >&2
    exit 64
fi

if [[ ! "$VPS_SSH_PORT" =~ ^[0-9]+$ ]]; then
    echo "autossh-vpn: invalid VPS_SSH_PORT=$VPS_SSH_PORT" >&2
    exit 64
fi

exec /usr/bin/autossh -M 0 \
    -o "ServerAliveInterval=30" \
    -o "ServerAliveCountMax=3" \
    -o "ExitOnForwardFailure=yes" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "BatchMode=yes" \
    -o "ConnectTimeout=15" \
    -i /root/.ssh/vpn_id_ed25519 \
    -N \
    -D "127.0.0.1:${AUTOSSH_VPN_SOCKS_PORT}" \
    -p "${VPS_SSH_PORT}" \
    "sysadmin@${VPS_IP}"

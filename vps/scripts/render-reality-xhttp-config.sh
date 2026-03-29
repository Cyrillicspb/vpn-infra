#!/bin/bash
set -euo pipefail

ENV_FILE="/opt/vpn/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found" >&2
    exit 1
fi

set -o allexport
# shellcheck disable=SC1090
source "$ENV_FILE"
set +o allexport

XRAY_XHTTP_UUID="${XRAY_XHTTP_UUID:-${XRAY_GRPC_UUID:-}}"
XRAY_XHTTP_PRIVATE_KEY="${XRAY_XHTTP_PRIVATE_KEY:-${XRAY_GRPC_PRIVATE_KEY:-}}"
XRAY_XHTTP_SHORT_ID="${XRAY_XHTTP_SHORT_ID:-${XRAY_GRPC_SHORT_ID:-}}"

: "${XRAY_XHTTP_UUID:?XRAY_XHTTP_UUID or XRAY_GRPC_UUID is required}"
: "${XRAY_XHTTP_PRIVATE_KEY:?XRAY_XHTTP_PRIVATE_KEY or XRAY_GRPC_PRIVATE_KEY is required}"
: "${XRAY_XHTTP_SHORT_ID:?XRAY_XHTTP_SHORT_ID or XRAY_GRPC_SHORT_ID is required}"
: "${XHTTP_CDN_PASSWORD:?XHTTP_CDN_PASSWORD is required}"

sudo install -d -m 755 /opt/vpn/xray
tmp_config="$(mktemp /tmp/reality-xhttp.XXXXXX.json)"
trap 'rm -f "$tmp_config"' EXIT

cat > "$tmp_config" <<EOF
{
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "0.0.0.0",
        "port": 2083,
        "protocol": "vless",
        "settings": {
            "clients": [{
                "id": "${XRAY_XHTTP_UUID}",
                "flow": "",
                "email": "xhttp-client@vpn"
            }],
            "decryption": "none"
        },
        "streamSettings": {
            "network": "xhttp",
            "security": "reality",
            "realitySettings": {
                "show": false,
                "target": "cdn.jsdelivr.net:443",
                "xver": 0,
                "serverNames": ["cdn.jsdelivr.net"],
                "privateKey": "${XRAY_XHTTP_PRIVATE_KEY}",
                "shortIds": ["${XRAY_XHTTP_SHORT_ID}"],
                "fingerprint": "chrome"
            },
            "xhttpSettings": {
                "path": "/",
                "password": "${XHTTP_CDN_PASSWORD}",
                "mode": "packet-up"
            }
        },
        "sniffing": {
            "enabled": true,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly": false
        }
    }],
    "outbounds": [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "blackhole", "tag": "block"}
    ]
}
EOF

sudo install -m 644 "$tmp_config" /opt/vpn/xray/reality-xhttp.json

echo "OK: /opt/vpn/xray/reality-xhttp.json rendered"

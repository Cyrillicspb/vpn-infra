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

XRAY_VISION_UUID="${XRAY_VISION_UUID:-${XRAY_XHTTP_UUID:-${XRAY_GRPC_UUID:-}}}"
XRAY_VISION_PRIVATE_KEY="${XRAY_VISION_PRIVATE_KEY:-${XRAY_XHTTP_PRIVATE_KEY:-${XRAY_GRPC_PRIVATE_KEY:-}}}"
XRAY_VISION_SHORT_ID="${XRAY_VISION_SHORT_ID:-${XRAY_XHTTP_SHORT_ID:-${XRAY_GRPC_SHORT_ID:-}}}"

: "${XRAY_VISION_UUID:?XRAY_VISION_UUID or XRAY_XHTTP_UUID is required}"
: "${XRAY_VISION_PRIVATE_KEY:?XRAY_VISION_PRIVATE_KEY or XRAY_XHTTP_PRIVATE_KEY is required}"
: "${XRAY_VISION_SHORT_ID:?XRAY_VISION_SHORT_ID or XRAY_XHTTP_SHORT_ID is required}"

mkdir -p /opt/vpn/xray

cat > /opt/vpn/xray/reality-vision.json <<EOF
{
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "0.0.0.0",
        "port": 443,
        "protocol": "vless",
        "settings": {
            "clients": [{
                "id": "${XRAY_VISION_UUID}",
                "flow": "xtls-rprx-vision",
                "email": "vision-client@vpn"
            }],
            "decryption": "none"
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": false,
                "target": "www.microsoft.com:443",
                "xver": 0,
                "serverNames": ["www.microsoft.com"],
                "privateKey": "${XRAY_VISION_PRIVATE_KEY}",
                "shortIds": ["${XRAY_VISION_SHORT_ID}"],
                "fingerprint": "chrome"
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

echo "OK: /opt/vpn/xray/reality-vision.json rendered"

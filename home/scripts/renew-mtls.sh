#!/usr/bin/env bash
# renew-mtls.sh — обновление mTLS сертификатов
#
# Использование (вызывается из Telegram-бота):
#   renew-mtls.sh client   — выпустить новый клиентский сертификат (для браузера / устройства)
#   renew-mtls.sh ca       — перевыпустить CA (редко, TTL 10 лет)
#
# CA и ключи хранятся на VPS: /opt/vpn/nginx/mtls/
# Скрипт подключается к VPS через Tier-2 WireGuard туннель (10.177.2.2).
# Готовый .p12 файл сохраняется в /tmp/mtls-client-<date>.p12 и выводится путь.

set -euo pipefail

MODE="${1:-client}"

SSH_KEY="/root/.ssh/vpn_id_ed25519"
VPS_HOST="10.177.2.2"
VPS_USER="sysadmin"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"

MTLS_DIR="/opt/vpn/nginx/mtls"
DATE="$(date +%Y%m%d-%H%M%S)"
P12_REMOTE="/tmp/client-${DATE}.p12"
P12_LOCAL="/tmp/mtls-client-${DATE}.p12"

# Проверка туннеля
if ! ssh $SSH_OPTS "${VPS_USER}@${VPS_HOST}" "echo ok" &>/dev/null; then
    echo "ERROR: VPS недоступен через туннель (10.177.2.2). Проверьте wg-tier2."
    exit 1
fi

case "$MODE" in

    client)
        echo "Выпуск клиентского сертификата mTLS..."

        ssh $SSH_OPTS "${VPS_USER}@${VPS_HOST}" bash <<EOF
set -euo pipefail

MTLS_DIR="${MTLS_DIR}"
DATE="${DATE}"
KEY="\${MTLS_DIR}/client-\${DATE}.key"
CSR="\${MTLS_DIR}/client-\${DATE}.csr"
CRT="\${MTLS_DIR}/client-\${DATE}.crt"
P12="${P12_REMOTE}"

# Генерация ключа и запроса
openssl genrsa -out "\$KEY" 2048 2>/dev/null
openssl req -new \
    -key "\$KEY" \
    -out "\$CSR" \
    -subj "/CN=vpn-admin-${DATE}/O=VPNInfra/C=RU" 2>/dev/null

# Подпись нашим CA (TTL 730 дней)
openssl x509 -req -days 730 \
    -in "\$CSR" \
    -CA "\${MTLS_DIR}/ca.crt" \
    -CAkey "\${MTLS_DIR}/ca.key" \
    -CAcreateserial \
    -out "\$CRT" 2>/dev/null

# Упаковка в .p12 без пароля (для удобства импорта)
openssl pkcs12 -export \
    -in "\$CRT" \
    -inkey "\$KEY" \
    -certfile "\${MTLS_DIR}/ca.crt" \
    -out "\$P12" \
    -passout pass: 2>/dev/null

# Удалить временные файлы
rm -f "\$KEY" "\$CSR" "\$CRT"
echo "OK: \$P12"
EOF

        # Скопировать .p12 на домашний сервер
        scp $SSH_OPTS "${VPS_USER}@${VPS_HOST}:${P12_REMOTE}" "${P12_LOCAL}" 2>/dev/null
        ssh $SSH_OPTS "${VPS_USER}@${VPS_HOST}" "rm -f ${P12_REMOTE}" 2>/dev/null || true

        echo "CERT_PATH=${P12_LOCAL}"
        echo "Клиентский сертификат готов: ${P12_LOCAL}"
        echo "Импортируйте .p12 в браузер или Keychain (macOS) / Certificates (Windows)."
        echo "Затем откройте https://${VPS_HOST}:8443 — браузер предложит выбрать сертификат."
        ;;

    ca)
        echo "Перевыпуск CA (корневого сертификата)..."
        echo "ВНИМАНИЕ: после этого все существующие клиентские сертификаты станут недействительными."
        echo "Потребуется перевыпустить клиентские сертификаты через /renew-cert."

        ssh $SSH_OPTS "${VPS_USER}@${VPS_HOST}" bash <<EOF
set -euo pipefail
MTLS_DIR="${MTLS_DIR}"

# Бэкап старого CA
cp "\${MTLS_DIR}/ca.key" "\${MTLS_DIR}/ca.key.bak.${DATE}" 2>/dev/null || true
cp "\${MTLS_DIR}/ca.crt" "\${MTLS_DIR}/ca.crt.bak.${DATE}" 2>/dev/null || true

# Новый CA (4096 bit, 10 лет)
openssl genrsa -out "\${MTLS_DIR}/ca.key" 4096 2>/dev/null
openssl req -new -x509 -days 3650 \
    -key "\${MTLS_DIR}/ca.key" \
    -out "\${MTLS_DIR}/ca.crt" \
    -subj "/CN=VPN-CA/O=VPNInfra/C=RU" 2>/dev/null
chmod 600 "\${MTLS_DIR}/ca.key"

# Перевыпустить серверный сертификат nginx
openssl genrsa -out /opt/vpn/nginx/ssl/server.key 2048 2>/dev/null
openssl req -new \
    -key /opt/vpn/nginx/ssl/server.key \
    -out /opt/vpn/nginx/ssl/server.csr \
    -subj "/CN=vpn-server/O=VPNInfra/C=RU" 2>/dev/null
openssl x509 -req -days 730 \
    -in /opt/vpn/nginx/ssl/server.csr \
    -CA "\${MTLS_DIR}/ca.crt" \
    -CAkey "\${MTLS_DIR}/ca.key" \
    -CAcreateserial \
    -out /opt/vpn/nginx/ssl/server.crt 2>/dev/null
rm -f /opt/vpn/nginx/ssl/server.csr
chmod 600 /opt/vpn/nginx/ssl/server.key

# Перезапустить nginx
docker restart nginx 2>/dev/null || true
echo "OK: CA перевыпущен, nginx перезапущен"
EOF

        echo "CA обновлён. Выпустите новый клиентский сертификат: /renew-cert"
        ;;

    *)
        echo "Использование: $0 [client|ca]"
        exit 1
        ;;
esac

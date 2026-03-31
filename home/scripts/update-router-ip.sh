#!/bin/bash
# update-router-ip.sh — обновление внешнего IP роутера в nft set для Gateway Mode
#
# Запускается из cron (каждые 5 минут), только если SERVER_MODE=gateway.
# Сравнивает текущий внешний IP с тем, что в nft set router_external_ips.
# При изменении — обновляет set и .env.
#
# Использование:
#   bash update-router-ip.sh [--force]

set -euo pipefail

ENV_FILE="/opt/vpn/.env"
LOG_FILE="/var/log/vpn-router-ip.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] update-router-ip: $*" | tee -a "$LOG_FILE"
}

# Загрузить .env
[[ -f "$ENV_FILE" ]] || { log "SKIP: $ENV_FILE не найден"; exit 0; }
# shellcheck disable=SC1090
set -o allexport
source "$ENV_FILE"
set +o allexport

# Только для Gateway Mode
[[ "${SERVER_MODE:-hosted}" == "gateway" ]] || { log "SKIP: не gateway mode"; exit 0; }

FORCE="${1:-}"

# ── Получить текущий внешний IP ───────────────────────────────────────────────

CURRENT_IP=""
for endpoint in "https://api.ipify.org" "https://ifconfig.me" "https://ipv4.icanhazip.com"; do
    CURRENT_IP=$(curl -4 -sf --max-time 8 --interface "${NET_INTERFACE:-}" "$endpoint" 2>/dev/null | tr -d '[:space:]' || true)
    [[ -n "$CURRENT_IP" ]] && break
done

if [[ -z "$CURRENT_IP" ]]; then
    log "WARN: не удалось определить внешний IP"
    exit 0
fi

# Базовая валидация IPv4
if ! echo "$CURRENT_IP" | grep -qP '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'; then
    log "WARN: некорректный IP: $CURRENT_IP"
    exit 0
fi

# ── Получить IP из nft set ────────────────────────────────────────────────────

NFT_IP=$(nft list set inet vpn router_external_ips 2>/dev/null \
    | grep -oP '\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}' | head -1 || true)

STORED_IP="${ROUTER_EXTERNAL_IP:-}"

# Если нет изменений — выходим
if [[ "$FORCE" != "--force" && "$CURRENT_IP" == "$NFT_IP" && "$CURRENT_IP" == "$STORED_IP" ]]; then
    log "OK: IP не изменился ($CURRENT_IP)"
    exit 0
fi

log "Изменение IP: nft=${NFT_IP:-none} env=${STORED_IP:-none} → current=$CURRENT_IP"

# ── Обновить nft set ──────────────────────────────────────────────────────────

if nft list table inet vpn &>/dev/null; then
    # Flush + add — атомарная замена
    if nft flush set inet vpn router_external_ips 2>/dev/null; then
        nft add element inet vpn router_external_ips "{ $CURRENT_IP }" 2>/dev/null \
            || log "WARN: не удалось добавить $CURRENT_IP в nft set"
        log "nft set router_external_ips обновлён → $CURRENT_IP"
    else
        log "WARN: nft flush set завершился с ошибкой (set может не существовать)"
    fi
else
    log "WARN: таблица inet vpn не найдена"
fi

# ── Обновить .env ─────────────────────────────────────────────────────────────

if grep -q "^ROUTER_EXTERNAL_IP=" "$ENV_FILE"; then
    sed -i "s/^ROUTER_EXTERNAL_IP=.*/ROUTER_EXTERNAL_IP=${CURRENT_IP}/" "$ENV_FILE"
else
    echo "ROUTER_EXTERNAL_IP=${CURRENT_IP}" >> "$ENV_FILE"
fi

log "ROUTER_EXTERNAL_IP=$CURRENT_IP записан в $ENV_FILE"

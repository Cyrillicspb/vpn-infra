#!/usr/bin/env bash
# Smoke test: консистентность ключей и секретов
# Проверяет что ключи в конфигах совпадают с .env (единственный источник правды).
set -uo pipefail

ENV_FILE="/opt/vpn/.env"
source "$ENV_FILE" 2>/dev/null || { echo "  [FAIL] $ENV_FILE не найден"; exit 1; }

# Guard: XRAY_INBOUNDS может не быть определён в .env
if [[ -n "${XRAY_INBOUNDS[*]+x}" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        warn "XRAY_INBOUNDS entry: $line (неожиданная переменная в .env)"
    done <<< "${XRAY_INBOUNDS[@]}"
fi

PASS=0; FAIL=0; WARN=0
TEST_NAME="KEY_CONSISTENCY"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# ── 1. WG0 (AmneziaWG): приватный ключ → производим публичный → сравниваем с .env ──
WG0_CONF="/etc/wireguard/wg0.conf"
if [[ -f "$WG0_CONF" ]]; then
    wg0_priv=$(awk '/^PrivateKey/{print $3; exit}' "$WG0_CONF" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$wg0_priv" ]]; then
        warn "wg0.conf: PrivateKey пуст"
    elif [[ -n "$wg0_priv" ]]; then
        wg0_derived_pub=$(echo "$wg0_priv" | wg pubkey 2>/dev/null | tr -d '[:space:]')
        env_pub="${AWG_SERVER_PUBLIC_KEY:-}"
        if [[ -z "$env_pub" ]]; then
            warn "AWG_SERVER_PUBLIC_KEY не задан в .env"
        elif [[ "$wg0_derived_pub" == "$env_pub" ]]; then
            pass "wg0: приватный ключ совпадает с AWG_SERVER_PUBLIC_KEY"
        else
            fail "wg0: производный публичный ключ НЕ совпадает с AWG_SERVER_PUBLIC_KEY в .env"
            echo "       wg0.conf  → $wg0_derived_pub"
            echo "       .env      → $env_pub"
        fi
    else
        warn "wg0.conf: не удалось прочитать PrivateKey"
    fi
else
    warn "wg0.conf не найден (AWG не установлен?)"
fi

# ── 2. WG1 (WireGuard): аналогично ─────────────────────────────────────────────────
WG1_CONF="/etc/wireguard/wg1.conf"
if [[ -f "$WG1_CONF" ]]; then
    wg1_priv=$(awk '/^PrivateKey/{print $3; exit}' "$WG1_CONF" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$wg1_priv" ]]; then
        warn "wg1.conf: PrivateKey пуст"
    elif [[ -n "$wg1_priv" ]]; then
        wg1_derived_pub=$(echo "$wg1_priv" | wg pubkey 2>/dev/null | tr -d '[:space:]')
        env_pub="${WG_SERVER_PUBLIC_KEY:-}"
        if [[ -z "$env_pub" ]]; then
            warn "WG_SERVER_PUBLIC_KEY не задан в .env"
        elif [[ "$wg1_derived_pub" == "$env_pub" ]]; then
            pass "wg1: приватный ключ совпадает с WG_SERVER_PUBLIC_KEY"
        else
            fail "wg1: производный публичный ключ НЕ совпадает с WG_SERVER_PUBLIC_KEY в .env"
            echo "       wg1.conf  → $wg1_derived_pub"
            echo "       .env      → $env_pub"
        fi
    else
        warn "wg1.conf: не удалось прочитать PrivateKey"
    fi
else
    warn "wg1.conf не найден (WG не установлен?)"
fi

# ── 3. AWG junk-параметры присутствуют в wg0.conf ──────────────────────────────────
if [[ -f "$WG0_CONF" ]]; then
    for param in H1 H2 H3 H4; do
        env_val="${!param:-}"   # AWG_H1 → нет, ищем H1? Нет, переменные AWG_H1..
        : # handled below
    done
    for param in AWG_H1 AWG_H2 AWG_H3 AWG_H4; do
        val="${!param:-}"
        if [[ -z "$val" ]]; then
            warn "$param не задан в .env"
            continue
        fi
        # Имя поля в конфиге: H1, H2, H3, H4
        field="${param#AWG_}"   # H1, H2, H3, H4
        if grep -q "^${field} = ${val}$" "$WG0_CONF" 2>/dev/null; then
            pass "wg0: $param ($val) совпадает с конфигом"
        else
            fail "wg0: $param из .env ($val) не найден в wg0.conf"
        fi
    done
fi

# ── 4. XRAY REALITY: публичный ключ в конфиге клиента ──────────────────────────────
XRAY_CONF="/opt/vpn/xray/config-reality.json"
if [[ -f "$XRAY_CONF" ]]; then
    env_pub="${XRAY_PUBLIC_KEY:-}"
    if [[ -z "$env_pub" ]]; then
        warn "XRAY_PUBLIC_KEY не задан в .env"
    elif grep -q "$env_pub" "$XRAY_CONF" 2>/dev/null; then
        pass "xray reality: XRAY_PUBLIC_KEY совпадает с конфигом"
    else
        fail "xray reality: XRAY_PUBLIC_KEY из .env не найден в $XRAY_CONF"
    fi
    env_uuid="${XRAY_UUID:-}"
    if [[ -n "$env_uuid" ]] && ! grep -q "$env_uuid" "$XRAY_CONF" 2>/dev/null; then
        fail "xray reality: XRAY_UUID из .env не найден в $XRAY_CONF"
    elif [[ -n "$env_uuid" ]]; then
        pass "xray reality: XRAY_UUID совпадает с конфигом"
    fi
else
    warn "$XRAY_CONF не найден (Xray не установлен?)"
fi

# ── 5. XRAY gRPC: публичный ключ в конфиге клиента ─────────────────────────────────
XRAY_GRPC_CONF="/opt/vpn/xray/config-grpc.json"
if [[ -f "$XRAY_GRPC_CONF" ]]; then
    env_pub="${XRAY_GRPC_PUBLIC_KEY:-}"
    if [[ -z "$env_pub" ]]; then
        warn "XRAY_GRPC_PUBLIC_KEY не задан в .env"
    elif grep -q "$env_pub" "$XRAY_GRPC_CONF" 2>/dev/null; then
        pass "xray grpc: XRAY_GRPC_PUBLIC_KEY совпадает с конфигом"
    else
        fail "xray grpc: XRAY_GRPC_PUBLIC_KEY из .env не найден в $XRAY_GRPC_CONF"
    fi
else
    warn "$XRAY_GRPC_CONF не найден (Xray gRPC не установлен?)"
fi

# ── 6. Hysteria2: auth в клиентском конфиге ─────────────────────────────────────────
HYSTERIA_CONF="/etc/hysteria/config.yaml"
if [[ -f "$HYSTERIA_CONF" ]]; then
    env_auth="${HYSTERIA2_AUTH:-}"
    if [[ -z "$env_auth" ]]; then
        warn "HYSTERIA2_AUTH не задан в .env"
    elif grep -q "$env_auth" "$HYSTERIA_CONF" 2>/dev/null; then
        pass "hysteria2: HYSTERIA2_AUTH совпадает с конфигом"
    else
        fail "hysteria2: HYSTERIA2_AUTH из .env не найден в $HYSTERIA_CONF"
    fi
else
    warn "$HYSTERIA_CONF не найден (Hysteria2 не установлена?)"
fi

# ── 7. Watchdog API token: присутствует в docker-compose ────────────────────────────
COMPOSE_FILE="/opt/vpn/docker-compose.yml"
if [[ -f "$COMPOSE_FILE" ]]; then
    env_token="${WATCHDOG_API_TOKEN:-}"
    if [[ -z "$env_token" ]]; then
        warn "WATCHDOG_API_TOKEN не задан в .env"
    elif grep -q "$env_token" "$COMPOSE_FILE" 2>/dev/null; then
        pass "watchdog: WATCHDOG_API_TOKEN найден в docker-compose.yml"
    else
        # Может быть в .env файле контейнера, не в compose напрямую
        warn "WATCHDOG_API_TOKEN не найден в docker-compose.yml (может читаться из .env)"
    fi
else
    warn "$COMPOSE_FILE не найден"
fi

# ── 8. Все обязательные переменные заданы в .env ────────────────────────────────────
REQUIRED_VARS=(
    AWG_SERVER_PRIVATE_KEY AWG_SERVER_PUBLIC_KEY
    WG_SERVER_PRIVATE_KEY WG_SERVER_PUBLIC_KEY
    AWG_H1 AWG_H2 AWG_H3 AWG_H4
    XRAY_UUID XRAY_PRIVATE_KEY XRAY_PUBLIC_KEY
    XRAY_GRPC_UUID XRAY_GRPC_PRIVATE_KEY XRAY_GRPC_PUBLIC_KEY
    HYSTERIA2_AUTH HYSTERIA2_OBFS_PASSWORD
    WATCHDOG_API_TOKEN BACKUP_GPG_PASSPHRASE
)
missing=()
for var in "${REQUIRED_VARS[@]}"; do
    [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -eq 0 ]]; then
    pass "Все обязательные секреты заданы в .env (${#REQUIRED_VARS[@]} переменных)"
else
    fail "Отсутствуют в .env: ${missing[*]}"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

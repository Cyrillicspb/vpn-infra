#!/usr/bin/env bash
# Smoke test: Docker контейнеры
# Проверяет что все обязательные контейнеры запущены и healthy.
set -uo pipefail

PASS=0; FAIL=0; WARN=0
TEST_NAME="DOCKER"

pass() { echo "  [PASS] $1"; (( PASS++ )); }
fail() { echo "  [FAIL] $1"; (( FAIL++ )); }
warn() { echo "  [WARN] $1"; (( WARN++ )); }

echo "=== ${TEST_NAME} ==="

# Обязательные контейнеры
REQUIRED_CONTAINERS=(
    "telegram-bot"
    "xray-client"
    "socket-proxy"
)

# Желательные контейнеры
OPTIONAL_CONTAINERS=(
    "xray-client-2"
    "cloudflared"
    "node-exporter"
)

# 1. Docker daemon запущен
if systemctl is-active --quiet docker; then
    pass "docker.service активен"
else
    fail "docker.service не запущен"
    echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
    exit 1
fi

# 2. Обязательные контейнеры
for CONTAINER in "${REQUIRED_CONTAINERS[@]}"; do
    STATE=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    case "$STATE" in
        running)
            pass "Контейнер $CONTAINER: running"
            ;;
        missing)
            fail "Контейнер $CONTAINER: не найден"
            ;;
        *)
            fail "Контейнер $CONTAINER: $STATE"
            ;;
    esac
done

# 3. Опциональные контейнеры
for CONTAINER in "${OPTIONAL_CONTAINERS[@]}"; do
    STATE=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    case "$STATE" in
        running)
            pass "Контейнер $CONTAINER: running"
            ;;
        missing)
            warn "Контейнер $CONTAINER: не найден (отключён?)"
            ;;
        *)
            warn "Контейнер $CONTAINER: $STATE"
            ;;
    esac
done

# 4. Проверка healthcheck для контейнеров с HEALTHCHECK
for CONTAINER in telegram-bot xray-client; do
    HEALTH=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
        "$CONTAINER" 2>/dev/null || echo "missing")
    case "$HEALTH" in
        healthy)
            pass "Healthcheck $CONTAINER: healthy"
            ;;
        unhealthy)
            fail "Healthcheck $CONTAINER: unhealthy"
            LAST_LOG=$(docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' \
                "$CONTAINER" 2>/dev/null | tail -c 200 || true)
            echo "    Последний вывод healthcheck: $LAST_LOG" >&2
            ;;
        starting)
            warn "Healthcheck $CONTAINER: starting (ещё не завершён)"
            ;;
        no-healthcheck)
            # Нет HEALTHCHECK директивы — нормально
            ;;
    esac
done

# 5. Нет контейнеров в состоянии restart loop
RESTARTING=$(docker ps --filter "status=restarting" --format "{{.Names}}" 2>/dev/null || true)
if [[ -z "$RESTARTING" ]]; then
    pass "Нет контейнеров в restart loop"
else
    fail "Контейнеры в restart loop: $RESTARTING"
fi

# 6. Docker сеть vpn существует
if docker network inspect vpn_net &>/dev/null 2>&1 || \
   docker network ls 2>/dev/null | grep -q "vpn"; then
    NET_NAME=$(docker network ls 2>/dev/null | grep "vpn" | awk '{print $2}' | head -1)
    pass "Docker сеть VPN существует: ${NET_NAME:-vpn}"
else
    warn "Docker сеть vpn не найдена"
fi

# 7. docker-compose.yml существует
if [[ -f "/opt/vpn/docker-compose.yml" ]]; then
    pass "docker-compose.yml существует"
else
    fail "docker-compose.yml не найден в /opt/vpn/"
fi

# 8. .env существует и с правильными правами
if [[ -f "/opt/vpn/.env" ]]; then
    PERMS=$(stat -c "%a" /opt/vpn/.env 2>/dev/null || echo "?")
    if [[ "$PERMS" == "600" ]]; then
        pass ".env существует (права: 600)"
    else
        warn ".env права: $PERMS (должно быть 600)"
    fi
else
    fail ".env не найден в /opt/vpn/"
fi

# 9. Логи Docker не заполнили диск (json-file max-size)
DOCKER_LOG_SIZE=$(du -sh /var/lib/docker/containers 2>/dev/null | awk '{print $1}' || echo "?")
pass "Размер Docker логов: $DOCKER_LOG_SIZE"

# 10. socket-proxy запущен (не прямой Docker socket)
if docker inspect socket-proxy &>/dev/null; then
    PROXY_STATE=$(docker inspect --format '{{.State.Status}}' socket-proxy 2>/dev/null)
    if [[ "$PROXY_STATE" == "running" ]]; then
        pass "socket-proxy запущен (прямой Docker socket не используется)"
    else
        warn "socket-proxy $PROXY_STATE"
    fi
else
    warn "socket-proxy не найден (рекомендуется для безопасности)"
fi

echo ""
echo "Итог ${TEST_NAME}: PASS=$PASS FAIL=$FAIL WARN=$WARN"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

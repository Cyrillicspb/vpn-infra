#!/bin/bash
# =============================================================================
# deploy.sh — Обновление VPN Infrastructure с auto-rollback
#
# Использование:
#   sudo bash deploy.sh             — проверить обновления и применить
#   sudo bash deploy.sh --force     — применить даже если версия не изменилась
#   sudo bash deploy.sh --check     — только проверить, не применять
#   sudo bash deploy.sh --rollback  — откатиться к последнему снапшоту
#   sudo bash deploy.sh --status    — состояние последнего деплоя
#
# Логика:
#   1. git pull из VPS-зеркала (не из GitHub — может быть заблокирован)
#   2. create_snapshot (tar.gz ключей + .env + БД)
#   3. apply_migrations (идемпотентно)
#   4. docker compose pull + up -d
#   5. Если watchdog.py изменился — setsid restart (переживает собственный рестарт)
#   6. rsync vps/ конфигов на VPS (при изменениях) + docker compose up
#   7. smoke_tests с таймаутом → FAIL → auto-rollback + Telegram алерт
#   8. Очистка старых снапшотов (хранить последние 5)
# =============================================================================
set -euo pipefail

# ── Константы ─────────────────────────────────────────────────────────────────
REPO_DIR="/opt/vpn"
ENV_FILE="$REPO_DIR/.env"
SNAPSHOT_DIR="$REPO_DIR/.deploy-snapshot"
MIGRATIONS_LOG="$REPO_DIR/.migrations-applied"
LOG_FILE="/var/log/vpn-deploy.log"
LOCK_FILE="/var/run/vpn-deploy.lock"
SSH_KEY="/root/.ssh/vpn_id_ed25519"
SMOKE_TIMEOUT=120   # секунд на все smoke-тесты
SNAPSHOT_KEEP=5     # сколько снапшотов хранить

# ── Цвета и логирование ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

_log() { echo -e "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
log_info()  { _log "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { _log "${GREEN}[✓]${NC}    $*"; }
log_warn()  { _log "${YELLOW}[!]${NC}    $*"; }
log_error() { _log "${RED}[✗]${NC}    $*"; }
log_step()  { _log "${CYAN}${BOLD}━━━ $* ━━━${NC}"; }

# ── Telegram уведомления ──────────────────────────────────────────────────────
notify() {
    local msg
    msg="$(printf '%b' "$1")"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        -d "parse_mode=Markdown" \
        > /dev/null 2>&1 || true
}

notify_update_available() {
    local current_ver="$1" new_ver="$2" changelog="$3"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    local text="🆕 *Доступно обновление* \`${current_ver}\` → \`${new_ver}\`

${changelog}"
    local keyboard="{\"inline_keyboard\":[[{\"text\":\"✅ Обновить ${new_ver}\",\"callback_data\":\"update:confirm:${new_ver}\"},{\"text\":\"❌ Пропустить\",\"callback_data\":\"update:skip:${new_ver}\"}]]}"
    curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        -d "parse_mode=Markdown" \
        --data-urlencode "reply_markup=${keyboard}" \
        > /dev/null 2>&1 || true
}

# ── Загрузка .env ─────────────────────────────────────────────────────────────
load_env() {
    [[ -f "$ENV_FILE" ]] || { log_warn ".env не найден ($ENV_FILE)"; return; }
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
}

# ── SSH к VPS ─────────────────────────────────────────────────────────────────
SSH_PROXY_CMD="/opt/vpn/scripts/ssh-proxy.sh"

# vps_exec — быстрые read-only команды (echo, cat, docker ps, etc.)
# Обрыв соединения при переключении стека не критичен — легко повторить.
vps_exec() {
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    [[ -x "$SSH_PROXY_CMD" ]] && proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    ssh -p "$port" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        "${proxy_opts[@]}" \
        "sysadmin@${VPS_IP:-localhost}" "$@" 2>/dev/null
}

# vps_tmux_exec — все команды которые что-то меняют на VPS (docker, apt, git).
# Запускает команду в tmux-сессии, устойчив к переключению стека.
# При обрыве SSH-сессии команда продолжает работать на VPS.
vps_tmux_exec() {
    local cmd="$1"
    local timeout="${2:-300}"  # секунд ожидания, по умолчанию 5 минут
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    [[ -x "$SSH_PROXY_CMD" ]] && proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    local _ssh="ssh -p $port -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes ${proxy_opts[*]}"
    local session="deploy_$$_${RANDOM}"
    local out_file="/tmp/${session}.out"
    local rc_file="/tmp/${session}.rc"

    # Запускаем команду в detached tmux-сессии
    $_ssh "sysadmin@${VPS_IP}" \
        "tmux new-session -d -s '${session}' \
         'bash -c \"${cmd//\"/\\\"}\" > ${out_file} 2>&1; echo \$? > ${rc_file}'" \
        2>/dev/null || return 1

    # Ждём завершения (опрос rc_file)
    local elapsed=0
    while (( elapsed < timeout )); do
        sleep 3; elapsed=$(( elapsed + 3 ))
        if $_ssh "sysadmin@${VPS_IP}" \
                "[ -f '${rc_file}' ] && echo done" 2>/dev/null | grep -q "done"; then
            break
        fi
    done

    # Читаем вывод и exit code
    local output rc_val
    output=$($_ssh "sysadmin@${VPS_IP}" "cat '${out_file}' 2>/dev/null" 2>/dev/null || true)
    rc_val=$($_ssh "sysadmin@${VPS_IP}" "cat '${rc_file}' 2>/dev/null" 2>/dev/null || echo "1")

    # Очищаем tmux-сессию и временные файлы
    $_ssh "sysadmin@${VPS_IP}" \
        "tmux kill-session -t '${session}' 2>/dev/null; \
         rm -f '${out_file}' '${rc_file}'" 2>/dev/null || true

    [[ -n "$output" ]] && echo "$output"
    return "${rc_val:-1}"
}

# =============================================================================
# Снапшот
# =============================================================================
create_snapshot() {
    log_step "Создание снапшота перед деплоем"
    mkdir -p "$SNAPSHOT_DIR"

    local snap_id; snap_id="$(date +%Y%m%d_%H%M%S)"
    local snap_path="$SNAPSHOT_DIR/$snap_id"
    mkdir -p "$snap_path"

    local current_ver; current_ver="$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")"

    # Критичные файлы: ключи, .env, БД, nftables, конфиги
    local items=(
        "/etc/wireguard"
        "$ENV_FILE"
        "/etc/nftables.conf"
        "/etc/nftables-blocked-static.conf"
        "/etc/hysteria/config.yaml"
        "$REPO_DIR/home/xray"
        "$REPO_DIR/home/dnsmasq/dnsmasq.d"
        "/etc/vpn-routes"
    )

    local tar_args=()
    for item in "${items[@]}"; do
        [[ -e "$item" ]] && tar_args+=("$item")
    done

    # SQLite .backup — гарантированно консистентная копия
    local db_path="$REPO_DIR/telegram-bot/data/vpn_bot.db"
    if [[ -f "$db_path" ]]; then
        sqlite3 "$db_path" ".backup $snap_path/vpn_bot.db" 2>/dev/null || \
            cp "$db_path" "$snap_path/vpn_bot.db"
        tar_args+=("$snap_path/vpn_bot.db")
    fi

    # Создаём tar.gz снапшота
    tar -czf "$snap_path/snapshot.tar.gz" --ignore-failed-read \
        "${tar_args[@]}" 2>/dev/null || true

    # Метаданные снапшота
    cat > "$snap_path/meta.json" << EOF
{
  "snap_id": "$snap_id",
  "version": "$current_ver",
  "timestamp": "$(date -Iseconds)",
  "hostname": "$(hostname)"
}
EOF

    echo "$snap_id" > "$SNAPSHOT_DIR/latest"
    log_ok "Снапшот создан: $snap_id (v$current_ver)"

    # Очищаем старые снапшоты (оставляем SNAPSHOT_KEEP)
    local old_snaps
    old_snaps=$(ls -1dt "$SNAPSHOT_DIR"/20*/ 2>/dev/null | tail -n +$((SNAPSHOT_KEEP + 1)))
    if [[ -n "$old_snaps" ]]; then
        echo "$old_snaps" | xargs rm -rf
        log_info "Удалены старые снапшоты (оставлено $SNAPSHOT_KEEP)"
    fi
}

# =============================================================================
# Rollback
# =============================================================================
rollback() {
    local reason="${1:-ручной откат}"
    log_error "Откат: $reason"

    local latest="$SNAPSHOT_DIR/latest"
    if [[ ! -f "$latest" ]]; then
        log_error "Нет снапшота для отката — восстановление невозможно"
        notify "❌ *Deploy FAILED* — снапшот не найден, ручное вмешательство требуется\nПричина: $reason"
        return 1
    fi

    local snap_id; snap_id="$(cat "$latest")"
    local snap_path="$SNAPSHOT_DIR/$snap_id"

    log_step "Откат к снапшоту $snap_id"
    notify "⚠️ *Deploy FAILED* — откат к \`$snap_id\`\nПричина: $reason"

    # Останавливаем сервисы
    systemctl stop watchdog 2>/dev/null || true
    (cd "$REPO_DIR" && docker compose stop telegram-bot 2>/dev/null || true)

    # Восстанавливаем файлы из снапшота
    if [[ -f "$snap_path/snapshot.tar.gz" ]]; then
        tar -xzf "$snap_path/snapshot.tar.gz" -C / 2>/dev/null || true
        log_info "Файлы восстановлены из $snap_id"
    fi

    # Восстанавливаем БД если есть
    local db_path="$REPO_DIR/telegram-bot/data/vpn_bot.db"
    if [[ -f "$snap_path/vpn_bot.db" ]]; then
        mkdir -p "$(dirname "$db_path")"
        cp "$snap_path/vpn_bot.db" "$db_path"
        log_info "БД восстановлена"
    fi

    # Перезагружаем nftables
    systemctl restart nftables 2>/dev/null || true
    nft -f /etc/nftables-blocked-static.conf 2>/dev/null || true

    # Перезапускаем сервисы
    systemctl restart dnsmasq 2>/dev/null || true
    (cd "$REPO_DIR" && docker compose up -d --remove-orphans 2>/dev/null || true)
    sleep 3
    systemctl start watchdog 2>/dev/null || true

    log_ok "Откат к $snap_id завершён"
    notify "✅ Откат к \`$snap_id\` выполнен успешно"
    return 0
}

# =============================================================================
# Получение обновлений из VPS-зеркала
# =============================================================================
git_pull() {
    log_step "Получение обновлений"

    # Настраиваем remote vps-mirror если не настроен
    # Используем туннельный IP (VPS_TUNNEL_IP) — всегда доступен через tier-2 туннель
    if [[ -n "${VPS_IP:-}" ]]; then
        local ssh_port="22"  # туннельный интерфейс всегда на порту 22
        local mirror_host="${VPS_TUNNEL_IP:-10.177.2.2}"
        local current_url
        current_url=$(git -C "$REPO_DIR" remote get-url vps-mirror 2>/dev/null || true)
        # Обновить URL если указывает на публичный IP вместо туннельного
        if [[ -z "$current_url" ]]; then
            git -C "$REPO_DIR" remote add vps-mirror \
                "ssh://sysadmin@${mirror_host}:${ssh_port}/opt/vpn/vpn-repo.git"
        elif [[ "$current_url" != *"$mirror_host"* ]]; then
            git -C "$REPO_DIR" remote set-url vps-mirror \
                "ssh://sysadmin@${mirror_host}:${ssh_port}/opt/vpn/vpn-repo.git"
        fi

        # Сначала из VPS-зеркала (с тегами)
        if GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes" \
           git -C "$REPO_DIR" fetch --tags vps-mirror 2>/dev/null; then
            log_info "Получено из VPS-зеркала"
        else
            log_warn "VPS-зеркало недоступно, пробуем GitHub напрямую..."
            if ! git -C "$REPO_DIR" fetch --tags origin 2>/dev/null; then
                log_warn "Не удалось получить обновления"
                return 1
            fi
            log_info "Получено из GitHub"
        fi
    else
        if ! git -C "$REPO_DIR" fetch --tags origin 2>/dev/null; then
            log_warn "Не удалось получить обновления"
            return 1
        fi
        log_info "Получено из GitHub"
    fi

    # Найти последний тег vX.Y.Z
    local latest_tag
    latest_tag="$(git -C "$REPO_DIR" tag -l 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
        | sort -V | tail -1)"
    if [[ -z "$latest_tag" ]]; then
        log_warn "Теги vX.Y.Z не найдены"
        return 1
    fi

    local _before; _before=$(git -C "$REPO_DIR" rev-parse HEAD)
    git -C "$REPO_DIR" checkout --detach "$latest_tag" 2>/dev/null || {
        log_warn "Не удалось переключиться на тег $latest_tag"
        return 1
    }
    local _after; _after=$(git -C "$REPO_DIR" rev-parse HEAD)
    if [[ "$_before" != "$_after" ]]; then
        log_ok "Переключено на $latest_tag"
        return 0
    fi
    log_info "Уже на последнем теге: $latest_tag"
    return 1
}

# =============================================================================
# Применение миграций (идемпотентно)
# =============================================================================
apply_system_configs() {
    # Синхронизирует системные конфиги из репо в /etc/ и /etc/systemd/system/.
    # Сравнивает sha256 — применяет только изменившиеся файлы.
    local changed=false

    # nftables.conf
    local src="$REPO_DIR/home/nftables/nftables.conf"
    local dst="/etc/nftables.conf"
    if [[ -f "$src" ]] && ! sha256sum -c <(sha256sum "$dst" 2>/dev/null | awk '{print $1 "  '"$src"'"}') &>/dev/null; then
        log_info "nftables.conf изменился — применяем..."
        if nft -c -f "$src" 2>/dev/null; then
            cp "$src" "$dst"
            nft -f "$dst" && log_ok "nftables обновлён" || log_warn "nft -f завершился с ошибкой"
            # nftables.conf делает flush ruleset — восстанавливаем blocked_static
            nft -f /etc/nftables-blocked-static.conf 2>/dev/null && log_ok "blocked_static восстановлен" || log_warn "blocked_static не найден — будет заполнен dnsmasq"
            changed=true
        else
            log_warn "nftables.conf не прошёл валидацию (nft -c) — пропускаем"
        fi
    fi

    # systemd units
    local units_changed=false
    for src in "$REPO_DIR/home/systemd/"*.service "$REPO_DIR/home/systemd/"*; do
        [[ -f "$src" ]] || continue
        local name; name=$(basename "$src")
        local dst_unit="/etc/systemd/system/$name"
        if [[ ! -f "$dst_unit" ]] || ! diff -q "$src" "$dst_unit" &>/dev/null; then
            cp "$src" "$dst_unit"
            log_ok "systemd unit обновлён: $name"
            units_changed=true
            changed=true
        fi
    done
    $units_changed && systemctl daemon-reload

    $changed || log_info "Системные конфиги без изменений"
}

apply_migrations() {
    local dir="$REPO_DIR/migrations"
    [[ -d "$dir" ]] || return 0
    touch "$MIGRATIONS_LOG"

    local count=0
    local db_file="/opt/vpn/telegram-bot/data/vpn_bot.db"
    while IFS= read -r -d '' migration; do
        local name; name="$(basename "$migration")"
        [[ "$name" == "apply.sh" ]] && continue  # точка входа, не сама миграция
        if grep -qxF "$name" "$MIGRATIONS_LOG" 2>/dev/null; then
            continue  # уже применена
        fi
        log_info "Миграция: $name"
        local ok=0
        case "$migration" in
            *.sql)
                if [[ -f "$db_file" ]]; then
                    sqlite3 "$db_file" < "$migration" >> "$LOG_FILE" 2>&1 && ok=1
                else
                    log_warn "Миграция $name пропущена: БД не найдена ($db_file)"
                    continue
                fi
                ;;
            *.sh)
                bash "$migration" >> "$LOG_FILE" 2>&1 && ok=1
                ;;
        esac
        if [[ $ok -eq 1 ]]; then
            echo "$name" >> "$MIGRATIONS_LOG"
            log_ok "Миграция $name применена"
            count=$((count + 1))
        else
            log_warn "Миграция $name не выполнилась — продолжаем"
        fi
    done < <(find "$dir" \( -name "*.sh" -o -name "*.sql" \) -print0 | sort -z)

    [[ $count -gt 0 ]] && log_info "Применено миграций: $count"
    return 0
}

# =============================================================================
# Smoke-тесты
# =============================================================================
# Возвращает список упавших тестов (одна строка — один тест)
_get_failed_tests() {
    local test_script="$REPO_DIR/tests/run-smoke-tests.sh"
    [[ -f "$test_script" ]] || { echo ""; return 0; }
    timeout "$SMOKE_TIMEOUT" bash "$test_script" 2>/dev/null \
        | grep '^\s*\[FAIL\]' | awk '{print $2}' | sort || true
}

run_smoke_tests() {
    log_step "Smoke-тесты (таймаут ${SMOKE_TIMEOUT}с)"
    local test_script="$REPO_DIR/tests/run-smoke-tests.sh"
    [[ -f "$test_script" ]] || { log_warn "Smoke-тесты не найдены ($test_script)"; return 0; }

    local output; output=$(mktemp)
    if timeout "$SMOKE_TIMEOUT" bash "$test_script" > "$output" 2>&1; then
        log_ok "Smoke-тесты прошли"
        rm -f "$output"
        return 0
    else
        local rc=$?
        log_error "Smoke-тесты ПРОВАЛИЛИСЬ (exit=$rc):"
        cat "$output" | tee -a "$LOG_FILE"
        rm -f "$output"
        return 1
    fi
}

# =============================================================================
# Деплой на VPS
# =============================================================================
VPS_DEPLOY_STATUS=""   # глобальный результат: заполняется deploy_vps, читается do_deploy

deploy_vps() {
    if [[ -z "${VPS_IP:-}" ]]; then
        VPS_DEPLOY_STATUS="—"
        log_warn "VPS_IP не задан, пропуск"
        return 0
    fi
    local force="${1:-}"
    log_step "Деплой на VPS ${VPS_IP}"

    local ssh_port="${VPS_SSH_PORT:-22}"
    local rsync_ssh="ssh -p $ssh_port -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes"
    [[ -x "$SSH_PROXY_CMD" ]] && \
        rsync_ssh+=" -o ProxyCommand='${SSH_PROXY_CMD} %h %p'"
    local vps_target="sysadmin@${VPS_IP}"

    # ── Синхронизация конфигов VPS при изменениях ───────────────────────────────
    if vps_any_changed || [[ "$force" == "--force" ]]; then
        log_info "Синхронизация конфигов VPS..."

        # docker-compose.yml
        if [[ -f "$REPO_DIR/vps/docker-compose.yml" ]]; then
            rsync -e "$rsync_ssh" -a \
                "$REPO_DIR/vps/docker-compose.yml" \
                "${vps_target}:/opt/vpn/docker-compose.yml" 2>/dev/null \
                && log_ok "docker-compose.yml синхронизирован" || true
        fi

        # nginx конфиги (без ssl/ и mtls/ — генерируются на VPS)
        if [[ -d "$REPO_DIR/vps/nginx" ]]; then
            rsync -e "$rsync_ssh" -a \
                --exclude="ssl/" --exclude="mtls/" \
                "$REPO_DIR/vps/nginx/" \
                "${vps_target}:/opt/vpn/nginx/" 2>/dev/null \
                && log_ok "nginx конфиги синхронизированы" || true
        fi

        # prometheus / alertmanager / grafana provisioning
        for subdir in prometheus alertmanager grafana/provisioning; do
            if [[ -d "$REPO_DIR/vps/$subdir" ]]; then
                vps_exec "mkdir -p /opt/vpn/$subdir" 2>/dev/null || true
                rsync -e "$rsync_ssh" -a \
                    "$REPO_DIR/vps/$subdir/" \
                    "${vps_target}:/opt/vpn/$subdir/" 2>/dev/null || true
            fi
        done
        vps_monitoring_changed && log_ok "Мониторинг конфиги синхронизированы"

        # scripts
        if [[ -d "$REPO_DIR/vps/scripts" ]]; then
            rsync -e "$rsync_ssh" -a \
                "$REPO_DIR/vps/scripts/" \
                "${vps_target}:/opt/vpn/scripts/" 2>/dev/null \
                && vps_exec "chmod +x /opt/vpn/scripts/*.sh 2>/dev/null || true" \
                && log_ok "VPS scripts синхронизированы" || true
        fi
    fi

    # ── Обновление контейнеров на VPS ───────────────────────────────────────────
    local retry=0 max_retry=2
    while (( retry <= max_retry )); do
        # Всегда pull новых образов (теги могут измениться в compose)
        local cmd="cd /opt/vpn && docker compose pull --quiet 2>/dev/null || true"

        # Принудительный рестарт nginx если его конфиги изменились
        if vps_nginx_changed || [[ "$force" == "--force" ]]; then
            cmd+=" && docker compose up -d --force-recreate nginx 2>/dev/null || true"
        fi

        # Принудительный рестарт мониторинга если его конфиги изменились
        if vps_monitoring_changed || [[ "$force" == "--force" ]]; then
            cmd+=" && docker compose up -d --force-recreate prometheus alertmanager grafana 2>/dev/null || true"
        fi

        # Общий up -d: подхватит новые образы и изменения в compose
        cmd+=" && docker compose up -d --remove-orphans"

        if vps_tmux_exec "$cmd" 300; then
            log_ok "VPS ${VPS_IP} обновлён"
            VPS_DEPLOY_STATUS="✅ ${VPS_IP}"
            return 0
        fi
        ((retry++))
        [[ $retry -le $max_retry ]] && { log_warn "Retry $retry/$max_retry..."; sleep 5; }
    done

    log_warn "Деплой на VPS не удался — обновите вручную: /vps deploy"
    VPS_DEPLOY_STATUS="❌ ${VPS_IP} (требует ручного обновления)"
    return 0   # Не прерываем деплой из-за VPS
}

# =============================================================================
# Проверить изменились ли подсистемы
# =============================================================================
watchdog_changed() {
    # Сравниваем время последнего коммита watchdog с временем старта сервиса.
    local last_commit service_epoch commit_epoch
    last_commit=$(git -C "$REPO_DIR" log -1 --format="%ct" -- \
        home/watchdog/watchdog.py home/watchdog/requirements.txt 2>/dev/null || echo "0")
    service_epoch=$(systemctl show watchdog --property=ActiveEnterTimestampMonotonic --value 2>/dev/null \
        | awk '{printf "%d", $1/1000000}' || echo "0")
    # Если сервис не запущен — перезапустить
    [[ "$service_epoch" -eq 0 ]] && return 0
    # Сравниваем через реальное время старта сервиса
    local service_real_epoch
    service_real_epoch=$(systemctl show watchdog --property=ActiveEnterTimestamp --value 2>/dev/null \
        | xargs -I{} date -d "{}" +%s 2>/dev/null || echo "0")
    commit_epoch="${last_commit:-0}"
    [[ "$commit_epoch" -gt "${service_real_epoch:-0}" ]]
}

bot_changed() {
    # Сравниваем git-хэш последнего коммита home/telegram-bot/ с хэшем, зашитым в образ.
    # Надёжнее сравнения по времени: не ломается при ручных пересборках.
    local current_hash image_hash
    current_hash=$(git -C "$REPO_DIR" log -1 --format="%H" -- home/telegram-bot/ 2>/dev/null || echo "")
    [[ -z "$current_hash" ]] && return 1  # нет коммитов — не пересобирать
    image_hash=$(docker inspect --format='{{index .Config.Labels "git-hash"}}' \
        vpn-telegram-bot:latest 2>/dev/null || echo "")
    [[ "$current_hash" != "$image_hash" ]]
}

xray_changed() {
    git -C "$REPO_DIR" diff HEAD@{1} HEAD -- \
        home/xray/ \
        2>/dev/null | grep -q "."
}

vps_any_changed() {
    git -C "$REPO_DIR" diff HEAD@{1} HEAD -- vps/ 2>/dev/null | grep -q "."
}

vps_nginx_changed() {
    git -C "$REPO_DIR" diff HEAD@{1} HEAD -- vps/nginx/ 2>/dev/null | grep -q "."
}

vps_monitoring_changed() {
    git -C "$REPO_DIR" diff HEAD@{1} HEAD -- \
        vps/prometheus/ vps/alertmanager/ vps/grafana/ \
        2>/dev/null | grep -q "."
}

vps_scripts_changed() {
    git -C "$REPO_DIR" diff HEAD@{1} HEAD -- vps/scripts/ 2>/dev/null | grep -q "."
}

# =============================================================================
# Главный деплой
# =============================================================================
do_deploy() {
    local force="${1:-}"
    local prev_ver; prev_ver="$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")"

    # Получаем обновления
    git_pull || { log_warn "Нет обновлений"; [[ "$force" == "--force" ]] || exit 0; }

    local new_ver; new_ver="$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")"

    # Проверяем нужен ли деплой
    if [[ "$prev_ver" == "$new_ver" && "$force" != "--force" ]]; then
        log_info "Версия не изменилась ($new_ver) — обновление не требуется"
        log_info "Используйте --force для принудительного деплоя"
        exit 0
    fi

    # Показываем diff
    local changed_files
    changed_files="$(git -C "$REPO_DIR" diff --name-only HEAD@{1} HEAD 2>/dev/null | head -20 || echo "(неизвестно)")"
    log_info "Изменённые файлы:\n$changed_files"
    notify "🚀 *Деплой* \`${prev_ver}\` → \`${new_ver}\`\nИзменено файлов: $(echo "$changed_files" | wc -l)"

    # Baseline: запомнить упавшие тесты ДО деплоя
    log_info "Собираем baseline smoke-тестов..."
    local baseline_fails; baseline_fails="$(_get_failed_tests)"
    if [[ -n "$baseline_fails" ]]; then
        log_warn "Pre-existing провалы (не будут причиной отката):"
        echo "$baseline_fails" | while read -r t; do log_warn "  - $t"; done
    fi

    # Снапшот
    create_snapshot

    # Миграции
    apply_migrations

    # Системные конфиги (/etc/nftables.conf, systemd units)
    apply_system_configs

    # ── Обновление домашнего сервера ───────────────────────────────────────────

    # Нужно ли перезапустить watchdog?
    local restart_watchdog=false
    watchdog_changed && restart_watchdog=true

    # Нужно ли пересобрать локальные образы?
    local rebuild_bot=false
    local rebuild_xray=false
    bot_changed && rebuild_bot=true
    xray_changed && rebuild_xray=true
    [[ "$force" == "--force" ]] && rebuild_bot=true && rebuild_xray=true

    # Обновляем Python venv если requirements изменились
    if git -C "$REPO_DIR" diff HEAD@{1} HEAD -- home/watchdog/requirements.txt 2>/dev/null | grep -q "."; then
        log_info "Обновление watchdog venv..."
        "$REPO_DIR/watchdog/venv/bin/pip" install -q --no-cache-dir \
            -r "$REPO_DIR/watchdog/requirements.txt" 2>/dev/null || true
    fi

    # Синхронизация кода из home/ в рабочие директории (repo структура vs. deployment)
    log_info "Синхронизация home/ → deployment директории..."
    rsync -a --exclude="data/" "$REPO_DIR/home/telegram-bot/" "$REPO_DIR/telegram-bot/" 2>/dev/null || true
    rsync -a "$REPO_DIR/home/watchdog/watchdog.py" "$REPO_DIR/watchdog/watchdog.py" 2>/dev/null || true
    rsync -a "$REPO_DIR/home/watchdog/plugins/" "$REPO_DIR/watchdog/plugins/" 2>/dev/null || true
    rsync -a "$REPO_DIR/home/scripts/" "$REPO_DIR/scripts/" 2>/dev/null && chmod +x "$REPO_DIR/scripts/"*.sh 2>/dev/null || true

    # Xray конфиги: шаблоны из home/xray/ → подставить .env → xray/
    # ВАЖНО: home/xray/*.json — шаблоны с ${VAR}, нельзя rsync напрямую
    source "$REPO_DIR/.env" 2>/dev/null || true
    for tmpl in "$REPO_DIR/home/xray/"*.json; do
        name=$(basename "$tmpl")
        envsubst < "$tmpl" > "$REPO_DIR/xray/$name"
    done
    log_ok "Xray конфиги обновлены (envsubst)"

    # CDN конфиг: регенерировать из .env если CF_CDN_HOSTNAME задан
    # Транспорт: splithttp (xHTTP H2) — WS устарел в Xray 26.x
    if [[ -n "${CF_CDN_HOSTNAME:-}" ]]; then
        CF_CDN_UUID="${CF_CDN_UUID:-$(python3 -c "import uuid; print(uuid.uuid4())")}"
        python3 -c "
import json, os
cfg = {
    'log': {'loglevel': 'warning'},
    'inbounds': [{'listen': '127.0.0.1', 'port': 1082, 'protocol': 'socks', 'settings': {'udp': True}}],
    'outbounds': [{'protocol': 'vless', 'tag': 'vless-xhttp-cdn-out', 'settings': {'vnext': [{'address': os.environ['CF_CDN_HOSTNAME'], 'port': 443, 'users': [{'id': os.environ['CF_CDN_UUID'], 'encryption': 'none', 'flow': ''}]}]}, 'streamSettings': {'network': 'splithttp', 'security': 'tls', 'tlsSettings': {'serverName': os.environ['CF_CDN_HOSTNAME'], 'alpn': ['h2', 'http/1.1'], 'allowInsecure': False}, 'splithttpSettings': {'path': '/vpn-cdn', 'host': os.environ['CF_CDN_HOSTNAME'], 'xPaddingBytes': '100-1000'}}}, {'protocol': 'freedom', 'tag': 'direct'}],
    'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'direct'}]}
}
json.dump(cfg, open('$REPO_DIR/xray/config-cdn.json', 'w'), indent=4)
" && log_ok "config-cdn.json обновлён (xHTTP CDN: ${CF_CDN_HOSTNAME})"
    fi

    # Docker Compose обновление
    log_step "Обновление Docker контейнеров"
    (cd "$REPO_DIR" && docker compose pull --quiet 2>/dev/null || true)

    # Пересборка локальных образов при изменении исходников
    # --no-cache только если изменился requirements.txt (pip-зависимости),
    # иначе быстрая сборка с Docker layer cache
    if $rebuild_bot; then
        log_info "telegram-bot изменился — пересборка образа..."
        local bot_no_cache=""
        git -C "$REPO_DIR" diff HEAD@{1} HEAD -- home/telegram-bot/requirements.txt 2>/dev/null | grep -q "." && bot_no_cache="--no-cache"
        [[ "$force" == "--force" && -z "$bot_no_cache" ]] && true  # force не форсирует --no-cache без изменений requirements
        local bot_git_hash; bot_git_hash=$(git -C "$REPO_DIR" log -1 --format="%H" -- home/telegram-bot/ 2>/dev/null || echo "unknown")
        (cd "$REPO_DIR" && docker compose build $bot_no_cache \
            --build-arg GIT_HASH="$bot_git_hash" telegram-bot)
        log_ok "telegram-bot пересобран${bot_no_cache:+ (no-cache)} (hash=${bot_git_hash:0:8})"
    fi
    if $rebuild_xray; then
        log_info "xray конфиги обновлены — перезапуск xray контейнеров..."
        (cd "$REPO_DIR" && docker compose up -d --force-recreate xray-client xray-client-2 xray-client-cdn 2>/dev/null || true)
        log_ok "xray контейнеры перезапущены"
    fi

    (cd "$REPO_DIR" && docker compose up -d --remove-orphans)

    # Перезапуск watchdog как отдельный процесс (переживёт собственный рестарт)
    if $restart_watchdog; then
        log_info "Watchdog изменился — перезапуск (detached)..."
        # setsid: новая сессия → не убивается при завершении текущего процесса
        setsid bash -c "sleep 3 && systemctl restart watchdog >> $LOG_FILE 2>&1" &
    fi

    # Деплой на VPS
    deploy_vps "$force"

    # Ждём стабилизации
    log_info "Ожидание стабилизации (15с)..."
    sleep 15

    # Smoke-тесты: откат только если появились НОВЫЕ провалы
    local after_fails; after_fails="$(_get_failed_tests)"
    local new_fails; new_fails="$(comm -13 <(echo "$baseline_fails") <(echo "$after_fails"))"
    if [[ -n "$new_fails" ]]; then
        log_error "Новые провалы после деплоя:"
        echo "$new_fails" | while read -r t; do log_error "  - $t"; done
        rollback "новые smoke-тест провалы после деплоя v${new_ver}: $(echo "$new_fails" | tr '\n' ' ')"
        notify "❌ *Deploy FAILED* v${new_ver} — откат выполнен\nНовые провалы: $(echo "$new_fails" | tr '\n' ' ')"
        exit 1
    elif [[ -n "$after_fails" ]]; then
        log_warn "Smoke-тесты: есть провалы но все pre-existing — деплой принят"
        run_smoke_tests || true   # показать полный вывод для информации
    else
        log_ok "Smoke-тесты прошли"
    fi

    # Итоговый отчёт
    local home_line="Домашний сервер: ✅"
    local vps_line=""
    if [[ -n "${VPS_DEPLOY_STATUS:-}" && "${VPS_DEPLOY_STATUS}" != "—" ]]; then
        vps_line="\nVPS: ${VPS_DEPLOY_STATUS}"
    fi

    log_ok "Deploy v${new_ver} завершён успешно"
    notify "✅ *Обновлено* до \`${new_ver}\`\n${home_line}${vps_line}"
}

# =============================================================================
# Проверка обновлений (--check): уведомить если есть новая версия
# =============================================================================
check_updates() {
    local current_ver; current_ver="$(cat "$REPO_DIR/version" 2>/dev/null | tr -d '[:space:]' || echo 'unknown')"
    log_info "Текущая версия: ${current_ver}"

    # Синхронизировать VPS-зеркало (сервер тянет с GitHub)
    if [[ -n "${VPS_IP:-}" ]]; then
        vps_exec "cd /opt/vpn/vpn-repo.git && git fetch --tags --quiet" 2>/dev/null || true
    fi

    # Получить последний тег vX.Y.Z из удалённого репозитория (не применять локально)
    local remote_ver=""
    if [[ -n "${VPS_IP:-}" ]]; then
        remote_ver="$(GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes" \
            git -C "$REPO_DIR" ls-remote --tags vps-mirror 'refs/tags/v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
            | awk '{print $2}' | sed 's|refs/tags/||' | grep -v '\^{}' | sort -V | tail -1)" || true
    fi
    if [[ -z "${remote_ver:-}" ]]; then
        remote_ver="$(git -C "$REPO_DIR" ls-remote --tags origin 'refs/tags/v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
            | awk '{print $2}' | sed 's|refs/tags/||' | grep -v '\^{}' | sort -V | tail -1)" || true
    fi

    if [[ -z "${remote_ver:-}" || "${remote_ver}" == "${current_ver}" ]]; then
        log_info "Версия актуальна: ${current_ver}"
        return 0
    fi

    log_info "Доступна новая версия: ${remote_ver}"

    # Проверить не пропущена ли эта версия
    local skip_ver; skip_ver="$(cat "$REPO_DIR/.skip-version" 2>/dev/null | tr -d '[:space:]')"
    if [[ "${skip_ver}" == "${remote_ver}" ]]; then
        log_info "Версия ${remote_ver} помечена как пропущенная"
        return 0
    fi

    # Извлечь секцию CHANGELOG для новой версии (из origin/master без применения)
    local changelog
    changelog="$(git -C "$REPO_DIR" show origin/master:CHANGELOG.md 2>/dev/null \
        | awk "/^## \[${remote_ver}\]/{found=1; next} found && /^## \[/{exit} found{print}" \
        | head -20 | sed '/^[[:space:]]*$/d')" || true
    [[ -z "$changelog" ]] && changelog="_(подробности: CHANGELOG.md)_"

    # Отправить уведомление с кнопками
    notify_update_available "${current_ver}" "${remote_ver}" "${changelog}"
    log_ok "Уведомление об обновлении ${remote_ver} отправлено"
}

# =============================================================================
# Статус
# =============================================================================
show_status() {
    echo ""
    echo "── Deploy Status ──────────────────────────────"
    echo "  Версия:   $(cat "$REPO_DIR/version" 2>/dev/null || echo 'unknown')"
    echo "  Снапшот:  $(cat "$SNAPSHOT_DIR/latest" 2>/dev/null || echo 'нет')"
    echo "  Последний деплой:"
    tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'
    echo "───────────────────────────────────────────────"
}

# =============================================================================
# Main
# =============================================================================
main() {
    # Проверка root
    [[ "$EUID" -eq 0 ]] || { echo "Запустите: sudo bash deploy.sh"; exit 1; }

    mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$SNAPSHOT_DIR")"
    echo "" >> "$LOG_FILE"
    echo "════ Deploy $(date '+%Y-%m-%d %H:%M:%S') ════" >> "$LOG_FILE"

    load_env

    # Один экземпляр деплоя
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log_error "Деплой уже запущен ($LOCK_FILE)"
        exit 1
    fi

    case "${1:-}" in
        --rollback) rollback "ручной откат" ;;
        --check)    check_updates ;;
        --status)   show_status ;;
        --force)    do_deploy "--force" ;;
        "")         do_deploy ;;
        *)          echo "Неизвестный аргумент: $1"; echo "Использование: $0 [--force|--check|--rollback|--status]"; exit 1 ;;
    esac
}

main "$@"

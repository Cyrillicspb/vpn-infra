#!/bin/bash
# =============================================================================
# common.sh — Общие функции и константы для setup.sh, install-home.sh,
#             install-vps.sh
#
# Использование (в начале скрипта, после установки STEP):
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/common.sh"
#
# Предоставляет: цвета, TOTAL_STEPS, STATE_FILE, ENV_FILE,
#   log_info/ok/warn/error, _progress_bar, step, is_done,
#   step_done, step_skip, die, env_set
# =============================================================================

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Константы ─────────────────────────────────────────────────────────────────
TOTAL_STEPS=61
STATE_FILE="/opt/vpn/.setup-state"
ENV_FILE="/opt/vpn/.env"
COMPACT_OUTPUT="${VPN_COMPACT_OUTPUT:-${VPN_NONINTERACTIVE:-}}"

# ── Логирование ───────────────────────────────────────────────────────────────
_log_emit() {
    local color="$1" prefix="$2"; shift 2
    if [[ -n "$COMPACT_OUTPUT" ]]; then
        printf '%s %s\n' "$prefix" "$*"
    else
        echo -e "${color}${prefix}${NC} $*"
    fi
}

log_info()  { _log_emit "${BLUE}"   "[INFO]" "$*"; }
log_ok()    { _log_emit "${GREEN}"  "[OK]"   "$*"; }
log_warn()  { _log_emit "${YELLOW}" "[WARN]" "$*"; }
log_error() { _log_emit "${RED}"    "[ERR]"  "$*" >&2; }

format_duration() {
    local total="${1:-0}"
    local hours=$(( total / 3600 ))
    local minutes=$(( (total % 3600) / 60 ))
    local seconds=$(( total % 60 ))
    if (( hours > 0 )); then
        printf '%02d:%02d:%02d' "$hours" "$minutes" "$seconds"
    else
        printf '%02d:%02d' "$minutes" "$seconds"
    fi
}

_compact_emit_failure_log() {
    local log_file="$1"
    [[ -f "$log_file" ]] || return 0

    local _matched
    _matched="$(grep -E '^(E: |W: |Err: |dpkg: error:|apt(?:-get)?: )' "$log_file" || true)"
    if [[ -n "$_matched" ]]; then
        printf '%s\n' "$_matched" >&2
    else
        tail -n 80 "$log_file" >&2 || true
    fi
}

run_with_compact_progress() {
    local label="$1"; shift

    if [[ -z "$COMPACT_OUTPUT" ]]; then
        "$@"
        return $?
    fi

    local log_file
    log_file="$(mktemp /tmp/vpn-install-step.XXXXXX.log)"
    log_info "${label}..."

    set +e
    "$@" >"$log_file" 2>&1 &
    local pid=$!
    local start_ts=$SECONDS
    local last_emit=-10

    while kill -0 "$pid" 2>/dev/null; do
        sleep 2
        kill -0 "$pid" 2>/dev/null || break
        local elapsed=$(( SECONDS - start_ts ))
        if (( elapsed >= 5 && elapsed - last_emit >= 10 )); then
            log_info "${label}... $(format_duration "$elapsed")"
            last_emit=$elapsed
        fi
    done

    wait "$pid"
    local rc=$?
    set -e

    if (( rc != 0 )); then
        log_error "${label}: команда завершилась с ошибкой"
        _compact_emit_failure_log "$log_file"
        rm -f "$log_file"
        return "$rc"
    fi

    rm -f "$log_file"
    return 0
}

apt_quiet() {
    local label="$1"; shift
    run_with_compact_progress "$label" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a APT_LISTCHANGES_FRONTEND=none \
        apt-get -o Dpkg::Use-Pty=0 -o APT::Color=0 -o Dpkg::Progress-Fancy=0 "$@"
}

step() {
    ((STEP++)) || true
    echo ""
    if [[ -n "$COMPACT_OUTPUT" ]]; then
        printf '[STEP %d/%d] %s\n' "${STEP}" "${TOTAL_STEPS}" "$*"
    else
        echo -e "${CYAN}${BOLD}━━━ Шаг ${STEP}/${TOTAL_STEPS}: $* ━━━${NC}"
    fi
    emit_progress "$*" "start"
}

# ── Машиночитаемый маркер прогресса (парсится TUI installer.py) ───────────────
# Формат: ##PROGRESS:current:total:name:status
emit_progress() {
    printf '##PROGRESS:%d:%d:%s:%s\n' "${STEP}" "${TOTAL_STEPS}" "${1:-unknown}" "${2:-done}"
}

# ── Состояние шагов (.setup-state) ────────────────────────────────────────────
is_done()    { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
step_done()  {
    echo "$1" >> "$STATE_FILE"
    [[ -z "$COMPACT_OUTPUT" ]] && log_ok "Готово: $1"
    emit_progress "$1" "done"
}
step_skip()  {
    ((STEP++)) || true
    [[ -z "$COMPACT_OUTPUT" ]] && log_info "Пропуск (уже выполнено): $1"
    emit_progress "$1" "skip"
}
step_reset() { sed -i "/^$(printf '%s' "$1" | sed 's/[.[\*^$]/\\&/g')$/d" "$STATE_FILE" 2>/dev/null || true; }

# ── Завершение с ошибкой ──────────────────────────────────────────────────────
die() {
    log_error "$*"
    echo ""
    if [[ -n "$COMPACT_OUTPUT" ]]; then
        echo "[ERR] Установка остановлена."
        echo "[ERR] Исправьте проблему выше и повторите запуск."
    else
        echo -e "${RED}━━━ Ошибка ━━━${NC}"
        echo "  Проблема: $*"
        echo "  Действие: проверьте вывод выше и устраните причину."
        echo "  Повтор:   sudo bash setup.sh  (выполненные шаги будут пропущены)"
    fi
    exit 1
}

# ── Запись/обновление переменной в .env ───────────────────────────────────────
# Безопасно для значений с |, /, &, \ (не использует sed)
env_set() {
    local key="$1" val="$2"
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { log_error "Невалидное имя переменной: $key"; return 1; }
    mkdir -p "$(dirname "$ENV_FILE")"
    chmod 700 "$(dirname "$ENV_FILE")"
    touch "$ENV_FILE"
    # || true: grep возвращает 1 если строка не найдена — это нормально
    { grep -v "^${key}=" "$ENV_FILE" || true; } > "${ENV_FILE}.tmp"
    chmod 600 "${ENV_FILE}.tmp"
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
    printf "%s='%s'\n" "$key" "${val//\'/\'\\\'\'}" >> "$ENV_FILE"
}

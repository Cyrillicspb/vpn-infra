#!/bin/bash
# =============================================================================
# deploy.sh — Обновление VPN Infrastructure с release-state и rollback
#
# Использование:
#   sudo bash deploy.sh
#   sudo bash deploy.sh --force
#   sudo bash deploy.sh --check
#   sudo bash deploy.sh --status
#   sudo bash deploy.sh --rollback
# =============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/vpn}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$REPO_DIR/.deploy-snapshot}"
STATE_DIR="${STATE_DIR:-$REPO_DIR/.deploy-state}"
MIGRATIONS_LOG="${MIGRATIONS_LOG:-$REPO_DIR/.migrations-applied}"
LOG_FILE="${LOG_FILE:-/var/log/vpn-deploy.log}"
LOCK_FILE="${LOCK_FILE:-/var/run/vpn-deploy.lock}"
SSH_KEY="${SSH_KEY:-/root/.ssh/vpn_id_ed25519}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-deploy-live}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-120}"
SNAPSHOT_KEEP="${SNAPSHOT_KEEP:-5}"
ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-0}"
BASELINE_SMOKE_FAILURES="${BASELINE_SMOKE_FAILURES:-}"

CURRENT_STATE_FILE="$STATE_DIR/current.json"
PENDING_STATE_FILE="$STATE_DIR/pending.json"
LAST_ATTEMPT_FILE="$STATE_DIR/last-attempt.json"
REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-/opt/vpn/.deploy-state}"
REMOTE_CURRENT_STATE_FILE="$REMOTE_STATE_DIR/current.json"
REMOTE_PENDING_STATE_FILE="$REMOTE_STATE_DIR/pending.json"
REMOTE_LAST_ATTEMPT_FILE="$REMOTE_STATE_DIR/last-attempt.json"
SSH_PROXY_CMD="$REPO_DIR/scripts/ssh-proxy.sh"
GITHUB_REPO_URL_DEFAULT="${GITHUB_REPO_URL_DEFAULT:-https://github.com/Cyrillicspb/vpn-infra.git}"
DEPLOY_USE_SSH_PROXY="${DEPLOY_USE_SSH_PROXY:-0}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

_log() { echo -e "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
log_info()  { _log "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { _log "${GREEN}[✓]${NC}    $*"; }
log_warn()  { _log "${YELLOW}[!]${NC}    $*"; }
log_error() { _log "${RED}[✗]${NC}    $*"; }
log_step()  { _log "${CYAN}${BOLD}━━━ $* ━━━${NC}"; }

usage() {
    cat <<EOF
Использование:
  bash deploy.sh
  bash deploy.sh --force
  bash deploy.sh --check
  bash deploy.sh --status
  bash deploy.sh --rollback
  bash deploy.sh --help

Опции:
  --check      Проверить доступность нового release и rollback readiness без применения
  --force      Применить релиз даже если commit не изменился
  --status     Показать current/pending/previous release и rollback status
  --rollback   Откатить к последнему подтвержденному snapshot
EOF
}

notify() {
    local msg
    msg="$(printf '%b' "$1")"
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_ADMIN_CHAT_ID:-}" ]] && return 0
    local tg_send="$REPO_DIR/scripts/tg-send.sh"
    if [[ ! -x "$tg_send" ]]; then
        return 0
    fi
    "$tg_send" "${TELEGRAM_ADMIN_CHAT_ID}" "${msg}" || true
}

persist_env_default() {
    local key="$1"
    local value="${2:-}"
    [[ -n "$value" ]] || return 0
    grep -q "^${key}=" "$ENV_FILE" 2>/dev/null && return 0
    printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
    export "${key}=${value}"
}

load_env() {
    [[ -f "$ENV_FILE" ]] || { log_warn ".env не найден ($ENV_FILE)"; return; }
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport

    persist_env_default "XRAY_XHTTP_UUID"        "${XRAY_XHTTP_UUID:-${XRAY_GRPC_UUID:-}}"
    persist_env_default "XRAY_XHTTP_PRIVATE_KEY" "${XRAY_XHTTP_PRIVATE_KEY:-${XRAY_GRPC_PRIVATE_KEY:-}}"
    persist_env_default "XRAY_XHTTP_PUBLIC_KEY"  "${XRAY_XHTTP_PUBLIC_KEY:-${XRAY_GRPC_PUBLIC_KEY:-}}"
    persist_env_default "XRAY_XHTTP_SHORT_ID"    "${XRAY_XHTTP_SHORT_ID:-${XRAY_GRPC_SHORT_ID:-}}"
    persist_env_default "XRAY_XHTTP_SOCKS_PORT"  "${XRAY_XHTTP_SOCKS_PORT:-${XRAY_GRPC_SOCKS_PORT:-1081}}"

    export XRAY_XHTTP_UUID="${XRAY_XHTTP_UUID:-${XRAY_GRPC_UUID:-}}"
    export XRAY_XHTTP_PRIVATE_KEY="${XRAY_XHTTP_PRIVATE_KEY:-${XRAY_GRPC_PRIVATE_KEY:-}}"
    export XRAY_XHTTP_PUBLIC_KEY="${XRAY_XHTTP_PUBLIC_KEY:-${XRAY_GRPC_PUBLIC_KEY:-}}"
    export XRAY_XHTTP_SHORT_ID="${XRAY_XHTTP_SHORT_ID:-${XRAY_GRPC_SHORT_ID:-}}"
    export XRAY_XHTTP_SOCKS_PORT="${XRAY_XHTTP_SOCKS_PORT:-${XRAY_GRPC_SOCKS_PORT:-1081}}"

    persist_env_default "TUIC_SERVER" "${TUIC_SERVER:-${VPS_IP:-}}"
    persist_env_default "TUIC_SERVER_NAME" "${TUIC_SERVER_NAME:-${TUIC_SERVER:-${VPS_IP:-}}}"
    persist_env_default "TUIC_PORT" "${TUIC_PORT:-8448}"
    persist_env_default "TUIC_UUID" "${TUIC_UUID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
    persist_env_default "TUIC_PASSWORD" "${TUIC_PASSWORD:-$(openssl rand -hex 16)}"
    persist_env_default "TUIC_SOCKS_PORT" "${TUIC_SOCKS_PORT:-1085}"
    persist_env_default "TROJAN_SERVER" "${TROJAN_SERVER:-${VPS_IP:-}}"
    persist_env_default "TROJAN_SERVER_NAME" "${TROJAN_SERVER_NAME:-${TROJAN_SERVER:-${VPS_IP:-}}}"
    persist_env_default "TROJAN_PORT" "${TROJAN_PORT:-8444}"
    persist_env_default "TROJAN_PASSWORD" "${TROJAN_PASSWORD:-$(openssl rand -hex 24)}"
    persist_env_default "TROJAN_SOCKS_PORT" "${TROJAN_SOCKS_PORT:-1086}"

    export TUIC_SERVER="${TUIC_SERVER:-${VPS_IP:-}}"
    export TUIC_SERVER_NAME="${TUIC_SERVER_NAME:-${TUIC_SERVER:-${VPS_IP:-}}}"
    export TUIC_PORT="${TUIC_PORT:-8448}"
    export TUIC_UUID="${TUIC_UUID:-}"
    export TUIC_PASSWORD="${TUIC_PASSWORD:-}"
    export TUIC_SOCKS_PORT="${TUIC_SOCKS_PORT:-1085}"
    export TROJAN_SERVER="${TROJAN_SERVER:-${VPS_IP:-}}"
    export TROJAN_SERVER_NAME="${TROJAN_SERVER_NAME:-${TROJAN_SERVER:-${VPS_IP:-}}}"
    export TROJAN_PORT="${TROJAN_PORT:-8444}"
    export TROJAN_PASSWORD="${TROJAN_PASSWORD:-}"
    export TROJAN_SOCKS_PORT="${TROJAN_SOCKS_PORT:-1086}"
}

die() {
    log_error "$*"
    exit 1
}

is_mock_mode() {
    [[ "${DEPLOY_TEST_MODE:-0}" == "1" ]]
}

mock_phase_failed() {
    local phase="$1"
    local raw="${DEPLOY_FAIL_PHASES:-${DEPLOY_FAIL_PHASE:-}}"
    [[ -n "$raw" ]] || return 1
    local item
    IFS=',' read -r -a items <<< "$raw"
    for item in "${items[@]}"; do
        if [[ "${item// /}" == "$phase" ]]; then
            return 0
        fi
    done
    return 1
}

json_get() {
    local file="$1"
    local key="$2"
    [[ -f "$file" ]] || return 0
    python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    sys.exit(0)
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

write_json_file() {
    local file="$1"
    local payload="$2"
    mkdir -p "$(dirname "$file")"
    python3 - "$file" "$payload" <<'PY'
import json, sys
path, payload = sys.argv[1], sys.argv[2]
data = json.loads(payload)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY
}

update_state_file() {
    local file="$1"
    local payload="$2"
    write_json_file "$file" "$payload"
}

set_last_attempt() {
    local status="$1"
    local phase="$2"
    local message="${3:-}"
    local payload
    payload="$(python3 - "$status" "$phase" "$message" <<'PY'
import json, sys
status, phase, message = sys.argv[1:4]
print(json.dumps({
    "status": status,
    "phase": phase,
    "message": message,
}, ensure_ascii=False))
PY
)"
    update_state_file "$LAST_ATTEMPT_FILE" "$payload"
}

json_get_string() {
    local key="$1"
    python3 - "$key" <<'PY'
import json, sys
key = sys.argv[1]
raw = sys.stdin.read().strip()
if not raw:
    sys.exit(0)
data = json.loads(raw)
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    sys.exit(0)
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

ensure_git_repo() {
    if [[ -d "$REPO_DIR/.git" ]]; then
        return 0
    fi

    log_warn "/opt/vpn/.git отсутствует — восстанавливаем git metadata"

    local tmp_clone
    tmp_clone="$(mktemp -d /tmp/vpn-repo-bootstrap.XXXXXX)"
    local cloned=false
    local github_url="${GITHUB_REPO_URL:-$GITHUB_REPO_URL_DEFAULT}"

    if [[ -n "${VPS_IP:-}" ]]; then
        local ssh_port="${VPS_SSH_PORT:-22}"
        local vps_mirror="ssh://sysadmin@${VPS_IP}:${ssh_port}/opt/vpn/vpn-repo.git"
        local proxy_cmd=""
        [[ -x "$SSH_PROXY_CMD" ]] && proxy_cmd="-o ProxyCommand='$SSH_PROXY_CMD %h %p'"
        if GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes ${proxy_cmd}" \
           git clone --no-checkout "$vps_mirror" "$tmp_clone" >/dev/null 2>&1; then
            log_info "git metadata восстановлена из VPS-зеркала"
            cloned=true
        else
            log_warn "VPS-зеркало недоступно для bootstrap .git"
        fi
    fi

    if [[ "$cloned" == false ]]; then
        if git clone --no-checkout "$github_url" "$tmp_clone" >/dev/null 2>&1; then
            log_info "git metadata восстановлена из GitHub"
            cloned=true
        else
            rm -rf "$tmp_clone"
            log_error "Не удалось восстановить .git ни из VPS-зеркала, ни из GitHub"
            return 1
        fi
    fi

    rm -rf "$REPO_DIR/.git"
    mv "$tmp_clone/.git" "$REPO_DIR/.git"
    rm -rf "$tmp_clone"
    git -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
}

tracked_tree_clean() {
    ! git -C "$REPO_DIR" status --porcelain --untracked-files=no | grep -q .
}

release_id_for_sha() {
    local sha="$1"
    echo "${sha:0:12}"
}

read_current_release() {
    if [[ -f "$CURRENT_STATE_FILE" ]]; then
        CURRENT_RELEASE_ID="$(json_get "$CURRENT_STATE_FILE" "current_release.id")"
        CURRENT_RELEASE_SHA="$(json_get "$CURRENT_STATE_FILE" "current_release.sha")"
        CURRENT_RELEASE_VERSION="$(json_get "$CURRENT_STATE_FILE" "current_release.version")"
        PREVIOUS_RELEASE_ID="$(json_get "$CURRENT_STATE_FILE" "previous_release.id")"
        PREVIOUS_RELEASE_SHA="$(json_get "$CURRENT_STATE_FILE" "previous_release.sha")"
        PREVIOUS_RELEASE_VERSION="$(json_get "$CURRENT_STATE_FILE" "previous_release.version")"
        return 0
    fi

    CURRENT_RELEASE_SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
    CURRENT_RELEASE_VERSION="$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")"
    CURRENT_RELEASE_ID="$(release_id_for_sha "$CURRENT_RELEASE_SHA")"
    PREVIOUS_RELEASE_ID=""
    PREVIOUS_RELEASE_SHA=""
    PREVIOUS_RELEASE_VERSION=""
}

write_current_release_state() {
    local status="$1"
    local message="$2"
    local payload
    payload="$(python3 - \
        "$CURRENT_RELEASE_ID" "$CURRENT_RELEASE_SHA" "$CURRENT_RELEASE_VERSION" \
        "$PREVIOUS_RELEASE_ID" "$PREVIOUS_RELEASE_SHA" "$PREVIOUS_RELEASE_VERSION" \
        "$status" "$message" <<'PY'
import json, sys
cur_id, cur_sha, cur_ver, prev_id, prev_sha, prev_ver, status, message = sys.argv[1:9]
print(json.dumps({
    "current_release": {"id": cur_id, "sha": cur_sha, "version": cur_ver},
    "previous_release": {"id": prev_id, "sha": prev_sha, "version": prev_ver},
    "status": status,
    "message": message,
}, ensure_ascii=False))
PY
)"
    update_state_file "$CURRENT_STATE_FILE" "$payload"
}

write_pending_release_state() {
    local phase="$1"
    local status="$2"
    local message="$3"
    local payload
    payload="$(python3 - \
        "${TARGET_RELEASE_ID:-}" "${TARGET_RELEASE_SHA:-}" "${TARGET_RELEASE_VERSION:-}" \
        "$phase" "$status" "$message" \
        "$CURRENT_RELEASE_ID" "$CURRENT_RELEASE_SHA" "$CURRENT_RELEASE_VERSION" <<'PY'
import json, sys
target_id, target_sha, target_ver, phase, status, message, base_id, base_sha, base_ver = sys.argv[1:10]
print(json.dumps({
    "pending_release": {"id": target_id, "sha": target_sha, "version": target_ver},
    "base_release": {"id": base_id, "sha": base_sha, "version": base_ver},
    "phase": phase,
    "status": status,
    "message": message,
}, ensure_ascii=False))
PY
)"
    update_state_file "$PENDING_STATE_FILE" "$payload"
}

clear_pending_state() {
    rm -f "$PENDING_STATE_FILE"
}

vps_exec() {
    if is_mock_mode; then
        bash -lc "$*"
        return $?
    fi
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    fi
    ssh -p "$port" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        "${proxy_opts[@]}" \
        "sysadmin@${VPS_IP:-localhost}" "$@"
}

vps_copy_stdin_to_file() {
    local remote_file="$1"
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    fi
    ssh -p "$port" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        "${proxy_opts[@]}" \
        "sysadmin@${VPS_IP:-localhost}" "cat > '$remote_file'"
}

vps_read_file() {
    local remote_file="$1"
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    fi
    ssh -p "$port" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        "${proxy_opts[@]}" \
        "sysadmin@${VPS_IP:-localhost}" "cat '$remote_file' 2>/dev/null"
}

vps_read_json_key() {
    local remote_file="$1"
    local key="$2"
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    fi
    ssh -p "$port" -i "$SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        "${proxy_opts[@]}" \
        "sysadmin@${VPS_IP:-localhost}" python3 - "$remote_file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    sys.exit(0)
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

vps_tmux_exec() {
    local cmd="$1"
    if is_mock_mode; then
        bash -lc "$cmd"
        return $?
    fi
    local timeout="${2:-300}"
    local port="${VPS_SSH_PORT:-22}"
    local proxy_opts=()
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        proxy_opts+=(-o "ProxyCommand=${SSH_PROXY_CMD} %h %p")
    fi
    local -a ssh_base=( ssh -p "$port" -i "$SSH_KEY"
        -o StrictHostKeyChecking=no -o BatchMode=yes
        -o ConnectTimeout=15
        "${proxy_opts[@]}" )
    local session="deploy_${$}_$(date +%s%N | tail -c 9)"
    local out_file="/tmp/${session}.out"
    local rc_file="/tmp/${session}.rc"

    "${ssh_base[@]}" "sysadmin@${VPS_IP}" \
        "tmux new-session -d -s '${session}' 'bash -lc \"${cmd//\"/\\\"}\" > ${out_file} 2>&1; echo \$? > ${rc_file}'" \
        >/dev/null 2>&1 || return 1

    local elapsed=0
    while (( elapsed < timeout )); do
        sleep 3
        elapsed=$(( elapsed + 3 ))
        if "${ssh_base[@]}" "sysadmin@${VPS_IP}" "[ -f '${rc_file}' ] && echo done" 2>/dev/null | grep -q done; then
            break
        fi
    done

    local output rc_val
    output=$("${ssh_base[@]}" "sysadmin@${VPS_IP}" "cat '${out_file}' 2>/dev/null" 2>/dev/null || true)
    rc_val=$("${ssh_base[@]}" "sysadmin@${VPS_IP}" "cat '${rc_file}' 2>/dev/null" 2>/dev/null || echo "1")
    "${ssh_base[@]}" "sysadmin@${VPS_IP}" \
        "tmux kill-session -t '${session}' 2>/dev/null; rm -f '${out_file}' '${rc_file}'" >/dev/null 2>&1 || true

    [[ -n "$output" ]] && echo "$output"
    return "${rc_val:-1}"
}

vps_rsync_ssh() {
    local ssh_port="${VPS_SSH_PORT:-22}"
    local rsync_ssh="ssh -p $ssh_port -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes"
    if [[ "$DEPLOY_USE_SSH_PROXY" == "1" && -x "$SSH_PROXY_CMD" ]]; then
        rsync_ssh+=" -o ProxyCommand='${SSH_PROXY_CMD} %h %p'"
    fi
    echo "$rsync_ssh"
}

sync_state_to_vps() {
    local file remote_file

    if is_mock_mode; then
        local mock_dir="${VPS_STATE_DIR:-}"
        [[ -n "$mock_dir" ]] || return 0
        mkdir -p "$mock_dir"
        for file in current.json pending.json last-attempt.json; do
            if [[ -f "$STATE_DIR/$file" ]]; then
                cp "$STATE_DIR/$file" "$mock_dir/$file"
            else
                rm -f "$mock_dir/$file"
            fi
        done
        return 0
    fi

    [[ -n "${VPS_IP:-}" ]] || return 0
    vps_exec "mkdir -p '$REMOTE_STATE_DIR'" >/dev/null

    for file in current.json pending.json last-attempt.json; do
        remote_file="$REMOTE_STATE_DIR/$file"
        if [[ -f "$STATE_DIR/$file" ]]; then
            vps_copy_stdin_to_file "$remote_file" < "$STATE_DIR/$file"
        else
            vps_exec "rm -f '$remote_file'" >/dev/null || true
        fi
    done
}

remote_state_get() {
    local file="$1"
    local key="$2"

    if is_mock_mode; then
        local mock_dir="${VPS_STATE_DIR:-}"
        [[ -n "$mock_dir" ]] || return 0
        json_get "$mock_dir/$file" "$key"
        return 0
    fi

    local raw
    raw="$(vps_read_json_key "$REMOTE_STATE_DIR/$file" "$key" 2>/dev/null | tr -d '\r\n' || true)"
    if [[ -n "$raw" ]]; then
        printf '%s' "$raw"
        return 0
    fi

    raw="$(vps_read_file "$REMOTE_STATE_DIR/$file" || true)"
    [[ -n "$raw" ]] || return 0
    printf '%s' "$raw" | json_get_string "$key"
}

smoke_failures_from_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    awk '/^[[:space:]]+\[FAIL\][[:space:]]+/ { print $2 }' "$file" | sort -u
}

collect_baseline_smoke_failures() {
    is_mock_mode && return 0

    local smoke_log
    smoke_log="$(mktemp /tmp/vpn-smoke-baseline.XXXXXX.log)"
    if timeout "$SMOKE_TIMEOUT" bash "$REPO_DIR/tests/run-smoke-tests.sh" >"$smoke_log" 2>&1; then
        BASELINE_SMOKE_FAILURES=""
        rm -f "$smoke_log"
        return 0
    fi

    BASELINE_SMOKE_FAILURES="$(smoke_failures_from_file "$smoke_log" || true)"
    if [[ -n "$BASELINE_SMOKE_FAILURES" ]]; then
        log_warn "Pre-existing провалы (не будут причиной отката):"
        while IFS= read -r failed; do
            [[ -n "$failed" ]] || continue
            log_warn "  - $failed"
        done <<< "$BASELINE_SMOKE_FAILURES"
    else
        log_warn "Baseline smoke завершился с ошибкой без списка FAIL-тестов"
    fi
    cat "$smoke_log" >> "$LOG_FILE" 2>/dev/null || true
    rm -f "$smoke_log"
}

fetch_target_release() {
    local source_ref=""
    TARGET_SOURCE_REMOTE=""

    if is_mock_mode; then
        TARGET_SOURCE_REMOTE="mock"
        TARGET_SOURCE_REF="mock/main"
        TARGET_RELEASE_SHA="${MOCK_TARGET_RELEASE_SHA:-1111111111111111111111111111111111111111}"
        TARGET_RELEASE_VERSION="${MOCK_TARGET_RELEASE_VERSION:-v.mock}"
        TARGET_RELEASE_ID="$(release_id_for_sha "$TARGET_RELEASE_SHA")"
        return 0
    fi

    ensure_git_repo

    if [[ -n "${VPS_IP:-}" ]]; then
        local ssh_port="${VPS_SSH_PORT:-22}"
        local current_url
        current_url=$(git -C "$REPO_DIR" remote get-url vps-mirror 2>/dev/null || true)
        if [[ -z "$current_url" ]]; then
            git -C "$REPO_DIR" remote add vps-mirror \
                "ssh://sysadmin@${VPS_IP}:${ssh_port}/opt/vpn/vpn-repo.git"
        elif [[ "$current_url" != *"${VPS_IP}"* ]]; then
            git -C "$REPO_DIR" remote set-url vps-mirror \
                "ssh://sysadmin@${VPS_IP}:${ssh_port}/opt/vpn/vpn-repo.git"
        fi

        local proxy_cmd=""
        [[ -x "$SSH_PROXY_CMD" ]] && proxy_cmd="-o ProxyCommand='$SSH_PROXY_CMD %h %p'"
        if GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes ${proxy_cmd}" \
           git -C "$REPO_DIR" fetch --tags vps-mirror '+refs/heads/*:refs/remotes/vps-mirror/*' >/dev/null 2>&1; then
            TARGET_SOURCE_REMOTE="vps-mirror"
        fi
    fi

    if [[ -z "$TARGET_SOURCE_REMOTE" ]]; then
        git -C "$REPO_DIR" fetch --tags origin '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1 || return 1
        TARGET_SOURCE_REMOTE="origin"
    fi

    for candidate in \
        "refs/remotes/${TARGET_SOURCE_REMOTE}/master" \
        "refs/remotes/${TARGET_SOURCE_REMOTE}/main" \
        "refs/remotes/origin/master" \
        "refs/remotes/origin/main"; do
        if git -C "$REPO_DIR" rev-parse --verify --quiet "$candidate" >/dev/null; then
            source_ref="$candidate"
            break
        fi
    done
    [[ -n "$source_ref" ]] || return 1

    TARGET_SOURCE_REF="$source_ref"
    TARGET_RELEASE_SHA="$(git -C "$REPO_DIR" rev-parse "$source_ref")"
    TARGET_RELEASE_VERSION="$(git -C "$REPO_DIR" show "${source_ref}:version" 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$TARGET_RELEASE_VERSION" ]] || TARGET_RELEASE_VERSION="unknown"
    TARGET_RELEASE_ID="$(release_id_for_sha "$TARGET_RELEASE_SHA")"
}

checkout_release() {
    local sha="$1"
    if is_mock_mode; then
        mock_phase_failed "checkout" && return 1
        return 0
    fi
    git -C "$REPO_DIR" checkout -B "$DEPLOY_BRANCH" "$sha" >/dev/null 2>&1
}

changed_between() {
    local from_sha="$1"
    local to_sha="$2"
    shift 2
    [[ -n "$from_sha" && -n "$to_sha" ]] || return 0
    git -C "$REPO_DIR" diff --quiet "$from_sha" "$to_sha" -- "$@" 2>/dev/null
    [[ $? -ne 0 ]]
}

create_snapshot() {
    log_step "Создание snapshot текущего release"
    mkdir -p "$SNAPSHOT_DIR" "$STATE_DIR"

    local snap_id snap_path current_ver
    snap_id="$(date +%Y%m%d_%H%M%S)"
    snap_path="$SNAPSHOT_DIR/$snap_id"
    mkdir -p "$snap_path"
    current_ver="$(cat "$REPO_DIR/version" 2>/dev/null || echo "unknown")"

    local items=(
        "/etc/wireguard"
        "$ENV_FILE"
        "/etc/nftables.conf"
        "/etc/nftables-blocked-static.conf"
        "/etc/hysteria/config.yaml"
        "$REPO_DIR/home/xray"
        "$REPO_DIR/home/dnsmasq/dnsmasq.d"
        "/etc/vpn-routes"
        "$CURRENT_STATE_FILE"
    )

    local tar_args=()
    local item
    for item in "${items[@]}"; do
        [[ -e "$item" ]] && tar_args+=("$item")
    done

    local db_path="$REPO_DIR/telegram-bot/data/vpn_bot.db"
    if [[ -f "$db_path" ]]; then
        sqlite3 "$db_path" ".backup $snap_path/vpn_bot.db" 2>/dev/null || cp "$db_path" "$snap_path/vpn_bot.db"
        tar_args+=("$snap_path/vpn_bot.db")
    fi

    tar -czf "$snap_path/snapshot.tar.gz" --ignore-failed-read "${tar_args[@]}"

    local meta
    meta="$(python3 - \
        "$snap_id" "$CURRENT_RELEASE_ID" "$CURRENT_RELEASE_SHA" "$current_ver" \
        "${PREVIOUS_RELEASE_ID:-}" "${PREVIOUS_RELEASE_SHA:-}" "${PREVIOUS_RELEASE_VERSION:-}" <<'PY'
import json, sys
snap_id, cur_id, cur_sha, cur_ver, prev_id, prev_sha, prev_ver = sys.argv[1:8]
print(json.dumps({
    "snapshot_id": snap_id,
    "release": {"id": cur_id, "sha": cur_sha, "version": cur_ver},
    "previous_release": {"id": prev_id, "sha": prev_sha, "version": prev_ver},
}, ensure_ascii=False))
PY
)"
    write_json_file "$snap_path/meta.json" "$meta"
    echo "$snap_id" > "$SNAPSHOT_DIR/latest"
    CURRENT_SNAPSHOT_ID="$snap_id"
    CURRENT_SNAPSHOT_PATH="$snap_path"

    local old_snaps
    old_snaps=$(ls -1dt "$SNAPSHOT_DIR"/20*/ 2>/dev/null | tail -n +$((SNAPSHOT_KEEP + 1)) || true)
    if [[ -n "$old_snaps" ]]; then
        echo "$old_snaps" | xargs rm -rf
    fi

    log_ok "Snapshot создан: $snap_id"
}

validate_preflight() {
    log_step "Preflight"
    if is_mock_mode; then
        mock_phase_failed "preflight" && die "mock preflight failure"
        mkdir -p "$SNAPSHOT_DIR" "$STATE_DIR"
        return 0
    fi
    tracked_tree_clean || die "tracked source tree dirty — deploy остановлен"
    [[ -x "$REPO_DIR/tests/run-smoke-tests.sh" ]] || die "tests/run-smoke-tests.sh не найден"
    [[ -n "${VPS_IP:-}" ]] || die "VPS_IP не задан — consistency-first deploy невозможен"
    vps_exec "echo ok" >/dev/null || die "VPS недоступен по SSH"
}

apply_migrations() {
    local dir="$REPO_DIR/migrations"
    [[ -d "$dir" ]] || return 0
    touch "$MIGRATIONS_LOG"

    local migration name db_file ok
    db_file="$REPO_DIR/telegram-bot/data/vpn_bot.db"
    while IFS= read -r -d '' migration; do
        name="$(basename "$migration")"
        [[ "$name" == "apply.sh" ]] && continue
        grep -qxF "$name" "$MIGRATIONS_LOG" 2>/dev/null && continue
        log_info "Миграция: $name"
        ok=0
        case "$migration" in
            *.sql)
                [[ -f "$db_file" ]] || die "БД не найдена для миграции $name"
                sqlite3 "$db_file" < "$migration" >> "$LOG_FILE" 2>&1 && ok=1
                ;;
            *.sh)
                bash "$migration" >> "$LOG_FILE" 2>&1 && ok=1
                ;;
        esac
        [[ "$ok" -eq 1 ]] || die "Миграция $name завершилась с ошибкой"
        echo "$name" >> "$MIGRATIONS_LOG"
    done < <(find "$dir" \( -name "*.sh" -o -name "*.sql" \) -print0 | sort -z)
}

apply_system_configs() {
    local changed=false
    local src dst generate_nft server_mode
    src="$REPO_DIR/home/nftables/nftables.conf"
    dst="/etc/nftables.conf"
    generate_nft="$REPO_DIR/scripts/generate-nftables.sh"
    server_mode="${SERVER_MODE:-hosted}"

    if [[ -f "$src" ]] && ! cmp -s "$src" "$dst" 2>/dev/null; then
        if [[ "$server_mode" == "gateway" && -x "$generate_nft" ]]; then
            bash "$generate_nft" --check >/dev/null 2>&1 || die "generate-nftables.sh --check провалился"
            bash "$generate_nft" >> "$LOG_FILE" 2>&1
            nft -f /etc/nftables-blocked-static.conf >> "$LOG_FILE" 2>&1 || die "не удалось восстановить blocked_static"
        else
            nft -c -f "$src" >/dev/null 2>&1 || die "nftables.conf не прошёл валидацию"
            cp "$src" "$dst"
            nft -f "$dst" >> "$LOG_FILE" 2>&1
            nft -f /etc/nftables-blocked-static.conf >> "$LOG_FILE" 2>&1 || die "не удалось восстановить blocked_static"
        fi
        changed=true
    fi

    local units_changed=false
    local unit_src unit_name unit_dst
    for unit_src in "$REPO_DIR/home/systemd/"*; do
        [[ -f "$unit_src" ]] || continue
        unit_name="$(basename "$unit_src")"
        unit_dst="/etc/systemd/system/$unit_name"
        if [[ ! -f "$unit_dst" ]] || ! cmp -s "$unit_src" "$unit_dst"; then
            cp "$unit_src" "$unit_dst"
            units_changed=true
            changed=true
        fi
    done
    $units_changed && systemctl daemon-reload
    $changed && log_ok "Системные конфиги синхронизированы" || log_info "Системные конфиги без изменений"
}

render_xray_templates() {
    mkdir -p "$REPO_DIR/xray"
    mkdir -p "$REPO_DIR/sing-box"
    export XRAY_VISION_UUID="${XRAY_VISION_UUID:-${XRAY_XHTTP_UUID:-}}"
    export XRAY_VISION_PUBLIC_KEY="${XRAY_VISION_PUBLIC_KEY:-${XRAY_XHTTP_PUBLIC_KEY:-}}"
    export XRAY_VISION_SHORT_ID="${XRAY_VISION_SHORT_ID:-${XRAY_XHTTP_SHORT_ID:-}}"
    local tmpl name result unresolved
    for tmpl in "$REPO_DIR/home/xray/"*.json; do
        [[ -f "$tmpl" ]] || continue
        name="$(basename "$tmpl")"
        result="$(envsubst < "$tmpl")"
        unresolved="$(echo "$result" | grep -oE '\$\{[^}]+\}' | sort -u | tr '\n' ' ' || true)"
        [[ -z "$unresolved" ]] || die "Шаблон $name содержит незамещённые переменные: $unresolved"
        printf "%s" "$result" > "$REPO_DIR/xray/$name"
    done
    for tmpl in "$REPO_DIR/home/sing-box/"*.json; do
        [[ -f "$tmpl" ]] || continue
        name="$(basename "$tmpl")"
        result="$(envsubst < "$tmpl")"
        unresolved="$(echo "$result" | grep -oE '\$\{[^}]+\}' | sort -u | tr '\n' ' ' || true)"
        [[ -z "$unresolved" ]] || die "Шаблон $name содержит незамещённые переменные: $unresolved"
        printf "%s" "$result" > "$REPO_DIR/sing-box/$name"
    done
    rm -f "$REPO_DIR/xray/config-reality.json" "$REPO_DIR/xray/config-grpc.json" 2>/dev/null || true

    if [[ -n "${CF_CDN_HOSTNAME:-}" ]]; then
        export CF_CDN_UUID="${CF_CDN_UUID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
        python3 - <<PY
import json, os
cfg = {
    "log": {"loglevel": "warning"},
    "inbounds": [{"listen": "127.0.0.1", "port": 1082, "protocol": "socks", "settings": {"udp": True}}],
    "outbounds": [
        {"protocol": "vless", "tag": "vless-xhttp-cdn-out", "settings": {"vnext": [{"address": os.environ["CF_CDN_HOSTNAME"], "port": 443, "users": [{"id": os.environ["CF_CDN_UUID"], "encryption": "none", "flow": ""}]}]}, "streamSettings": {"network": "splithttp", "security": "tls", "tlsSettings": {"serverName": os.environ["CF_CDN_HOSTNAME"], "alpn": ["h2", "http/1.1"], "allowInsecure": False}, "splithttpSettings": {"path": "/vpn-cdn", "host": os.environ["CF_CDN_HOSTNAME"], "xPaddingBytes": "100-1000"}}},
        {"protocol": "freedom", "tag": "direct"}
    ],
    "routing": {"domainStrategy": "IPIfNonMatch", "rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]}
}
with open("$REPO_DIR/xray/config-cdn.json", "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=4)
PY
    fi
}

sync_home_runtime() {
    log_step "Применение release на home"

    if is_mock_mode; then
        local phase="apply-home"
        [[ "${ROLLBACK_MODE:-false}" == "true" ]] && phase="rollback-home"
        mock_phase_failed "$phase" && die "mock failure at ${phase}"
        return 0
    fi

    rsync -a "$REPO_DIR/home/docker-compose.yml" "$REPO_DIR/docker-compose.yml"
    rsync -a --delete --exclude="data/" "$REPO_DIR/home/telegram-bot/" "$REPO_DIR/telegram-bot/"
    rsync -a --delete "$REPO_DIR/home/prometheus/" "$REPO_DIR/prometheus/"
    rsync -a --delete "$REPO_DIR/home/grafana/" "$REPO_DIR/grafana/"
    rsync -a --delete "$REPO_DIR/home/alertmanager/" "$REPO_DIR/alertmanager/"
    rsync -a --delete "$REPO_DIR/home/nginx/" "$REPO_DIR/nginx/"
    rsync -a --delete "$REPO_DIR/home/sing-box/" "$REPO_DIR/sing-box/"
    rsync -a "$REPO_DIR/home/watchdog/watchdog.py" "$REPO_DIR/watchdog/watchdog.py"
    rsync -a --delete "$REPO_DIR/home/watchdog/plugins/" "$REPO_DIR/watchdog/plugins/"
    rsync -a --delete "$REPO_DIR/home/scripts/" "$REPO_DIR/scripts/"
    chmod +x "$REPO_DIR/scripts/"*.sh 2>/dev/null || true
    ln -sfn "$REPO_DIR/.env" "$REPO_DIR/home/.env"
    rm -rf "$REPO_DIR/watchdog/plugins/reality" "$REPO_DIR/watchdog/plugins/reality-grpc" 2>/dev/null || true

    render_xray_templates

    if changed_between "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" home/watchdog/requirements.txt; then
        [[ -x "$REPO_DIR/watchdog/venv/bin/pip" ]] || die "watchdog venv pip не найден"
        "$REPO_DIR/watchdog/venv/bin/pip" install -q --no-cache-dir -r "$REPO_DIR/home/watchdog/requirements.txt"
    fi

    if [[ "${ROLLBACK_MODE:-false}" != "true" ]]; then
        apply_migrations
    fi
    apply_system_configs

    local rebuild_bot=false rebuild_xray=false bot_no_cache=""
    changed_between "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" home/telegram-bot/ && rebuild_bot=true
    changed_between "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" home/xray/ && rebuild_xray=true
    [[ "${FORCE_DEPLOY:-false}" == "true" ]] && rebuild_bot=true && rebuild_xray=true

    docker compose -f "$REPO_DIR/docker-compose.yml" pull

    if $rebuild_bot; then
        changed_between "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" home/telegram-bot/requirements.txt && bot_no_cache="--no-cache"
        local bot_git_hash
        bot_git_hash="$(git -C "$REPO_DIR" log -1 --format="%H" -- home/telegram-bot/ 2>/dev/null || echo "unknown")"
        (cd "$REPO_DIR" && docker compose build $bot_no_cache --build-arg GIT_HASH="$bot_git_hash" telegram-bot)
    fi
    if $rebuild_xray; then
        (cd "$REPO_DIR" && docker compose up -d --force-recreate xray-client-xhttp xray-client-cdn xray-client-vision sing-box-tuic-client sing-box-trojan-client)
    fi
    (cd "$REPO_DIR" && docker compose --profile extra-stacks pull sing-box-tuic-client sing-box-trojan-client)
    (cd "$REPO_DIR" && docker compose --profile extra-stacks up -d sing-box-tuic-client sing-box-trojan-client)

    (cd "$REPO_DIR" && docker compose up -d --remove-orphans)
    systemctl restart watchdog
}

vps_any_changed() {
    changed_between "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" vps/
}

deploy_vps() {
    log_step "Применение release на VPS"
    [[ -n "${VPS_IP:-}" ]] || die "VPS_IP не задан"

    if is_mock_mode; then
        local phase="apply-vps"
        [[ "${ROLLBACK_MODE:-false}" == "true" ]] && phase="rollback-vps"
        sync_state_to_vps
        mock_phase_failed "$phase" && die "mock failure at ${phase}"
        return 0
    fi

    local rsync_ssh
    rsync_ssh="$(vps_rsync_ssh)"
    local vps_target="sysadmin@${VPS_IP}"

    if vps_any_changed || [[ "${FORCE_DEPLOY:-false}" == "true" ]]; then
        vps_exec "mkdir -p /opt/vpn/nginx /opt/vpn/scripts /opt/vpn/prometheus /opt/vpn/alertmanager /opt/vpn/grafana/provisioning /opt/vpn/.deploy-state /opt/vpn/sing-box" >/dev/null
        rsync -e "$rsync_ssh" -a "$REPO_DIR/vps/docker-compose.yml" "${vps_target}:/opt/vpn/docker-compose.yml"
        rsync -e "$rsync_ssh" -a --delete --exclude="ssl/" --exclude="mtls/" "$REPO_DIR/vps/nginx/" "${vps_target}:/opt/vpn/nginx/"
        rsync -e "$rsync_ssh" -a --delete "$REPO_DIR/vps/sing-box/" "${vps_target}:/opt/vpn/sing-box/"
        rsync -e "$rsync_ssh" -a --delete "$REPO_DIR/vps/scripts/" "${vps_target}:/opt/vpn/scripts/"
        rsync -e "$rsync_ssh" -a --delete "$REPO_DIR/vps/prometheus/" "${vps_target}:/opt/vpn/prometheus/"
        rsync -e "$rsync_ssh" -a --delete "$REPO_DIR/vps/alertmanager/" "${vps_target}:/opt/vpn/alertmanager/"
        rsync -e "$rsync_ssh" -a --delete "$REPO_DIR/vps/grafana/provisioning/" "${vps_target}:/opt/vpn/grafana/provisioning/"
    fi

    sync_state_to_vps
    envsubst < "$REPO_DIR/vps/sing-box/tuic-server.json" | vps_copy_stdin_to_file "/opt/vpn/sing-box/tuic-server.json"
    envsubst < "$REPO_DIR/vps/sing-box/trojan-server.json" | vps_copy_stdin_to_file "/opt/vpn/sing-box/trojan-server.json"

    local cmd
    cmd="sudo -n bash -lc 'set -euo pipefail; cd /opt/vpn; chmod +x /opt/vpn/scripts/*.sh 2>/dev/null || true; bash /opt/vpn/scripts/render-reality-xhttp-config.sh; docker compose pull; docker compose --profile extra-stacks pull trojan-server tuic-server; docker compose up -d --remove-orphans; docker compose --profile extra-stacks up -d trojan-server tuic-server; mkdir -p \"$REMOTE_STATE_DIR\"'"
    vps_exec "$cmd" || die "VPS deploy завершился с ошибкой"
}

run_smoke_tests() {
    log_step "Smoke-тесты"
    local smoke_log
    smoke_log="$(mktemp /tmp/vpn-smoke-verify.XXXXXX.log)"
    local smoke_rc=0
    timeout "$SMOKE_TIMEOUT" bash "$REPO_DIR/tests/run-smoke-tests.sh" >"$smoke_log" 2>&1 || smoke_rc=$?
    cat "$smoke_log"

    local current_failures new_failures
    current_failures="$(smoke_failures_from_file "$smoke_log" || true)"
    if [[ -n "$BASELINE_SMOKE_FAILURES" ]]; then
        new_failures="$(comm -23 <(printf '%s\n' "$current_failures" | sed '/^$/d' | sort -u) <(printf '%s\n' "$BASELINE_SMOKE_FAILURES" | sed '/^$/d' | sort -u) || true)"
    else
        new_failures="$current_failures"
    fi
    rm -f "$smoke_log"

    if [[ -n "$new_failures" ]]; then
        die "Smoke suite introduced new failures: $(echo "$new_failures" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    fi
    if [[ $smoke_rc -ne 0 && -n "$current_failures" ]]; then
        log_warn "Smoke suite всё ещё содержит pre-existing провалы: $(echo "$current_failures" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
        return 0
    fi
    [[ $smoke_rc -eq 0 ]] || die "Smoke suite завершился с ошибкой"
}

watchdog_health_check() {
    local token
    token="$(grep '^WATCHDOG_API_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
    [[ -n "$token" ]] || die "WATCHDOG_API_TOKEN не найден"
    curl -sf --max-time 10 -H "Authorization: Bearer ${token}" http://127.0.0.1:8080/status >/dev/null
}

verify_vps_release_parity() {
    local remote_sha
    sync_state_to_vps
    if [[ -f "$PENDING_STATE_FILE" ]]; then
        remote_sha="$(remote_state_get "pending.json" "pending_release.sha" | tr -d '\r\n' || true)"
        [[ "$remote_sha" == "$TARGET_RELEASE_SHA" ]] || die "VPS pending release не совпадает с target SHA (expected $TARGET_RELEASE_SHA, got ${remote_sha:-empty})"
    else
        remote_sha="$(remote_state_get "current.json" "current_release.sha" | tr -d '\r\n' || true)"
        [[ "$remote_sha" == "$CURRENT_RELEASE_SHA" ]] || die "VPS current release не совпадает с current SHA (expected $CURRENT_RELEASE_SHA, got ${remote_sha:-empty})"
    fi
}

health_gate() {
    log_step "Health gate"
    if is_mock_mode; then
        local phase="verify"
        [[ "${ROLLBACK_MODE:-false}" == "true" ]] && phase="rollback-verify"
        verify_vps_release_parity
        mock_phase_failed "$phase" && die "mock failure at ${phase}"
        return 0
    fi
    watchdog_health_check
    run_smoke_tests
    verify_vps_release_parity
}

finalize_success() {
    PREVIOUS_RELEASE_ID="$CURRENT_RELEASE_ID"
    PREVIOUS_RELEASE_SHA="$CURRENT_RELEASE_SHA"
    PREVIOUS_RELEASE_VERSION="$CURRENT_RELEASE_VERSION"
    CURRENT_RELEASE_ID="$TARGET_RELEASE_ID"
    CURRENT_RELEASE_SHA="$TARGET_RELEASE_SHA"
    CURRENT_RELEASE_VERSION="$TARGET_RELEASE_VERSION"
    write_current_release_state "ready" "release applied"
    clear_pending_state
    set_last_attempt "success" "commit" "release ${CURRENT_RELEASE_ID} applied"
    sync_state_to_vps
}

resolve_snapshot_path() {
    local selector
    selector="$(cat "$SNAPSHOT_DIR/latest" 2>/dev/null || true)"
    [[ -n "$selector" ]] || die "Snapshot для rollback не найден"

    [[ -d "$SNAPSHOT_DIR/$selector" && -f "$SNAPSHOT_DIR/$selector/meta.json" ]] || die "Не найден latest snapshot: $selector"
    ROLLBACK_META_PATH="$SNAPSHOT_DIR/$selector/meta.json"
    ROLLBACK_SNAPSHOT_PATH="$(dirname "$ROLLBACK_META_PATH")"
}

mark_rollback_failed() {
    local message="$1"
    write_pending_release_state "rollback" "failed" "$message"
    set_last_attempt "rollback-failed" "rollback" "$message"
    sync_state_to_vps || true
    notify "🚨 *Rollback failed* — ${message}"
}

perform_rollback() {
    resolve_snapshot_path
    local target_id target_sha target_ver previous_id previous_sha previous_ver
    target_id="$(json_get "$ROLLBACK_META_PATH" "release.id")"
    target_sha="$(json_get "$ROLLBACK_META_PATH" "release.sha")"
    target_ver="$(json_get "$ROLLBACK_META_PATH" "release.version")"
    previous_id="$(json_get "$ROLLBACK_META_PATH" "previous_release.id")"
    previous_sha="$(json_get "$ROLLBACK_META_PATH" "previous_release.sha")"
    previous_ver="$(json_get "$ROLLBACK_META_PATH" "previous_release.version")"
    [[ -n "$target_sha" ]] || die "Snapshot meta не содержит release.sha"

    TARGET_RELEASE_ID="$target_id"
    TARGET_RELEASE_SHA="$target_sha"
    TARGET_RELEASE_VERSION="$target_ver"
    CURRENT_RELEASE_ID="$target_id"
    CURRENT_RELEASE_SHA="$target_sha"
    CURRENT_RELEASE_VERSION="$target_ver"
    PREVIOUS_RELEASE_ID="$previous_id"
    PREVIOUS_RELEASE_SHA="$previous_sha"
    PREVIOUS_RELEASE_VERSION="$previous_ver"
    ROLLBACK_MODE=true
    write_pending_release_state "rollback" "running" "rolling back to ${target_id}"
    set_last_attempt "running" "rollback" "rollback to ${target_id}"
    sync_state_to_vps || true
    notify "⚠️ *Rollback* → \`${target_id}\`"

    if ! (
        checkout_release "$TARGET_RELEASE_SHA"
        if [[ -f "$ROLLBACK_SNAPSHOT_PATH/snapshot.tar.gz" ]]; then
            tar -xzf "$ROLLBACK_SNAPSHOT_PATH/snapshot.tar.gz" -C /
        fi
        if [[ -f "$ROLLBACK_SNAPSHOT_PATH/vpn_bot.db" ]]; then
            mkdir -p "$REPO_DIR/telegram-bot/data"
            cp "$ROLLBACK_SNAPSHOT_PATH/vpn_bot.db" "$REPO_DIR/telegram-bot/data/vpn_bot.db"
        fi

        sync_home_runtime
        deploy_vps
        health_gate
    ); then
        mark_rollback_failed "rollback to ${target_id} failed"
        return 1
    fi

    write_current_release_state "ready" "rollback completed"
    clear_pending_state
    set_last_attempt "rollback-completed" "rollback" "rollback to ${target_id} completed"
    sync_state_to_vps || true
    notify "✅ *Rollback completed* → \`${target_id}\`"
}

auto_rollback_on_failure() {
    local reason="$1"
    log_error "$reason"
    write_pending_release_state "$(json_get "$PENDING_STATE_FILE" "phase")" "failed" "$reason"
    set_last_attempt "failed" "$(json_get "$PENDING_STATE_FILE" "phase")" "$reason"
    sync_state_to_vps || true
    notify "❌ *Deploy failed* — ${reason}"
    perform_rollback
}

show_status() {
    read_current_release
    echo ""
    echo "── Deploy Status ──────────────────────────────"
    echo "  Current release:  ${CURRENT_RELEASE_ID:-unknown} (${CURRENT_RELEASE_VERSION:-unknown})"
    echo "  Current sha:      ${CURRENT_RELEASE_SHA:-unknown}"
    echo "  Previous release: ${PREVIOUS_RELEASE_ID:-none} (${PREVIOUS_RELEASE_VERSION:-none})"
    echo "  Previous sha:     ${PREVIOUS_RELEASE_SHA:-none}"
    if [[ -f "$PENDING_STATE_FILE" ]]; then
        echo "  Pending:          $(json_get "$PENDING_STATE_FILE" "pending_release.id")"
        echo "  Pending phase:    $(json_get "$PENDING_STATE_FILE" "phase") / $(json_get "$PENDING_STATE_FILE" "status")"
        echo "  Pending message:  $(json_get "$PENDING_STATE_FILE" "message")"
    else
        echo "  Pending:          none"
    fi
    echo "  Last attempt:     $(json_get "$LAST_ATTEMPT_FILE" "status") / $(json_get "$LAST_ATTEMPT_FILE" "phase")"
    echo "  Last message:     $(json_get "$LAST_ATTEMPT_FILE" "message")"
    echo "  Latest snapshot:  $(cat "$SNAPSHOT_DIR/latest" 2>/dev/null || echo 'none')"
    echo "───────────────────────────────────────────────"
}

check_updates() {
    read_current_release
    fetch_target_release || die "Не удалось получить target release"
    validate_preflight

    echo ""
    echo "── Deploy Check ───────────────────────────────"
    echo "  Current release: ${CURRENT_RELEASE_ID} (${CURRENT_RELEASE_VERSION})"
    echo "  Target release:  ${TARGET_RELEASE_ID} (${TARGET_RELEASE_VERSION})"
    echo "  Target sha:      ${TARGET_RELEASE_SHA}"
    echo "  Source remote:   ${TARGET_SOURCE_REMOTE}"
    echo "  Rollback ready:  $( [[ -f "$SNAPSHOT_DIR/latest" ]] && echo yes || echo no )"
    echo "───────────────────────────────────────────────"

    if [[ "$CURRENT_RELEASE_SHA" == "$TARGET_RELEASE_SHA" ]]; then
        log_info "Remote release уже совпадает с текущим"
    else
        log_ok "Найден новый release"
    fi
}

do_deploy() {
    read_current_release
    fetch_target_release || die "Не удалось получить target release"
    validate_preflight

    collect_baseline_smoke_failures

    if [[ "$CURRENT_RELEASE_SHA" == "$TARGET_RELEASE_SHA" && "${FORCE_DEPLOY:-false}" != "true" ]]; then
        set_last_attempt "noop" "check" "release ${CURRENT_RELEASE_ID} already current"
        sync_state_to_vps || true
        log_info "Commit не изменился (${CURRENT_RELEASE_ID}) — deploy не требуется"
        return 0
    fi

    create_snapshot
    write_pending_release_state "prepare" "running" "preparing ${TARGET_RELEASE_ID}"
    set_last_attempt "running" "prepare" "preparing ${TARGET_RELEASE_ID}"
    sync_state_to_vps || true

    local changed_files
    changed_files="$(git -C "$REPO_DIR" diff --name-only "$CURRENT_RELEASE_SHA" "$TARGET_RELEASE_SHA" | head -20 || true)"
    log_info "Изменённые файлы:\n${changed_files:-"(нет)"}"

    if ! checkout_release "$TARGET_RELEASE_SHA"; then
        auto_rollback_on_failure "Не удалось переключить repo на target release"
        return 1
    fi

    write_pending_release_state "apply-home" "running" "applying home release ${TARGET_RELEASE_ID}"
    sync_state_to_vps || true
    if ! ( sync_home_runtime ); then
        auto_rollback_on_failure "Применение release на home завершилось с ошибкой"
        return 1
    fi

    write_pending_release_state "apply-vps" "running" "applying VPS release ${TARGET_RELEASE_ID}"
    sync_state_to_vps || true
    if ! ( deploy_vps ); then
        auto_rollback_on_failure "Применение release на VPS завершилось с ошибкой"
        return 1
    fi

    write_pending_release_state "verify" "running" "verifying release ${TARGET_RELEASE_ID}"
    sync_state_to_vps || true
    if ! ( health_gate ); then
        auto_rollback_on_failure "Health gate не пройден"
        return 1
    fi

    finalize_success
    notify "✅ *Обновлено* до \`${CURRENT_RELEASE_VERSION}\` (\`${CURRENT_RELEASE_ID}\`)"
    log_ok "Deploy ${CURRENT_RELEASE_ID} завершён успешно"
}

main() {
    if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
        usage
        exit 0
    fi

    if [[ "${1:-}" == "--rollback" && -n "${2:-}" ]]; then
        usage
        exit 1
    fi

    if [[ "$ALLOW_NON_ROOT" != "1" && "$EUID" -ne 0 ]]; then
        echo "Запустите: sudo bash deploy.sh"
        exit 1
    fi

    mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")" "$SNAPSHOT_DIR" "$STATE_DIR"
    echo "" >> "$LOG_FILE"
    echo "════ Deploy $(date '+%Y-%m-%d %H:%M:%S') ════" >> "$LOG_FILE"

    load_env

    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Деплой уже запущен ($LOCK_FILE)"

    case "${1:-}" in
        --check)
            check_updates
            ;;
        --force)
            FORCE_DEPLOY=true
            do_deploy
            ;;
        --status)
            show_status
            ;;
        --rollback)
            if [[ -n "${2:-}" ]]; then
                usage
                exit 1
            fi
            perform_rollback
            ;;
        "")
            FORCE_DEPLOY=false
            do_deploy
            ;;
        *)
            usage
            exit 1
            ;;
    esac
}

main "$@"

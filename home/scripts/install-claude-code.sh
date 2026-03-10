#!/usr/bin/env bash
# install-claude-code.sh — Установка Claude Code на домашний сервер
#
# Выполняет:
#   1. Проверяет/устанавливает git и Node.js 18+
#   2. npm install -g @anthropic-ai/claude-code
#   3. Клонирует репозиторий в /opt/vpn (если ещё не клонирован)
#   4. Выводит инструкцию запуска
#
# Использование:
#   bash install-claude-code.sh
#   bash install-claude-code.sh --skip-clone   (не клонировать репо)
#   bash install-claude-code.sh --repo-url <url>

set -euo pipefail

REPO_URL="${REPO_URL:-}"
VPN_DIR="/opt/vpn"
VPS_MIRROR_HOST="${VPS_IP:-}"
VPS_MIRROR_USER="sysadmin"
VPS_MIRROR_PATH="/opt/vpn/vpn-repo.git"
GITHUB_REPO="https://github.com/your-org/vpn-infra.git"  # заменить при публикации
SKIP_CLONE=false

# --- аргументы ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-clone)   SKIP_CLONE=true ;;
        --repo-url)     REPO_URL="$2"; shift ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
    shift
done

# --- цвета ---
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; RESET='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; RESET=''
fi

info()    { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Шаг 1: git
# ---------------------------------------------------------------------------
info "Проверка git..."
if ! command -v git &>/dev/null; then
    info "Устанавливаем git..."
    sudo apt-get update -qq
    sudo apt-get install -y git
fi
git --version

# ---------------------------------------------------------------------------
# Шаг 2: Node.js 18+
# ---------------------------------------------------------------------------
info "Проверка Node.js..."

node_ok=false
if command -v node &>/dev/null; then
    node_ver=$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)
    if [[ "${node_ver:-0}" -ge 18 ]]; then
        info "Node.js $(node --version) уже установлен."
        node_ok=true
    else
        warn "Node.js $(node --version) < 18. Обновляем..."
    fi
fi

if [[ "$node_ok" == false ]]; then
    info "Устанавливаем Node.js 22 LTS..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
    node --version
fi

# ---------------------------------------------------------------------------
# Шаг 3: Claude Code
# ---------------------------------------------------------------------------
info "Устанавливаем @anthropic-ai/claude-code..."
sudo npm install -g @anthropic-ai/claude-code

# проверка
if ! command -v claude &>/dev/null; then
    # npm global bin может не быть в PATH для root
    NPM_BIN=$(npm root -g 2>/dev/null | sed 's|/node_modules||')/.bin
    export PATH="$NPM_BIN:$PATH"
fi

claude --version 2>/dev/null || warn "claude не в PATH; добавьте $(npm root -g | sed 's|/node_modules||')/.bin в PATH"

# ---------------------------------------------------------------------------
# Шаг 4: Клонировать репозиторий в /opt/vpn (если нужно)
# ---------------------------------------------------------------------------
if [[ "$SKIP_CLONE" == true ]]; then
    info "Пропускаем клонирование репозитория (--skip-clone)."
elif [[ -d "$VPN_DIR/.git" ]]; then
    info "Репозиторий уже клонирован в $VPN_DIR. Обновляем..."
    git -C "$VPN_DIR" pull --ff-only 2>/dev/null || warn "git pull не удался — возможно, нет соединения с зеркалом."
else
    info "Клонируем репозиторий в $VPN_DIR..."
    sudo mkdir -p "$VPN_DIR"

    # Сначала пробуем VPS-зеркало (через SSH)
    cloned=false
    if [[ -n "$VPS_MIRROR_HOST" ]]; then
        info "Пробуем VPS-зеркало: $VPS_MIRROR_USER@$VPS_MIRROR_HOST:$VPS_MIRROR_PATH"
        if sudo git clone \
            "ssh://${VPS_MIRROR_USER}@${VPS_MIRROR_HOST}${VPS_MIRROR_PATH}" \
            "$VPN_DIR" 2>/dev/null; then
            info "Клонировано из VPS-зеркала."
            cloned=true
        else
            warn "VPS-зеркало недоступно. Пробуем GitHub..."
        fi
    fi

    if [[ "$cloned" == false ]]; then
        target_url="${REPO_URL:-$GITHUB_REPO}"
        info "Клонируем из: $target_url"
        if ! sudo git clone "$target_url" "$VPN_DIR"; then
            die "Не удалось клонировать репозиторий. Проверьте URL и доступность GitHub."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Готово
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║         Claude Code успешно установлен       ║${RESET}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo
echo "Запуск:"
echo
echo "    cd $VPN_DIR && claude"
echo
echo "При первом запуске потребуется авторизация (claude.ai или API-ключ)."
echo "Репозиторий $VPN_DIR уже открыт — Claude видит все конфиги."
echo
echo "Полезные вопросы для диагностики:"
echo "  «Проверь статус всех VPN-сервисов»"
echo "  «Покажи последние ошибки watchdog»"
echo "  «Что не так с туннелем?»"

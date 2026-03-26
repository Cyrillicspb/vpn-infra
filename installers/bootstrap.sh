#!/bin/bash
# bootstrap.sh — запускается внутри self-extracting install.sh
# $1 = директория с распакованным содержимым
set -euo pipefail

EXTRACT_DIR="${1:?bootstrap.sh: не передана директория}"
cd "$EXTRACT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${RESET}"; }
info() { echo -e "  → $*"; }
err()  { echo -e "${RED}  ✗ $*${RESET}" >&2; }

echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║    VPN Infrastructure — Установка        ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── Проверка root ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Требуются права root. Запустите: sudo bash install.sh"
    exit 1
fi
ok "Запущено от root"

# ── Проверка Python 3.10+ ─────────────────────────────────────────────────────
info "Проверка Python 3.10+..."
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    err "Python 3.10+ не найден. Установите: sudo apt-get install python3"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}")')
ok "Python $PY_VER"

# ── Установка pip если отсутствует ────────────────────────────────────────────
if ! python3 -m pip --version &>/dev/null 2>&1; then
    info "Установка python3-pip..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip 2>/dev/null \
        || { err "Не удалось установить python3-pip"; exit 1; }
fi
ok "pip доступен"

# ── Установка textual ─────────────────────────────────────────────────────────
if ! python3 -c 'import textual' 2>/dev/null; then
    WHEELS_DIR="$EXTRACT_DIR/wheels"
    if [[ -d "$WHEELS_DIR" ]] && ls "$WHEELS_DIR"/*.whl &>/dev/null 2>&1; then
        info "Установка textual из локальных wheel (офлайн)..."
        pip3 install --no-index --find-links="$WHEELS_DIR" textual \
            --break-system-packages --quiet \
            || { err "Не удалось установить textual из wheels"; exit 1; }
    else
        info "Установка textual из PyPI..."
        pip3 install 'textual>=0.47.0' --break-system-packages --quiet \
            || { err "Не удалось установить textual"; exit 1; }
    fi
fi
ok "textual установлен"

# ── Запуск TUI ────────────────────────────────────────────────────────────────
echo ""
info "Запуск установщика..."
exec python3 "$EXTRACT_DIR/installers/gui/installer.py"

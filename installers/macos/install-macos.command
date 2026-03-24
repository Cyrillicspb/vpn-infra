#!/usr/bin/env bash
# VPN Infrastructure — Установщик для macOS
# Двойной клик запускает установку.
# Требования: macOS 10.15+, SSH доступ к домашнему серверу (Ubuntu 24.04).

set -euo pipefail
cd "$(dirname "$0")" || exit 1

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

_ok()   { echo -e "${GREEN}  ✓ $*${RESET}"; }
_info() { echo -e "${BLUE}  → $*${RESET}"; }
_warn() { echo -e "${YELLOW}  ⚠ $*${RESET}"; }
_err()  { echo -e "${RED}  ✗ $*${RESET}"; }
_step() { echo -e "\n${BOLD}$*${RESET}"; }

clear
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║    VPN Infrastructure — Установка        ║${RESET}"
echo -e "${BOLD}║    macOS → домашний сервер (Ubuntu)      ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── [1/5] Проверка SSH ────────────────────────────────────────────────────────
_step "[1/5] Проверка SSH..."
if ! command -v ssh &>/dev/null; then
    _err "ssh не найден"
    echo "    Установите Xcode Command Line Tools: xcode-select --install"
    read -rp "Нажмите Enter для выхода..."; exit 1
fi
if ! command -v scp &>/dev/null; then
    _err "scp не найден"
    read -rp "Нажмите Enter для выхода..."; exit 1
fi
_ok "SSH доступен"

# ── [2/5] SSH-ключ и подключение ──────────────────────────────────────────────
_step "[2/5] SSH-ключ..."
SSH_KEY="$HOME/.ssh/vpn_deploy_key"
if [[ ! -f "$SSH_KEY" ]]; then
    _info "Создание SSH ключа..."
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "vpn-deploy-$(date +%Y%m%d)" -q
    _ok "Ключ создан: $SSH_KEY"
else
    _ok "Ключ существует: $SSH_KEY"
fi

# ── Данные сервера ────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Введите адрес вашего Ubuntu-сервера:${RESET}"
echo ""

while true; do
    read -rp "  IP-адрес сервера: " SERVER_IP
    [[ "$SERVER_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] && break
    _err "Неверный формат IP. Пример: 192.168.1.100"
done

read -rp "  SSH порт [22]: " SSH_PORT
SSH_PORT="${SSH_PORT:-22}"

SSH_OPTS=(-i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -p "$SSH_PORT")

echo ""

# ── Автоопределение пользователя ──────────────────────────────────────────────
_info "Определение пользователя..."
SERVER_USER=""
for try_user in sysadmin "$USER"; do
    if ssh -n -i "$SSH_KEY" -o "StrictHostKeyChecking=accept-new" -o "BatchMode=yes" -o "ConnectTimeout=5" -p "$SSH_PORT" "${try_user}@${SERVER_IP}" "exit 0" 2>/dev/null; then
        SERVER_USER="$try_user"
        _ok "Подключение как $SERVER_USER"
        break
    fi
done

if [[ -z "$SERVER_USER" ]]; then
    echo "  Ключ не установлен. Введите пользователя созданного при установке Ubuntu:"
    read -rp "  Пользователь: " SERVER_USER
    [[ -z "$SERVER_USER" ]] && SERVER_USER=sysadmin
    _info "Копирование SSH ключа на сервер..."
    echo "  (Потребуется пароль от ${SERVER_USER}@${SERVER_IP})"
    echo ""
    if ssh-copy-id -i "${SSH_KEY}.pub" \
        -o "StrictHostKeyChecking=accept-new" \
        -p "$SSH_PORT" \
        "${SERVER_USER}@${SERVER_IP}" 2>/dev/null; then
        _ok "Ключ скопирован"
    else
        _warn "ssh-copy-id не сработал. Добавьте ключ вручную на сервере:"
        echo "  echo '$(cat "${SSH_KEY}.pub")' >> ~/.ssh/authorized_keys"
        echo ""
        read -rp "  Нажмите Enter когда ключ добавлен..."
    fi
fi

echo "  Пользователь: $SERVER_USER"

# ── [3/5] Подготовка сервера ──────────────────────────────────────────────────
_step "[3/5] Подготовка сервера..."

# Проверка подключения
if ! ssh "${SSH_OPTS[@]}" -o "ConnectTimeout=10" -o "BatchMode=yes" \
    "${SERVER_USER}@${SERVER_IP}" "hostname" 2>/dev/null; then
    _err "Не удалось подключиться к ${SERVER_USER}@${SERVER_IP}:${SSH_PORT}"
    echo ""
    echo "  Проверьте: IP, пользователь, порт, SSH на сервере"
    read -rp "  Нажмите Enter для выхода..."; exit 1
fi
_ok "Сервер доступен: ${SERVER_USER}@${SERVER_IP}:${SSH_PORT}"

# Настройка sudo без пароля (потребуется пароль пользователя один раз)
echo ""
_info "Настройка прав (потребуется пароль ${SERVER_USER}):"
ssh -t "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}" \
    "echo '${SERVER_USER} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/vpn-installer > /dev/null && sudo chmod 440 /etc/sudoers.d/vpn-installer" \
    && _ok "sudo настроен" || _warn "Не удалось настроить sudo -- возможны запросы пароля"

# ── Подтверждение ─────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}  Установка займёт 20–40 минут. Не закрывайте окно.${RESET}"
echo ""
read -rp "  Начать установку? [y/N]: " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Отменено."; exit 0; }

# ── [4/5] Загрузка репозитория ────────────────────────────────────────────────
_step "[4/5] Загрузка репозитория..."

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SETUP_PATH="/opt/vpn/setup.sh"
UPLOAD_OK=0

if [[ -f "$REPO_ROOT/setup.sh" && -f "$REPO_ROOT/install-home.sh" && -d "$REPO_ROOT/home" ]]; then
    _info "Упаковка локального репозитория..."
    TMP_ARCHIVE="$(mktemp /tmp/vpn-infra-XXXXXX.tar.gz)"
    tar -czf "$TMP_ARCHIVE" \
        --exclude='.git' --exclude='*.pyc' --exclude='__pycache__' \
        --exclude='*/venv/*' --exclude='node_modules' --exclude='*.log' \
        --exclude='.env' \
        -C "$REPO_ROOT" . 2>/dev/null
    _info "Загрузка архива на сервер..."
    ssh "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}" \
        "sudo mkdir -p /opt/vpn && sudo chown ${SERVER_USER}:${SERVER_USER} /opt/vpn"
    scp -i "$SSH_KEY" -P "$SSH_PORT" -o "StrictHostKeyChecking=accept-new" \
        "$TMP_ARCHIVE" "${SERVER_USER}@${SERVER_IP}:/tmp/vpn-infra.tar.gz"
    ssh "${SSH_OPTS[@]}" "${SERVER_USER}@${SERVER_IP}" \
        "tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner --overwrite --touch 2>/dev/null; rm /tmp/vpn-infra.tar.gz"
    rm -f "$TMP_ARCHIVE"
    UPLOAD_OK=1
    _ok "Репозиторий загружен из локальной копии"
else
    _info "Скачивание последнего релиза с GitHub..."
    if ssh "${SSH_OPTS[@]}" -o "ServerAliveInterval=30" "${SERVER_USER}@${SERVER_IP}" \
        "curl -fsSL --max-time 120 -L https://github.com/Cyrillicspb/vpn-infra/releases/latest/download/vpn-infra.tar.gz -o /tmp/vpn-infra.tar.gz && sudo mkdir -p /opt/vpn && sudo tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner --overwrite --touch; rm -f /tmp/vpn-infra.tar.gz"; then
        UPLOAD_OK=1
        _ok "Релиз скачан с GitHub"
    else
        _err "Не удалось скачать релиз"
        echo "  Попробуйте вручную: scp vpn-infra.tar.gz ${SERVER_USER}@${SERVER_IP}:/tmp/"
        read -rp "  Нажмите Enter для выхода..."; exit 1
    fi
fi

# ── [5/5] Установка ───────────────────────────────────────────────────────────
_step "[5/5] Установка..."

TUI_INSTALLER="/opt/vpn/installers/gui/installer.py"
USE_TUI=0

# Проверяем Python 3.10+
_info "Проверка Python 3.10+ на сервере..."
PY_VER=0
PY_VER=$(ssh "${SSH_OPTS[@]}" -o "BatchMode=yes" -o "ConnectTimeout=5" \
    "${SERVER_USER}@${SERVER_IP}" \
    "python3 -c 'import sys; v=sys.version_info; print(v.major*100+v.minor)' 2>/dev/null || echo 0" \
    2>/dev/null || echo 0)

if [[ "$PY_VER" =~ ^[0-9]+$ ]] && [[ "$PY_VER" -ge 310 ]]; then
    FILE_OK=$(ssh "${SSH_OPTS[@]}" -o "BatchMode=yes" -o "ConnectTimeout=5" \
        "${SERVER_USER}@${SERVER_IP}" \
        "test -f '$TUI_INSTALLER' && echo yes || echo no" 2>/dev/null || echo no)
    if [[ "$FILE_OK" == "yes" ]]; then
        # Устанавливаем textual если нет
        _info "Установка TUI-компонентов..."
        ssh "${SSH_OPTS[@]}" -o "BatchMode=yes" "${SERVER_USER}@${SERVER_IP}" \
            "sudo apt-get install -y -qq python3-pip python3-venv 2>/dev/null; sudo pip3 install textual --break-system-packages --ignore-installed --quiet 2>/dev/null" || true
        # Проверяем что textual доступен
        if ssh "${SSH_OPTS[@]}" -o "BatchMode=yes" -o "ConnectTimeout=5" \
            "${SERVER_USER}@${SERVER_IP}" \
            "python3 -c 'import textual'" 2>/dev/null; then
            USE_TUI=1
            PY_DISPLAY=$(printf 'Python %d.%d' $((PY_VER/100)) $((PY_VER%100)))
            _ok "$PY_DISPLAY — TUI-установщик готов"
        else
            _warn "textual недоступен — откат на консольный режим"
        fi
    else
        _warn "installer.py не найден — откат на консольный режим"
    fi
else
    PY_DISPLAY=$(printf 'Python %d.%d' $((PY_VER/100)) $((PY_VER%100)))
    _warn "$PY_DISPLAY < 3.10 — используем консольный режим"
fi

echo ""

# ── Запуск TUI ────────────────────────────────────────────────────────────────
if [[ $USE_TUI -eq 1 ]]; then
    echo -e "${BOLD}▶ Запуск TUI-установщика...${RESET}"
    echo -e "${BLUE}══════════════════════════════════════════${RESET}"
    echo ""
    TUI_RC=0
    ssh -i "$SSH_KEY" \
        -o "StrictHostKeyChecking=accept-new" \
        -o "ServerAliveInterval=30" \
        -o "ServerAliveCountMax=10" \
        -p "$SSH_PORT" \
        -t "${SERVER_USER}@${SERVER_IP}" \
        "cd /opt/vpn && sudo python3 installers/gui/installer.py" \
        || TUI_RC=$?

    echo ""

    if [[ $TUI_RC -eq 0 ]]; then
        echo -e "${BLUE}══════════════════════════════════════════${RESET}"
        _ok "Готово!"
        echo ""
        echo "  Если установка завершена — следующие шаги:"
        echo "    1. Port Forwarding: UDP 51820+51821 → ${SERVER_IP}"
        echo "    2. Telegram: напишите /start вашему боту"
        echo "    3. /adddevice — получите конфиг WireGuard/AWG"
        echo ""
        read -rp "  Нажмите Enter для выхода..."
        exit 0
    fi

    _warn "TUI завершился с кодом $TUI_RC — откат на консольный режим..."
    echo ""
fi

# ── Fallback: tmux + setup.sh ─────────────────────────────────────────────────
echo -e "${BOLD}▶ Запуск setup.sh в tmux...${RESET}"
echo -e "${BLUE}══════════════════════════════════════════${RESET}"
echo ""

RESULT=0
ssh -i "$SSH_KEY" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "ServerAliveInterval=30" \
    -o "ServerAliveCountMax=10" \
    -p "$SSH_PORT" \
    -t "${SERVER_USER}@${SERVER_IP}" \
    "tmux new-session -A -s vpn-install 'sudo bash $SETUP_PATH'" \
    || RESULT=$?

echo ""
echo -e "${BLUE}══════════════════════════════════════════${RESET}"

if [[ $RESULT -eq 0 ]]; then
    _ok "Установка завершена успешно!"
    echo ""
    echo "  Следующие шаги:"
    echo "    1. Port Forwarding на роутере:"
    echo "       UDP 51820 → ${SERVER_IP}:51820  (AmneziaWG)"
    echo "       UDP 51821 → ${SERVER_IP}:51821  (WireGuard)"
    echo "    2. Telegram: напишите /start вашему боту"
    echo "    3. /adddevice — получите конфиг"
else
    _err "Установка завершилась с ошибкой (код $RESULT)"
    echo ""
    echo "  Диагностика:"
    echo "    ssh -i $SSH_KEY -p $SSH_PORT ${SERVER_USER}@${SERVER_IP}"
    echo "    cat /tmp/vpn-setup.log"
    echo ""
    echo "  Повторный запуск безопасен — выполненные шаги пропустятся."
fi

echo ""
read -rp "  Нажмите Enter для выхода..."

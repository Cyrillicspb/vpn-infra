#!/usr/bin/env bash
# install.sh — однострочный установщик vpn-infra
#
# Запуск на чистом Ubuntu-сервере:
#   curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/install.sh | sudo bash
#
# Что делает:
#   1. Клонирует репозиторий в /opt/vpn (git или tar-fallback из Releases)
#   2. Скачивает docker-images.tar.gz из GitHub Releases (образы фазы 1)
#   3. Распаковывает образы в /opt/vpn/docker-images/
#   4. Запускает setup.sh (TUI или консольный режим)
#
# Идемпотентен: повторный запуск не сломает уже установленное.

set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_OWNER="Cyrillicspb"
REPO_NAME="vpn-infra"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}"
RELEASE_API="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest"
OPT_VPN="/opt/vpn"
DOCKER_IMAGES_DIR="${OPT_VPN}/docker-images"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Запустите от root: sudo bash install.sh"
    exit 1
fi

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════╗"
echo "║       vpn-infra — bootstrap install     ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Минимальные зависимости ────────────────────────────────────────────────
info "Проверка базовых зависимостей (curl, git)..."
apt-get update -qq 2>/dev/null || true
for pkg in curl git; do
    if ! command -v "$pkg" &>/dev/null; then
        apt-get install -y -qq "$pkg" 2>/dev/null || true
    fi
done
ok "Базовые зависимости готовы"

# ── 2. Получение репозитория ──────────────────────────────────────────────────
info "Загрузка репозитория в ${OPT_VPN}..."
mkdir -p "$OPT_VPN"

_repo_ok=false

# Если репозиторий уже есть — не перескачиваем
if [[ -f "${OPT_VPN}/setup.sh" && -f "${OPT_VPN}/install-home.sh" ]]; then
    ok "Репозиторий уже есть в ${OPT_VPN} — пропускаем клонирование"
    _repo_ok=true
fi

# Попытка git clone
if ! $_repo_ok && command -v git &>/dev/null; then
    if timeout 60 git clone --depth=1 "$REPO_URL" /tmp/vpn-infra-clone 2>/dev/null; then
        cp -rT /tmp/vpn-infra-clone "$OPT_VPN"
        rm -rf /tmp/vpn-infra-clone
        _repo_ok=true
        ok "Репозиторий клонирован через git"
    else
        warn "git clone не удался (GitHub заблокирован?) — пробуем tar из Releases"
    fi
fi

# Fallback: tar из GitHub Releases
if ! $_repo_ok; then
    info "Скачиваем vpn-infra.tar.gz из GitHub Releases..."
    _tar_url="${REPO_URL}/releases/latest/download/vpn-infra.tar.gz"
    if curl -fsSL --max-time 120 -L "$_tar_url" -o /tmp/vpn-infra.tar.gz 2>/dev/null; then
        tar xzf /tmp/vpn-infra.tar.gz -C "$OPT_VPN" \
            --no-same-permissions --no-same-owner --overwrite --touch 2>/dev/null
        rm -f /tmp/vpn-infra.tar.gz
        _repo_ok=true
        ok "Репозиторий скачан из GitHub Releases"
    else
        err "Не удалось скачать репозиторий."
        err "Скопируйте репозиторий вручную: scp vpn-infra.tar.gz root@server:/tmp/"
        err "Затем: tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn && bash /opt/vpn/setup.sh"
        exit 1
    fi
fi

chmod +x "${OPT_VPN}/setup.sh" "${OPT_VPN}/install-home.sh" 2>/dev/null || true

# ── 3. Docker-образы фазы 1 из GitHub Releases ───────────────────────────────
# Образы нужны для поднятия VPN без доступа к Docker Hub:
#   python:3.12-slim, teddysun/xray, tecnativa/docker-socket-proxy, nginx:stable-alpine
#
# Если образы уже распакованы — пропускаем.
_img_count=$(ls "${DOCKER_IMAGES_DIR}"/*.tar.gz 2>/dev/null | wc -l || echo 0)
if [[ "$_img_count" -gt 0 ]]; then
    ok "Docker-образы уже есть: ${_img_count} файлов в ${DOCKER_IMAGES_DIR}"
else
    info "Поиск docker-images.tar.gz в GitHub Releases..."
    _docker_url=$(curl -sf --max-time 20 "$RELEASE_API" 2>/dev/null \
        | grep -o '"browser_download_url": *"[^"]*docker-images\.tar\.gz"' \
        | grep -o 'https://[^"]*' | head -1 || true)

    if [[ -n "$_docker_url" ]]; then
        info "Скачиваем docker-images.tar.gz (это может занять несколько минут)..."
        if curl -fsSL --max-time 600 -L "$_docker_url" \
                -o /tmp/docker-images.tar.gz 2>/dev/null; then
            mkdir -p "$DOCKER_IMAGES_DIR"
            tar xzf /tmp/docker-images.tar.gz -C "$DOCKER_IMAGES_DIR" \
                --no-same-permissions --no-same-owner --overwrite 2>/dev/null || true
            rm -f /tmp/docker-images.tar.gz
            _img_count=$(ls "${DOCKER_IMAGES_DIR}"/*.tar.gz 2>/dev/null | wc -l || echo 0)
            ok "Docker-образы фазы 1 готовы: ${_img_count} файлов"
        else
            warn "Не удалось скачать docker-images.tar.gz"
            warn "Образы будут скачаны через зеркала при установке (шаг 31)"
        fi
    else
        warn "docker-images.tar.gz не найден в релизе"
        warn "Сгенерируйте: bash dev/save-docker-images.sh (нужен доступ к Docker Hub)"
        warn "Образы будут скачаны через зеркала при установке (шаг 31)"
    fi
fi

# ── 4. Запуск setup.sh ────────────────────────────────────────────────────────
echo ""
info "Запускаем установщик setup.sh..."
echo ""
exec bash "${OPT_VPN}/setup.sh"

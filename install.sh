#!/usr/bin/env bash
# install.sh — официальный однострочный bootstrap StackInfra
#
# Запуск на чистом Ubuntu-сервере:
#   curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/install.sh | sudo bash
#
# Что делает:
#   1. Клонирует репозиторий в /opt/vpn (git или tar-fallback из Releases)
#   2. Скачивает docker-images*.tar.gz из GitHub Releases
#   3. Распаковывает локальный image cache в /opt/vpn/docker-images/
#   4. Запускает setup.sh (TUI или консольный режим)
#
# Идемпотентен: повторный запуск не сломает уже установленное.
# Это единственный поддерживаемый путь старта установки.

# Если install.sh запущен через stdin (curl | bash), материализуем его во временный
# файл и перезапускаем как обычный script file. Это убирает проблемы pipe/stdin/tty.
if [[ -z "${BASH_SOURCE[0]:-}" && "${0:-}" == "bash" ]]; then
    _self_tmp="$(mktemp /tmp/vpn-bootstrap.XXXXXX.sh)"
    cat >"$_self_tmp"
    chmod 700 "$_self_tmp"
    exec bash "$_self_tmp" "$@"
fi

set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export VPN_STRICT_BUNDLE=1

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

read_install_version() {
    local version_file="$1"
    local version_value=""
    if [[ -f "$version_file" ]]; then
        version_value="$(tr -d '[:space:]' < "$version_file" 2>/dev/null || true)"
    fi
    case "$version_value" in
        ''|*[!0-9.]*)
            return 1
            ;;
        *)
            printf '%s' "$version_value"
            ;;
    esac
}

count_tar_archives() {
    local dir="$1"
    local files=()
    shopt -s nullglob
    files=("${dir}"/*.tar.gz "${dir}"/docker-images/*.tar.gz)
    shopt -u nullglob
    printf '%d' "${#files[@]}"
}

release_asset_url() {
    local release_json="$1" asset_name="$2"
    printf '%s' "$release_json" \
        | grep -o "\"browser_download_url\": *\"[^\"]*${asset_name}\"" \
        | grep -o 'https://[^"]*' | head -1 || true
}

release_tag_name() {
    local release_json="$1"
    printf '%s' "$release_json" \
        | grep -o '"tag_name": *"[^"]*"' \
        | sed -E 's/^"tag_name": *"([^"]*)"$/\1/' \
        | head -1 || true
}

require_release_asset_url() {
    local release_json="$1" asset_name="$2"
    local url
    url="$(release_asset_url "$release_json" "$asset_name")"
    [[ -n "$url" ]] || {
        err "В релизе отсутствует обязательный asset: ${asset_name}"
        exit 1
    }
    printf '%s' "$url"
}

download_with_progress() {
    local url="$1" dest="$2" label="$3"
    local progress_log
    progress_log="$(mktemp /tmp/vpn-curl-progress.XXXXXX)"

    if [[ -t 1 && -r /dev/tty && -w /dev/tty ]]; then
        curl -fL --connect-timeout 20 --retry 2 --retry-delay 2 \
            --progress-bar -o "$dest" "$url" > /dev/tty 2>&1
    else
        curl -fL --connect-timeout 20 --retry 2 --retry-delay 2 \
            --progress-bar -o "$dest" "$url" 2>"$progress_log"
        sed -n "s/^#/# ${label}: /p" "$progress_log" || true
    fi

    local rc=$?
    rm -f "$progress_log"
    return "$rc"
}

if [[ $EUID -ne 0 ]]; then
    err "Запустите от root: sudo bash install.sh"
    exit 1
fi

_release_json="$(curl -sf --max-time 20 "$RELEASE_API" 2>/dev/null || true)"
_bootstrap_tag="$(release_tag_name "$_release_json")"
case "$_bootstrap_tag" in
    v*) info "StackInfra bootstrap install ${_bootstrap_tag}" ;;
    *)  info "StackInfra bootstrap install" ;;
esac

# ── 1. Минимальные зависимости ────────────────────────────────────────────────
info "Проверка базовых зависимостей (curl, git)..."
_missing_pkgs=()
for pkg in curl git; do
    if ! command -v "$pkg" &>/dev/null; then
        _missing_pkgs+=("$pkg")
    fi
done

if (( ${#_missing_pkgs[@]} > 0 )); then
    info "Не хватает пакетов: ${_missing_pkgs[*]}"
    apt-get update -qq 2>/dev/null || true
    apt-get install -y -qq "${_missing_pkgs[@]}" 2>/dev/null || true
fi
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

chmod +x "${OPT_VPN}/setup.sh" "${OPT_VPN}/install-home.sh" \
    "${OPT_VPN}/scripts/docker-load-cache.sh" "${OPT_VPN}/dev/save-docker-images.sh" \
    "${OPT_VPN}/scripts/build-system-package-bundles.sh" "${OPT_VPN}/dev/save-system-packages.sh" \
    "${OPT_VPN}/scripts/build-python-wheel-bundles.sh" "${OPT_VPN}/scripts/build-release-assets-manifest.sh" 2>/dev/null || true

_install_version="$(read_install_version "${OPT_VPN}/version" || true)"
if [[ -n "${_install_version}" ]]; then
    info "Устанавливаемая версия: v${_install_version}"
fi

# ── 2.5. Проверка обязательных release assets ────────────────────────────────
if [[ -f "${OPT_VPN}/scripts/release-bundle-assets.sh" ]]; then
    # shellcheck source=scripts/release-bundle-assets.sh
    source "${OPT_VPN}/scripts/release-bundle-assets.sh"
    info "Проверяем обязательные release assets..."
    while IFS= read -r _asset; do
        [[ -n "$_asset" ]] || continue
        require_release_asset_url "$_release_json" "$_asset" >/dev/null
    done < <(release_bundle_asset_names)
    ok "Все обязательные release assets найдены"
fi

# ── 3. Python wheel bundles из GitHub Releases ───────────────────────────────
if [[ -f "${OPT_VPN}/scripts/python-wheel-groups.sh" ]]; then
    # shellcheck source=scripts/python-wheel-groups.sh
    source "${OPT_VPN}/scripts/python-wheel-groups.sh"
    info "Скачиваем python wheel bundles..."
    _wheel_manifest_url="$(require_release_asset_url "$_release_json" "python-wheel-bundles-manifest.txt")"
    download_with_progress "$_wheel_manifest_url" /tmp/python-wheel-bundles-manifest.txt python-wheel-bundles-manifest.txt
    cp /tmp/python-wheel-bundles-manifest.txt "${OPT_VPN}/python-wheel-bundles-manifest.txt"

    while IFS= read -r _group; do
        [[ -n "$_group" ]] || continue
        _asset="$(python_wheel_bundle_asset_name "$_group")"
        _wheel_url="$(require_release_asset_url "$_release_json" "$_asset")"
        case "$_group" in
            installer-gui) _target_dir="${OPT_VPN}/installers/gui/wheels" ;;
            watchdog) _target_dir="${OPT_VPN}/home/watchdog/wheels" ;;
            telegram-bot) _target_dir="${OPT_VPN}/home/telegram-bot/wheels" ;;
            *) err "Неизвестная wheel group: ${_group}"; exit 1 ;;
        esac
        rm -rf "$_target_dir"
        mkdir -p "$_target_dir"
        info "Скачиваем ${_asset}..."
        download_with_progress "$_wheel_url" "/tmp/${_asset}" "${_asset}"
        tar xzf "/tmp/${_asset}" -C "$_target_dir" \
            --no-same-permissions --no-same-owner --overwrite 2>/dev/null || true
        rm -f "/tmp/${_asset}"
        ok "${_asset} скачан и распакован"
    done < <(python_wheel_bundle_groups)
fi

# ── 3.5. System package bundles из GitHub Releases ───────────────────────────
_system_pkg_dir="${OPT_VPN}/system-packages"
if [[ -f "${OPT_VPN}/scripts/system-package-groups.sh" ]]; then
    # shellcheck source=scripts/system-package-groups.sh
    source "${OPT_VPN}/scripts/system-package-groups.sh"
    mapfile -t _system_assets < <(
        while IFS= read -r _group; do
            system_package_bundle_asset_name "$_group"
        done < <(system_package_bundle_groups)
    )
    info "Скачиваем system package bundles..."
    mkdir -p "$_system_pkg_dir"
    _system_manifest_url="$(require_release_asset_url "$_release_json" "system-packages-manifest.txt")"
    download_with_progress "$_system_manifest_url" /tmp/system-packages-manifest.txt system-packages-manifest.txt
    cp /tmp/system-packages-manifest.txt "${OPT_VPN}/system-packages-manifest.txt"
    for _asset in "${_system_assets[@]}"; do
        _pkg_url="$(require_release_asset_url "$_release_json" "$_asset")"
        case "$_asset" in
            system-packages-home-core.tar.gz) _extract_dir="home-core" ;;
            system-packages-home-docker.tar.gz) _extract_dir="home-docker" ;;
            system-packages-home-awg.tar.gz) _extract_dir="home-awg" ;;
            system-packages-vps-core.tar.gz) _extract_dir="vps-core" ;;
            system-packages-vps-docker.tar.gz) _extract_dir="vps-docker" ;;
            *) err "Неизвестный system package asset: ${_asset}"; exit 1 ;;
        esac
        mkdir -p "${_system_pkg_dir}/${_extract_dir}"
        info "Скачиваем ${_asset}..."
        if download_with_progress "$_pkg_url" "/tmp/${_asset}" "${_asset}"; then
            tar xzf "/tmp/${_asset}" -C "${_system_pkg_dir}/${_extract_dir}" \
                --no-same-permissions --no-same-owner --overwrite 2>/dev/null || true
            rm -f "/tmp/${_asset}"
            ok "${_asset} скачан и распакован"
        else
            err "Не удалось скачать ${_asset}"
            exit 1
        fi
    done
fi

# ── 4. Docker-образы из GitHub Releases ───────────────────────────────────────
# Основные архивы домашней установки: docker-images.tar.gz + docker-images-monitoring.tar.gz.
# Без monitoring bundle установка считается неполной и должна завершаться ошибкой.
_img_count="$(count_tar_archives "${DOCKER_IMAGES_DIR}")"
case "$_img_count" in
    ''|*[!0-9]*) _img_count=0 ;;
esac
if [[ "$_img_count" -gt 0 ]]; then
    ok "Docker-образы уже есть: ${_img_count} файлов в ${DOCKER_IMAGES_DIR}"
else
    _bundle_assets=(docker-images.tar.gz docker-images-monitoring.tar.gz docker-images-vps.tar.gz)
    _required_assets=(docker-images.tar.gz docker-images-monitoring.tar.gz)
    if [[ -f "${OPT_VPN}/scripts/docker-image-groups.sh" ]]; then
        # shellcheck source=scripts/docker-image-groups.sh
        source "${OPT_VPN}/scripts/docker-image-groups.sh"
        mapfile -t _bundle_assets < <(
            while IFS= read -r _group; do
                docker_image_bundle_asset_name "$_group"
            done < <(docker_image_bundle_groups)
        )
    fi

    info "Поиск архивов Docker-образов в GitHub Releases..."
    mkdir -p "$DOCKER_IMAGES_DIR"
    _docker_manifest_url="$(require_release_asset_url "$_release_json" "docker-bundles-manifest.txt")"
    download_with_progress "$_docker_manifest_url" /tmp/docker-bundles-manifest.txt docker-bundles-manifest.txt
    cp /tmp/docker-bundles-manifest.txt "${OPT_VPN}/docker-bundles-manifest.txt"
    _downloaded=0
    _missing_required=()

    for _asset in "${_bundle_assets[@]}"; do
        _docker_url="$(release_asset_url "$_release_json" "$_asset")"
        _is_required=0
        for _required in "${_required_assets[@]}"; do
            if [[ "$_asset" == "$_required" ]]; then
                _is_required=1
                break
            fi
        done

        if [[ -z "$_docker_url" ]]; then
            if [[ "$_is_required" -eq 1 ]]; then
                err "${_asset} не найден в релизе"
                _missing_required+=("$_asset")
            else
                warn "${_asset} не найден в релизе"
            fi
            continue
        fi

        info "Скачиваем ${_asset}..."
        if download_with_progress "$_docker_url" "/tmp/${_asset}" "${_asset}"; then
            tar xzf "/tmp/${_asset}" -C "$DOCKER_IMAGES_DIR" \
                --strip-components=1 \
                --no-same-permissions --no-same-owner --overwrite 2>/dev/null || true
            rm -f "/tmp/${_asset}"
            ok "${_asset} скачан и распакован"
            ((_downloaded++)) || true
        else
            if [[ "$_is_required" -eq 1 ]]; then
                err "Не удалось скачать ${_asset}"
                _missing_required+=("$_asset")
            else
                warn "Не удалось скачать ${_asset}"
            fi
        fi
    done

    if (( ${#_missing_required[@]} > 0 )); then
        err "Не удалось подготовить обязательные Docker bundles: ${_missing_required[*]}"
        err "Без них monitoring не будет поднят во время установки."
        exit 1
    fi

    _img_count="$(count_tar_archives "${DOCKER_IMAGES_DIR}")"
    case "$_img_count" in
        ''|*[!0-9]*) _img_count=0 ;;
    esac
    if [[ "$_downloaded" -gt 0 && "$_img_count" -gt 0 ]]; then
        ok "Docker-образы готовы: ${_img_count} файлов"
    else
        err "Локальный Docker image cache не подготовлен"
        err "Сгенерируйте и загрузите архивы: bash dev/save-docker-images.sh"
        exit 1
    fi
fi

# ── 4. Запуск setup.sh ────────────────────────────────────────────────────────
echo ""
info "Запускаем установщик setup.sh..."
echo ""
if [[ -r /dev/tty && -w /dev/tty ]]; then
    exec </dev/tty >/dev/tty 2>/dev/tty env VPN_COMPACT_OUTPUT=1 VPN_INSTALL_VERSION="${_install_version:-}" VPN_STRICT_BUNDLE=1 bash "${OPT_VPN}/setup.sh"
else
    exec env VPN_COMPACT_OUTPUT=1 VPN_INSTALL_VERSION="${_install_version:-}" VPN_STRICT_BUNDLE=1 bash "${OPT_VPN}/setup.sh"
fi

#!/usr/bin/env bash
# dev/save-docker-images.sh — сохранение Docker-образов для офлайн-установки
#
# Запускать на машине с доступом к Docker Hub (VPS вне РФ, или ПК за рубежом).
#
# Что делает:
#   1. Скачивает образы home phase 1, monitoring и VPS core
#   2. Сохраняет в docker-images/*.tar.gz
#   3. Пакует в docker-images*.tar.gz по группам
#   4. Загружает архивы в GitHub Release (gh release upload)
#
# После загрузки install.sh подхватит образы автоматически.
#
# Использование:
#   bash dev/save-docker-images.sh
#   bash dev/save-docker-images.sh --upload-only   # только загрузить уже сохранённые

set -eo pipefail

REPO_OWNER="Cyrillicspb"
REPO_NAME="vpn-infra"
OUTDIR="docker-images"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/docker-image-groups.sh
source "${REPO_DIR}/scripts/docker-image-groups.sh"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }

UPLOAD_ONLY=false
[[ "${1:-}" == "--upload-only" ]] && UPLOAD_ONLY=true
mapfile -t GROUPS < <(docker_image_bundle_groups)

# Проверка зависимостей
if ! command -v docker &>/dev/null; then
    echo "Ошибка: docker не установлен" >&2
    exit 1
fi

if [[ "$UPLOAD_ONLY" == false ]]; then
    # ── Шаг 1: Pull и сохранение образов ─────────────────────────────────────
    mkdir -p "$OUTDIR"

    info "Будут подготовлены группы:"
    printf '  - %s\n' "${GROUPS[@]}"
    echo ""

    mapfile -t ALL_IMAGES < <(docker_image_group_names all)
    total_saved=0
    for img in "${ALL_IMAGES[@]}"; do
        fname=$(docker_image_to_archive_name "$img")
        outpath="${OUTDIR}/${fname}"

        info "Pull: ${img}..."
        docker pull "$img"

        info "Сохраняем → ${outpath}"
        docker save "$img" | gzip > "$outpath"
        size=$(du -sh "$outpath" | cut -f1)
        ok "${fname} (${size})"
        ((total_saved++)) || true
    done

    echo ""
    ok "Сохранено образов: ${total_saved}"
    ls -lh "${OUTDIR}"/*.tar.gz
    echo "Итого: $(du -sh "$OUTDIR" | cut -f1)"

    # ── Шаг 2: Упаковка групповых архивов ─────────────────────────────────────
    for group in "${GROUPS[@]}"; do
        archive=$(docker_image_bundle_asset_name "$group")
        mapfile -t GROUP_IMAGES < <(docker_image_group_names "$group")
        archive_members=()
        for img in "${GROUP_IMAGES[@]}"; do
            archive_members+=("${OUTDIR}/$(docker_image_to_archive_name "$img")")
        done

        info "Пакуем ${group} → ${archive}..."
        tar czf "$archive" "${archive_members[@]}"
        size=$(du -sh "$archive" | cut -f1)
        ok "${archive} готов (${size})"
    done
fi

# ── Шаг 3: Загрузка в GitHub Release ─────────────────────────────────────────
if ! command -v gh &>/dev/null; then
    warn "gh CLI не установлен — загрузка в GitHub Releases пропущена"
    warn "Установите: https://cli.github.com/"
    warn "Затем: gh release upload <TAG> docker-images*.tar.gz"
    echo ""
    echo "Или скопируйте вручную на сервер:"
    echo "  scp docker-images*.tar.gz user@server:/tmp/"
    echo "  ssh user@server 'mkdir -p /opt/vpn/docker-images && for f in /tmp/docker-images*.tar.gz; do tar xzf \"\$f\" -C /opt/vpn/docker-images; done'"
    exit 0
fi

# Получаем тег последнего релиза
LATEST_TAG=$(gh release list --repo "${REPO_OWNER}/${REPO_NAME}" --limit 1 \
    | awk '{print $1}' | head -1)

if [[ -z "$LATEST_TAG" ]]; then
    warn "Нет релизов в репозитории. Создайте релиз сначала."
    exit 1
fi

for group in "${GROUPS[@]}"; do
    archive=$(docker_image_bundle_asset_name "$group")
    if [[ ! -f "$archive" ]]; then
        echo "Ошибка: ${archive} не найден. Запустите без --upload-only." >&2
        exit 1
    fi
    info "Загружаем ${archive} в релиз ${LATEST_TAG}..."
    gh release upload "$LATEST_TAG" "$archive" \
        --repo "${REPO_OWNER}/${REPO_NAME}" \
        --clobber
done

ok "Загружено в https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/tag/${LATEST_TAG}"
echo ""
echo "install.sh автоматически скачает образы при следующей установке."

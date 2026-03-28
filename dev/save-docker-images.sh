#!/usr/bin/env bash
# dev/save-docker-images.sh — сохранение Docker-образов фазы 1
#
# Запускать на машине с доступом к Docker Hub (VPS вне РФ, или ПК за рубежом).
#
# Что делает:
#   1. Скачивает образы фазы 1 (нужны для установки VPN без интернета)
#   2. Сохраняет в docker-images/*.tar.gz
#   3. Пакует в docker-images.tar.gz
#   4. Загружает в GitHub Release (gh release upload)
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
ARCHIVE="docker-images.tar.gz"

# Образы фазы 1 — нужны для поднятия VPN (до доступа к интернету)
PHASE1_IMAGES=(
    "python:3.12-slim"
    "teddysun/xray:latest"
    "tecnativa/docker-socket-proxy:latest"
    "nginx:stable-alpine"
)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }

UPLOAD_ONLY=false
[[ "${1:-}" == "--upload-only" ]] && UPLOAD_ONLY=true

# Проверка зависимостей
if ! command -v docker &>/dev/null; then
    echo "Ошибка: docker не установлен" >&2
    exit 1
fi

if [[ "$UPLOAD_ONLY" == false ]]; then
    # ── Шаг 1: Pull и сохранение образов ─────────────────────────────────────
    mkdir -p "$OUTDIR"

    info "Размер образов фазы 1 (оценка):"
    echo "  python:3.12-slim              ~150 MB"
    echo "  teddysun/xray:latest          ~25 MB"
    echo "  tecnativa/docker-socket-proxy ~15 MB"
    echo "  nginx:stable-alpine           ~45 MB"
    echo "  Итого сжато (gzip):           ~100-120 MB"
    echo ""

    total_saved=0
    for img in "${PHASE1_IMAGES[@]}"; do
        fname=$(echo "$img" | tr '/:' '__').tar.gz
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

    # ── Шаг 2: Упаковка в архив ───────────────────────────────────────────────
    info "Пакуем в ${ARCHIVE}..."
    tar czf "$ARCHIVE" "$OUTDIR"/*.tar.gz
    size=$(du -sh "$ARCHIVE" | cut -f1)
    ok "${ARCHIVE} готов (${size})"
fi

# ── Шаг 3: Загрузка в GitHub Release ─────────────────────────────────────────
if [[ ! -f "$ARCHIVE" ]]; then
    echo "Ошибка: ${ARCHIVE} не найден. Запустите без --upload-only." >&2
    exit 1
fi

if ! command -v gh &>/dev/null; then
    warn "gh CLI не установлен — загрузка в GitHub Releases пропущена"
    warn "Установите: https://cli.github.com/"
    warn "Затем: gh release upload <TAG> ${ARCHIVE}"
    echo ""
    echo "Или скопируйте вручную на сервер:"
    echo "  scp ${ARCHIVE} user@server:/tmp/"
    echo "  ssh user@server 'mkdir -p /opt/vpn/docker-images && tar xzf /tmp/${ARCHIVE} -C /opt/vpn/docker-images'"
    exit 0
fi

# Получаем тег последнего релиза
LATEST_TAG=$(gh release list --repo "${REPO_OWNER}/${REPO_NAME}" --limit 1 \
    | awk '{print $1}' | head -1)

if [[ -z "$LATEST_TAG" ]]; then
    warn "Нет релизов в репозитории. Создайте релиз сначала."
    exit 1
fi

info "Загружаем ${ARCHIVE} в релиз ${LATEST_TAG}..."
gh release upload "$LATEST_TAG" "$ARCHIVE" \
    --repo "${REPO_OWNER}/${REPO_NAME}" \
    --clobber

ok "Загружено в https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/tag/${LATEST_TAG}"
echo ""
echo "install.sh автоматически скачает образы при следующей установке."

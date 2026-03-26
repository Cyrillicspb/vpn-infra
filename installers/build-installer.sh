#!/bin/bash
# build-installer.sh — сборка самораспаковывающегося install.sh
#
# Использование:
#   bash installers/build-installer.sh [--no-wheels] [--output path/install.sh]
#
# Результат: install.sh (~5-8 MB с wheels, ~500 KB без)
# Запуск на сервере: sudo bash install.sh

set -euo pipefail
cd "$(dirname "$0")/.."

OUTPUT="install.sh"
WITH_WHEELS=1

for arg in "$@"; do
    case "$arg" in
        --no-wheels)  WITH_WHEELS=0 ;;
        --output=*)   OUTPUT="${arg#--output=}" ;;
        --output)     shift; OUTPUT="$1" ;;
    esac
done

TMPDIR_BUILD=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BUILD"' EXIT

echo "→ Подготовка файлов..."

PAYLOAD_DIR="$TMPDIR_BUILD/payload"
mkdir -p "$PAYLOAD_DIR"

# Копируем нужные файлы (без .git, venv, логов, секретов)
rsync -a --exclude='.git' --exclude='*.pyc' --exclude='__pycache__' \
    --exclude='*/venv/*' --exclude='*.log' --exclude='.env' \
    --exclude='node_modules' --exclude='.setup-state' \
    --exclude='installers/windows' --exclude='installers/macos' \
    --exclude='installers/linux' \
    --exclude='docs' --exclude='tests' --exclude='.github' \
    --exclude='*.tar.gz' --exclude='install.sh' \
    --exclude='overnight-*' --exclude='run-overnight*' \
    . "$PAYLOAD_DIR/" 2>/dev/null || {
    # rsync может отсутствовать — используем cp + find
    cp -r setup.sh common.sh install-home.sh install-vps.sh \
        deploy.sh restore.sh version \
        home vps installers/gui installers/bootstrap.sh \
        "$PAYLOAD_DIR/" 2>/dev/null || true
    mkdir -p "$PAYLOAD_DIR/installers"
    cp -r installers/gui "$PAYLOAD_DIR/installers/" 2>/dev/null || true
    cp installers/bootstrap.sh "$PAYLOAD_DIR/installers/" 2>/dev/null || true
}

# ── Скачивание wheels (для офлайн-установки) ──────────────────────────────────
if [[ $WITH_WHEELS -eq 1 ]]; then
    echo "→ Загрузка wheel-пакетов textual..."
    WHEELS_DIR="$PAYLOAD_DIR/wheels"
    mkdir -p "$WHEELS_DIR"
    PIP_ARGS=(--dest "$WHEELS_DIR" --python-version 3.10
              --only-binary=:all: --platform manylinux2014_x86_64 --quiet)
    PACKAGES=(
        'textual>=0.47.0'
        'aiohttp==3.9.5' 'fastapi==0.111.0' 'uvicorn[standard]==0.29.0'
        'pydantic==2.7.1' 'slowapi==0.1.9' 'psutil==5.9.8'
        'PyYAML==6.0.1' 'aiofiles==23.2.1' 'aggregate6'
    )
    FAIL=0
    for pkg in "${PACKAGES[@]}"; do
        python3 -m pip download "$pkg" "${PIP_ARGS[@]}" 2>/dev/null || FAIL=1
    done
    WHEEL_COUNT=$(ls "$WHEELS_DIR"/*.whl 2>/dev/null | wc -l || echo 0)
    if [[ $WHEEL_COUNT -gt 0 ]]; then
        echo "  ✓ Загружено $WHEEL_COUNT wheel-пакетов"
        [[ $FAIL -eq 1 ]] && echo "  ⚠ Некоторые пакеты не скачались — будет онлайн-установка"
    else
        echo "  ⚠ Не удалось загрузить wheels — установка потребует интернет"
        rm -rf "$WHEELS_DIR"
    fi
fi

# ── Упаковка payload ──────────────────────────────────────────────────────────
echo "→ Упаковка..."
TARBALL="$TMPDIR_BUILD/payload.tar.gz"
tar -czf "$TARBALL" -C "$TMPDIR_BUILD" payload

PAYLOAD_B64="$TMPDIR_BUILD/payload.b64"
base64 "$TARBALL" > "$PAYLOAD_B64"

# ── Сборка install.sh ─────────────────────────────────────────────────────────
echo "→ Сборка $OUTPUT..."
cat > "$OUTPUT" << 'HEADER'
#!/bin/bash
# VPN Infrastructure — Self-Extracting Installer
# Использование: sudo bash install.sh
#
# Содержит весь репозиторий + Python wheels (offline).
# Запуск TUI: автоматически после распаковки.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите с правами root: sudo bash install.sh" >&2
    exit 1
fi

TMPDIR_INST=$(mktemp -d /tmp/vpn-install-XXXXXX)
trap 'rm -rf "$TMPDIR_INST"' EXIT

echo "→ Распаковка..."
PAYLOAD_LINE=$(grep -n '^__PAYLOAD__$' "$0" | cut -d: -f1)
tail -n +"$((PAYLOAD_LINE + 1))" "$0" | base64 -d | tar -xz -C "$TMPDIR_INST"

exec bash "$TMPDIR_INST/payload/installers/bootstrap.sh" "$TMPDIR_INST/payload"
exit $?

__PAYLOAD__
HEADER

cat "$PAYLOAD_B64" >> "$OUTPUT"
chmod +x "$OUTPUT"

SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo ""
echo "✓ Готово: $OUTPUT ($SIZE)"
echo ""
echo "  Запуск на сервере:"
echo "    sudo bash install.sh"

#!/usr/bin/env bash
# dev/save-system-packages.sh — подготовка и загрузка .deb-бандлов для офлайн-установки.

set -euo pipefail

REPO_OWNER="Cyrillicspb"
REPO_NAME="vpn-infra"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/system-package-groups.sh
source "${REPO_DIR}/scripts/system-package-groups.sh"

UPLOAD_ONLY=false
[[ "${1:-}" == "--upload-only" ]] && UPLOAD_ONLY=true

if [[ "$UPLOAD_ONLY" == false ]]; then
    bash "${REPO_DIR}/scripts/build-system-package-bundles.sh" \
        --output-dir "${REPO_DIR}" \
        --work-dir "${REPO_DIR}/system-packages-build" \
        --manifest-out "${REPO_DIR}/system-packages-manifest.txt"
fi

if ! command -v gh &>/dev/null; then
    echo "gh CLI не установлен" >&2
    exit 1
fi

LATEST_TAG=$(gh release list --repo "${REPO_OWNER}/${REPO_NAME}" --limit 1 | awk '{print $1}' | head -1)
[[ -n "$LATEST_TAG" ]] || { echo "Нет релизов" >&2; exit 1; }

while IFS= read -r group; do
    asset="$(system_package_bundle_asset_name "$group")"
    [[ -f "${REPO_DIR}/${asset}" ]] || { echo "Не найден ${asset}" >&2; exit 1; }
    gh release upload "$LATEST_TAG" "${REPO_DIR}/${asset}" \
        --repo "${REPO_OWNER}/${REPO_NAME}" \
        --clobber
done < <(system_package_bundle_groups)

gh release upload "$LATEST_TAG" "${REPO_DIR}/system-packages-manifest.txt" \
    --repo "${REPO_OWNER}/${REPO_NAME}" \
    --clobber

if [[ -f "${REPO_DIR}/release-assets-manifest.txt" ]]; then
    gh release upload "$LATEST_TAG" "${REPO_DIR}/release-assets-manifest.txt" \
        --repo "${REPO_OWNER}/${REPO_NAME}" \
        --clobber
fi

echo "Uploaded to ${LATEST_TAG}"

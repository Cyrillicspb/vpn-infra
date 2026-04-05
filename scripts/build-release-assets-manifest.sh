#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release-bundle-assets.sh
source "${SCRIPT_DIR}/release-bundle-assets.sh"

OUT="${1:-release-assets-manifest.txt}"

{
    echo "schema=1"
    echo "generated_at=$(date -u +%FT%TZ)"
    echo "release_tag=${RELEASE_TAG:-unknown}"
    echo "commit_sha=${RELEASE_COMMIT_SHA:-unknown}"
    echo "builder_digest=$(sha256sum "$0" "${SCRIPT_DIR}/release-bundle-assets.sh" | sha256sum | awk '{print $1}')"
    while IFS= read -r asset; do
        [[ -n "$asset" ]] || continue
        echo "asset|required|${asset}"
    done < <(release_bundle_asset_names)
} > "$OUT"

echo "Manifest: $OUT"

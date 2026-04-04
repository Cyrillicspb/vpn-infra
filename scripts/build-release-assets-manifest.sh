#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release-bundle-assets.sh
source "${SCRIPT_DIR}/release-bundle-assets.sh"

OUT="${1:-release-assets-manifest.txt}"

{
    echo "schema=1"
    echo "generated_at=$(date -u +%FT%TZ)"
    while IFS= read -r asset; do
        [[ -n "$asset" ]] || continue
        echo "asset|required|${asset}"
    done < <(release_bundle_asset_names)
} > "$OUT"

echo "Manifest: $OUT"

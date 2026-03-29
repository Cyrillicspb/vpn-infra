#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/docker-image-groups.sh
source "${SCRIPT_DIR}/docker-image-groups.sh"

cd "$REPO_DIR"

MANIFEST_OUT="${MANIFEST_OUT:-docker-bundles-manifest.txt}"
OUTPUT_DIR="${OUTPUT_DIR:-.}"
WORK_DIR="${WORK_DIR:-docker-images}"
PULL_IMAGES=1
MANIFEST_ONLY=0
BUILD_GROUPS_CSV=""

usage() {
    cat <<'EOF'
Usage:
  build-docker-bundles.sh [options]

Options:
  --manifest-out PATH      Path to bundle manifest file
  --output-dir DIR         Directory for bundle archives
  --work-dir DIR           Directory for per-image archives
  --build-groups CSV       Comma-separated group list to build
  --manifest-only          Only resolve images and write manifest
  --no-pull                Reuse already pulled local images
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manifest-out) MANIFEST_OUT="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir) WORK_DIR="$2"; shift 2 ;;
        --build-groups) BUILD_GROUPS_CSV="$2"; shift 2 ;;
        --manifest-only) MANIFEST_ONLY=1; shift ;;
        --no-pull) PULL_IMAGES=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

mkdir -p "$OUTPUT_DIR" "$WORK_DIR"

if (( PULL_IMAGES == 1 )); then
    mapfile -t ALL_IMAGES < <(docker_image_group_names all)
    for img in "${ALL_IMAGES[@]}"; do
        echo "Pull: $img"
        docker pull "$img"
    done
fi

tmp_manifest="$(mktemp)"
{
    echo "schema=1"
    echo "generated_at=$(date -u +%FT%TZ)"
} > "$tmp_manifest"

while IFS= read -r group; do
    asset="$(docker_image_bundle_asset_name "$group")"
    group_lines=()
    while IFS= read -r img; do
        archive_name="$(docker_image_to_archive_name "$img")"
        repo_digest="$(docker image inspect --format '{{join .RepoDigests "\n"}}' "$img" | head -1 || true)"
        image_id="$(docker image inspect --format '{{.Id}}' "$img")"
        group_lines+=("image|${group}|${img}|${repo_digest}|${image_id}|${archive_name}")
    done < <(docker_image_group_names "$group")

    printf '%s\n' "${group_lines[@]}" >> "$tmp_manifest"
    group_digest="$(printf '%s\n' "${group_lines[@]}" | sha256sum | awk '{print $1}')"
    echo "group|${group}|${asset}|${group_digest}" >> "$tmp_manifest"
done < <(docker_image_bundle_groups)

mv "$tmp_manifest" "$MANIFEST_OUT"
echo "Manifest: $MANIFEST_OUT"

if (( MANIFEST_ONLY == 1 )); then
    exit 0
fi

if [[ -z "$BUILD_GROUPS_CSV" ]]; then
    mapfile -t TARGET_GROUPS < <(docker_image_bundle_groups)
else
    IFS=',' read -r -a TARGET_GROUPS <<< "$BUILD_GROUPS_CSV"
fi

for group in "${TARGET_GROUPS[@]}"; do
    [[ -n "$group" ]] || continue
    asset="$(docker_image_bundle_asset_name "$group")"
    members=()
    while IFS= read -r img; do
        fname="$(docker_image_to_archive_name "$img")"
        archive_path="${WORK_DIR}/${fname}"
        if [[ ! -f "$archive_path" ]]; then
            echo "Save: ${archive_path}"
            docker save "$img" | gzip > "$archive_path"
        fi
        members+=("${WORK_DIR}/${fname}")
    done < <(docker_image_group_names "$group")

    echo "Bundle: ${asset}"
    tar -czf "${OUTPUT_DIR}/${asset}" "${members[@]}"
    ls -lh "${OUTPUT_DIR}/${asset}"
done

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/python-wheel-groups.sh
source "${SCRIPT_DIR}/python-wheel-groups.sh"

PYTHON_WHEEL_BUNDLE_SCHEMA=1
PYTHON_WHEEL_BUNDLE_BUILDER_DIGEST="$(
    cat "${SCRIPT_DIR}/build-python-wheel-bundles.sh" "${SCRIPT_DIR}/python-wheel-groups.sh" \
        | sha256sum | awk '{print $1}'
)"

OUTPUT_DIR="${OUTPUT_DIR:-.}"
WORK_DIR="${WORK_DIR:-python-wheel-build}"
MANIFEST_OUT="${MANIFEST_OUT:-python-wheel-bundles-manifest.txt}"
BUILD_GROUPS_CSV=""
MANIFEST_ONLY=0

usage() {
    cat <<'EOF'
Usage:
  build-python-wheel-bundles.sh [options]

Options:
  --output-dir DIR         Directory for bundle archives
  --work-dir DIR           Working directory for wheel download
  --manifest-out PATH      Path to manifest file
  --build-groups CSV       Comma-separated group list to build
  --manifest-only          Only write manifest, do not build archives
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir) WORK_DIR="$2"; shift 2 ;;
        --manifest-out) MANIFEST_OUT="$2"; shift 2 ;;
        --build-groups) BUILD_GROUPS_CSV="$2"; shift 2 ;;
        --manifest-only) MANIFEST_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

mkdir -p "$OUTPUT_DIR" "$WORK_DIR"
cd "$REPO_DIR"

if [[ -z "$BUILD_GROUPS_CSV" ]]; then
    mapfile -t TARGET_GROUPS < <(python_wheel_bundle_groups)
else
    IFS=',' read -r -a TARGET_GROUPS <<< "$BUILD_GROUPS_CSV"
fi

tmp_manifest="$(mktemp)"
{
    echo "schema=${PYTHON_WHEEL_BUNDLE_SCHEMA}"
    echo "generated_at=$(date -u +%FT%TZ)"
    echo "builder_digest=${PYTHON_WHEEL_BUNDLE_BUILDER_DIGEST}"
} > "$tmp_manifest"

for group in "${TARGET_GROUPS[@]}"; do
    [[ -n "$group" ]] || continue
    asset="$(python_wheel_bundle_asset_name "$group")"
    req_file="$(python_wheel_group_requirements_file "$group")"
    mapfile -t reqs < <(grep -Ev '^\s*(#|$)' "$req_file" || true)
    mapfile -t extras < <(python_wheel_group_extra_packages "$group" || true)

    group_lines=()
    for pkg in "${extras[@]}"; do
        [[ -n "$pkg" ]] || continue
        group_lines+=("pkg|${group}|extra|${pkg}")
    done
    for pkg in "${reqs[@]}"; do
        [[ -n "$pkg" ]] || continue
        group_lines+=("pkg|${group}|req|${pkg}")
    done
    group_lines+=("builder|${group}|${PYTHON_WHEEL_BUNDLE_SCHEMA}|${PYTHON_WHEEL_BUNDLE_BUILDER_DIGEST}")
    printf '%s\n' "${group_lines[@]}" >> "$tmp_manifest"
    group_digest="$(printf '%s\n' "${group_lines[@]}" | sha256sum | awk '{print $1}')"
    echo "group|${group}|${asset}|${group_digest}" >> "$tmp_manifest"

    if (( MANIFEST_ONLY == 1 )); then
        continue
    fi

    group_dir="${WORK_DIR}/${group}"
    rm -rf "$group_dir"
    mkdir -p "$group_dir"

    download_args=(
        --dest "$group_dir"
        --python-version 3.12
        --only-binary=:all:
        --platform manylinux2014_x86_64
        --quiet
    )

    if [[ ${#extras[@]} -gt 0 ]]; then
        python3 -m pip download "${extras[@]}" "${download_args[@]}"
    fi
    python3 -m pip download -r "$req_file" "${download_args[@]}"

    mapfile -t wheels < <(find "$group_dir" -maxdepth 1 -type f -name '*.whl' | sort)
    [[ ${#wheels[@]} -gt 0 ]] || { echo "No wheels downloaded for group ${group}" >&2; exit 1; }
    for whl in "${wheels[@]}"; do
        echo "wheel|${group}|$(basename "$whl")|$(sha256sum "$whl" | awk '{print $1}')" >> "$tmp_manifest"
    done

    tar -czf "${OUTPUT_DIR}/${asset}" -C "$group_dir" .
done

mv "$tmp_manifest" "$MANIFEST_OUT"
echo "Manifest: $MANIFEST_OUT"

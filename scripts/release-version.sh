#!/usr/bin/env bash
set -euo pipefail

normalize_release_version() {
    local raw="${1:-}"
    raw="${raw#v}"
    case "$raw" in
        ''|*[!0-9.]*)
            return 1
            ;;
        *)
            printf '%s\n' "$raw"
            ;;
    esac
}

release_tag_for_ref() {
    local ref="${1:-HEAD}"
    git tag --points-at "$ref" --list 'v*' --sort=-v:refname | head -1 || true
}

release_version_from_tag() {
    local tag="${1:-}"
    normalize_release_version "$tag"
}

release_version_from_file() {
    local file="${1:-version}"
    [[ -f "$file" ]] || return 1
    local raw
    raw="$(tr -d '[:space:]' < "$file" 2>/dev/null || true)"
    normalize_release_version "$raw"
}

previous_release_tag_for_ref() {
    local ref="${1:-HEAD}"
    local exact_tag
    exact_tag="$(release_tag_for_ref "$ref")"
    while IFS= read -r tag; do
        [[ -n "$tag" ]] || continue
        [[ "$tag" == "$exact_tag" ]] && continue
        printf '%s\n' "$tag"
        return 0
    done < <(git tag --merged "$ref" --list 'v*' --sort=-v:refname)
    return 1
}

next_release_version() {
    local base="${1:-}"
    base="$(normalize_release_version "$base")"
    local prefix patch next_patch
    prefix="$(echo "$base" | rev | cut -d. -f2- | rev)"
    patch="$(echo "$base" | rev | cut -d. -f1 | rev)"
    next_patch=$((patch + 1))
    if [[ -n "$prefix" ]]; then
        printf '%s.%s\n' "$prefix" "$next_patch"
    else
        printf '%s\n' "$next_patch"
    fi
}

release_version_for_ref() {
    local ref="${1:-HEAD}"
    local tag
    tag="$(git tag --points-at "$ref" --list 'v*' --sort=-v:refname | head -1 || true)"
    if [[ -n "$tag" ]]; then
        release_version_from_tag "$tag"
        return 0
    fi
    tag="$(git tag --list 'v*' --sort=-v:refname | head -1 || true)"
    if [[ -n "$tag" ]]; then
        release_version_from_tag "$tag"
        return 0
    fi
    return 1
}

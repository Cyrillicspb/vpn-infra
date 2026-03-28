#!/usr/bin/env bash
# docker-load-cache.sh — загрузка локального кэша Docker-образов.

set -euo pipefail

DIR="/opt/vpn/docker-images"
PATTERN="*.tar.gz"
LABEL="Docker image cache"
ALLOW_EMPTY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            DIR="$2"
            shift 2
            ;;
        --pattern)
            PATTERN="$2"
            shift 2
            ;;
        --label)
            LABEL="$2"
            shift 2
            ;;
        --allow-empty)
            ALLOW_EMPTY=1
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

mapfile -t FILES < <(find "$DIR" -maxdepth 1 -type f -name "$PATTERN" | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
    [[ "$ALLOW_EMPTY" -eq 1 ]] && exit 0
    echo "No cached Docker images found in ${DIR} (${PATTERN})" >&2
    exit 1
fi

echo "[i] ${LABEL}: ${#FILES[@]} archive(s)"
for archive in "${FILES[@]}"; do
    echo "[i]   docker load: $(basename "$archive")"
    case "$archive" in
        *.tar.gz)
            gunzip -c "$archive" | docker load >/dev/null
            ;;
        *.tar)
            docker load -i "$archive" >/dev/null
            ;;
        *)
            echo "[!] Unsupported archive format: $archive" >&2
            ;;
    esac
done

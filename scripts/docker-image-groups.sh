#!/usr/bin/env bash
# docker-image-groups.sh — единый список Docker-образов для офлайн-кэша.

docker_image_group_names() {
    local group="${1:-}"

    case "$group" in
        home-phase1)
            cat <<'EOF'
python:3.12-slim
teddysun/xray:latest
tecnativa/docker-socket-proxy:latest
nginx:stable-alpine
EOF
            ;;
        home-monitoring)
            cat <<'EOF'
prom/prometheus:latest
prom/alertmanager:latest
grafana/grafana:latest
grafana/grafana-image-renderer:latest
prom/node-exporter:latest
EOF
            ;;
        vps-core)
            cat <<'EOF'
ghcr.io/mhsanaei/3x-ui:latest
teddysun/xray:latest
nginx:1.25.4-alpine
cloudflare/cloudflared:2024.5.0
prom/node-exporter:v1.7.0
networkstatic/iperf3:latest
tobyxdd/hysteria:v2.6.1
EOF
            ;;
        all)
            {
                docker_image_group_names home-phase1
                docker_image_group_names home-monitoring
                docker_image_group_names vps-core
            } | awk '!seen[$0]++'
            ;;
        *)
            echo "Unknown docker image group: ${group}" >&2
            return 1
            ;;
    esac
}

docker_image_bundle_asset_name() {
    local group="${1:-}"

    case "$group" in
        home-phase1) echo "docker-images.tar.gz" ;;
        home-monitoring) echo "docker-images-monitoring.tar.gz" ;;
        vps-core) echo "docker-images-vps.tar.gz" ;;
        *)
            echo "Unknown docker image bundle group: ${group}" >&2
            return 1
            ;;
    esac
}

docker_image_bundle_groups() {
    cat <<'EOF'
home-phase1
home-monitoring
vps-core
EOF
}

docker_image_to_archive_name() {
    local image="${1:-}"
    printf '%s.tar.gz\n' "${image//[:\/]/__}"
}

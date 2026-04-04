#!/usr/bin/env bash
# release-bundle-assets.sh — единый список обязательных release bundle assets.

_release_assets_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/system-package-groups.sh
source "${_release_assets_script_dir}/system-package-groups.sh"
# shellcheck source=scripts/docker-image-groups.sh
source "${_release_assets_script_dir}/docker-image-groups.sh"
# shellcheck source=scripts/python-wheel-groups.sh
source "${_release_assets_script_dir}/python-wheel-groups.sh"

release_bundle_manifest_assets() {
    cat <<'EOF'
release-assets-manifest.txt
docker-bundles-manifest.txt
system-packages-manifest.txt
python-wheel-bundles-manifest.txt
EOF
}

release_bundle_asset_names() {
    echo "vpn-infra.tar.gz"
    release_bundle_manifest_assets
    while IFS= read -r group; do
        python_wheel_bundle_asset_name "$group"
    done < <(python_wheel_bundle_groups)
    while IFS= read -r group; do
        docker_image_bundle_asset_name "$group"
    done < <(docker_image_bundle_groups)
    while IFS= read -r group; do
        system_package_bundle_asset_name "$group"
    done < <(system_package_bundle_groups)
}

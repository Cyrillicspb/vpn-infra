#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/system-package-groups.sh
source "${SCRIPT_DIR}/system-package-groups.sh"

SYSTEM_PACKAGE_BUNDLE_SCHEMA=2
SYSTEM_PACKAGE_BUNDLE_BUILDER_DIGEST="$(
    cat "${SCRIPT_DIR}/build-system-package-bundles.sh" "${SCRIPT_DIR}/system-package-groups.sh" \
        | sha256sum | awk '{print $1}'
)"

OUTPUT_DIR="${OUTPUT_DIR:-.}"
WORK_DIR="${WORK_DIR:-system-packages-build}"
MANIFEST_OUT="${MANIFEST_OUT:-system-packages-manifest.txt}"
BUILD_GROUPS_CSV=""
MANIFEST_ONLY=0

usage() {
    cat <<'EOF'
Usage:
  build-system-package-bundles.sh [options]

Options:
  --output-dir DIR         Directory for bundle archives
  --work-dir DIR           Working directory for per-group .deb files
  --manifest-out PATH      Path to manifest file
  --build-groups CSV       Comma-separated group list to build
  --manifest-only          Only write manifest, do not build archives
EOF
}

prepare_apt_repo_config() {
    local group="$1"
    local repo_kind="$2"
    local config_dir="$3"

    mkdir -p "${config_dir}/etc/apt" \
             "${config_dir}/etc/apt/sources.list.d" \
             "${config_dir}/etc/apt/keyrings" \
             "${config_dir}/usr/share/keyrings"

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg

if [[ -f /etc/apt/sources.list ]]; then
  cp /etc/apt/sources.list '/repo/${config_dir}/etc/apt/sources.list'
fi
if [[ -d /etc/apt/sources.list.d ]]; then
  cp -a /etc/apt/sources.list.d/. '/repo/${config_dir}/etc/apt/sources.list.d/'
fi

case '${repo_kind}' in
  ubuntu)
    ;;
  docker)
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o '/repo/${config_dir}/etc/apt/keyrings/docker.gpg'
    chmod a+r '/repo/${config_dir}/etc/apt/keyrings/docker.gpg'
    . /etc/os-release
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \${VERSION_CODENAME} stable\" > '/repo/${config_dir}/etc/apt/sources.list.d/docker.list'
    ;;
  amnezia)
    cat > /tmp/amnezia.asc <<'EOF_KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGV0UhsBEAC33rMndHSN/k+u7gcZbh9/FjgYfGltQAtVe2QDxzn7UV+k/ChX
OrYRw6Izw/DrhaapkNCThK2jwJE64e0NjboLH7UrrmSJLXMfOlDFbyGJVRA+1sTB
lo7kKHY0xiZ1CHDzjKNV3czbesu80A9nuTZYyWHEn9ax6wsqKG3N8SvzQkUrIOVD
2wZjh0p273CCEGkBnax1ghAV3MF8OrsPU6FRJ+ZakzKbu54g68xoV+2813YECme0
JKsWfUUe/1uEJOXCvuACURSxnYr0sihJd8QI/jHSGlfeq72e5MflFEOrnu5xaDSJ
r2W5lvUetG7EGSxtNKd7Jm/KhUV04g7arA0qydRjRToW3QqyzG7VB2nXKz3AOBYN
earWAuBcTkfPvRVchxbjiYonKZA5tIlVrpawMZsdxKvYwl6LVnpBcccFWPhpudfy
4TpCqCxRoAanOCvSirI3/y7TcZMBw643SaxXi1ifGeg6eyMzrLtP3CeonKBHGzrt
1eeKGtEw/PFN4RmwpBePxi+uj0CoTD6zjCQa3c8EeB4Qz7tt6PnpibxdtZE8sBdd
51wSA/fPGi2tFph8IVAsws7oxcQxZYl8CyncKDLcoR4dxVHYdFEDDf1GjRjoQ3Ai
nD7fxD5qYzExe50DBVpuUbWcAiGICNxfvzQtUSRRtMoSHDcvzsy03KC6VwARAQAB
tB5MYXVuY2hwYWQgUFBBIGZvciBJdXJpaSBFZ29yb3aJAk4EEwEKADgWIQR1yd1y
x5mHDjEFQuJBZvLCVykIKAUCZXRSGwIbAwULCQgHAgYVCgkICwIEFgIDAQIeAQIX
gAAKCRBBZvLCVykIKBu5D/9akmHCHlUqm2RTTBeTMbLNGc0l6YugpPaCM6vz0O9k
BFP5PfRaNSRzyF7wHFHNY3JUHcor28my1fD8AE4+C3PwXz8tVYLh57UUsp4wjqHY
+MTl/1ngDViPGD3PRjB8ZlO+19yerfplZv1Jaw7FZZv2BZOAXb+ddqUG4EmlzOnC
EhcSDdFrzEBB3RGthjIb3QkKWKGbELDiMfogmsO9BE139Raiw23blagDrbnWsG4j
ReZeu3atjG6AW8eL7m+i7bKKshD2CYVMznI5cYGLMKo9w7sb33uylPj1Vx9O7joP
2GFf2rTpCY8wgzk7i1RqsipJ80u1/DY91Xdizv3f2BBe6UY7qHKoK00O11J0y8yU
is2Asycy33Wy51pf6rCFUBLQu+c1fEypHF6jqANmQwaH7pPBliy4gGWvrVggzV4m
xv7SnRiMi4PFyVwjKWm8dmuMxi/B9s++VG/ed+5aYgJYL58MohG3MUI/L58eitSC
DDcQ1iAnBmawnGMKPqzMgRFB3OU3wDwfh7LNVvQqWpQ4q7pr4Cq1CvZvGoggXWDo
1/vylPsRmiiuNetfsoVYmrkgtj1om07m5Xp1v4SyXJH11c3dc/xfMmn/4RlMWIpq
86IsOjpr3avsw3FVUNCgD5Wf5+rHG+7gNmM6Cm/F8MDfAnnRmsw4h6hgvcJNQT5D
ig==
=MT+d
-----END PGP PUBLIC KEY BLOCK-----
EOF_KEY
    gpg --dearmor -o '/repo/${config_dir}/usr/share/keyrings/amnezia-ppa.gpg' /tmp/amnezia.asc
    . /etc/os-release
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/amnezia-ppa.gpg] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu \${VERSION_CODENAME} main\" > '/repo/${config_dir}/etc/apt/sources.list.d/amnezia.list'
    ;;
esac
"
}

download_group_packages() {
    local group="$1"
    local config_dir="$2"
    local group_dir="$3"
    local pkgs="$4"

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
apt-get update -qq
apt-get install -y -qq ca-certificates

rm -rf /etc/apt/sources.list.d/*
rm -rf /etc/apt/keyrings/*
rm -f /etc/apt/sources.list
mkdir -p /etc/apt/sources.list.d /etc/apt/keyrings /usr/share/keyrings

if [[ -f '/repo/${config_dir}/etc/apt/sources.list' ]]; then
  cp '/repo/${config_dir}/etc/apt/sources.list' /etc/apt/sources.list
fi
if [[ -d '/repo/${config_dir}/etc/apt/sources.list.d' ]]; then
  cp -a '/repo/${config_dir}/etc/apt/sources.list.d/.' /etc/apt/sources.list.d/
fi
if [[ -d '/repo/${config_dir}/etc/apt/keyrings' ]]; then
  cp -a '/repo/${config_dir}/etc/apt/keyrings/.' /etc/apt/keyrings/
fi
if [[ -d '/repo/${config_dir}/usr/share/keyrings' ]]; then
  cp -a '/repo/${config_dir}/usr/share/keyrings/.' /usr/share/keyrings/
fi

apt-get update -qq
rm -f /var/cache/apt/archives/*.deb
apt-get install --download-only -y -qq --no-install-recommends ${pkgs}
cp /var/cache/apt/archives/*.deb '/repo/${group_dir}/'
"
}

validate_group_bundle() {
    local group="$1"
    local group_dir="$2"

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
tmp_apt=\$(mktemp -d /tmp/vpn-bundle-apt.XXXXXX)
mkdir -p \"\$tmp_apt/archives/partial\"
cp -f /repo/${group_dir}/*.deb \"\$tmp_apt/archives/\"
mapfile -t bundle_pkgs < '/repo/${group_dir}/group-packages.txt'
dpkg -i \"\$tmp_apt\"/archives/*.deb >/dev/null 2>&1 || true
env DEBIAN_FRONTEND=noninteractive \
  apt-get -o Dpkg::Use-Pty=0 -o APT::Color=0 -o Dpkg::Progress-Fancy=0 \
  -o Dir::Cache::archives=\"\$tmp_apt/archives\" \
  install -y --no-download --no-install-recommends \"\${bundle_pkgs[@]}\"
rm -rf \"\$tmp_apt\"
"
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

if [[ -z "$BUILD_GROUPS_CSV" ]]; then
    mapfile -t TARGET_GROUPS < <(system_package_bundle_groups)
else
    IFS=',' read -r -a TARGET_GROUPS <<< "$BUILD_GROUPS_CSV"
fi

tmp_manifest="$(mktemp)"
{
    echo "schema=${SYSTEM_PACKAGE_BUNDLE_SCHEMA}"
    echo "generated_at=$(date -u +%FT%TZ)"
    echo "builder_digest=${SYSTEM_PACKAGE_BUNDLE_BUILDER_DIGEST}"
} > "$tmp_manifest"

for group in "${TARGET_GROUPS[@]}"; do
    [[ -n "$group" ]] || continue
    repo_kind="$(system_package_group_repo_kind "$group")"
    asset="$(system_package_bundle_asset_name "$group")"
    mapfile -t group_pkgs < <(system_package_group_names "$group")

    group_lines=()
    for pkg in "${group_pkgs[@]}"; do
        [[ -n "$pkg" ]] || continue
        group_lines+=("pkg|${group}|${repo_kind}|${pkg}")
    done
    group_lines+=("builder|${group}|${SYSTEM_PACKAGE_BUNDLE_SCHEMA}|${SYSTEM_PACKAGE_BUNDLE_BUILDER_DIGEST}")
    printf '%s\n' "${group_lines[@]}" >> "$tmp_manifest"
    group_digest="$(printf '%s\n' "${group_lines[@]}" | sha256sum | awk '{print $1}')"
    echo "group|${group}|${asset}|${group_digest}" >> "$tmp_manifest"

    if (( MANIFEST_ONLY == 1 )); then
        continue
    fi

    group_dir="${WORK_DIR}/${group}"
    config_dir="${WORK_DIR}/${group}-apt-config"
    rm -rf "$group_dir"
    rm -rf "$config_dir"
    mkdir -p "$group_dir"
    printf '%s\n' "${group_pkgs[@]}" > "${group_dir}/group-packages.txt"
    pkgs="$(printf '%s\n' "${group_pkgs[@]}" | tr '\n' ' ')"

    prepare_apt_repo_config "$group" "$repo_kind" "$config_dir"
    download_group_packages "$group" "$config_dir" "$group_dir" "$pkgs"

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
apt-get update -qq
apt-get install -y -qq dpkg-dev
cd '/repo/${group_dir}'
dpkg-scanpackages . /dev/null > Packages
gzip -9c Packages > Packages.gz
"

    mapfile -t debs < <(find "$group_dir" -maxdepth 1 -type f -name '*.deb' | sort)
    if [[ ${#debs[@]} -eq 0 ]]; then
        echo "No .deb files downloaded for group ${group}" >&2
        exit 1
    fi

    {
        for deb in "${debs[@]}"; do
            echo "deb|${group}|$(basename "$deb")|$(sha256sum "$deb" | awk '{print $1}')"
        done
    } >> "$tmp_manifest"
    tar -czf "${OUTPUT_DIR}/${asset}" -C "$group_dir" .

    validate_group_bundle "$group" "$group_dir"
done

mv "$tmp_manifest" "$MANIFEST_OUT"
echo "Manifest: $MANIFEST_OUT"

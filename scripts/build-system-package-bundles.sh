#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/system-package-groups.sh
source "${SCRIPT_DIR}/system-package-groups.sh"

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
    echo "schema=1"
    echo "generated_at=$(date -u +%FT%TZ)"
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
    printf '%s\n' "${group_lines[@]}" >> "$tmp_manifest"
    group_digest="$(printf '%s\n' "${group_lines[@]}" | sha256sum | awk '{print $1}')"
    echo "group|${group}|${asset}|${group_digest}" >> "$tmp_manifest"

    if (( MANIFEST_ONLY == 1 )); then
        continue
    fi

    group_dir="${WORK_DIR}/${group}"
    rm -rf "$group_dir"
    mkdir -p "$group_dir"
    pkgs="$(printf '%s\n' "${group_pkgs[@]}" | tr '\n' ' ')"

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg apt-utils dpkg-dev

case '${repo_kind}' in
  ubuntu)
    ;;
  docker)
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    . /etc/os-release
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \${VERSION_CODENAME} stable\" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    ;;
  amnezia)
    install -m 0755 -d /usr/share/keyrings
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
    gpg --dearmor -o /usr/share/keyrings/amnezia-ppa.gpg /tmp/amnezia.asc
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/amnezia-ppa.gpg] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu noble main\" > /etc/apt/sources.list.d/amnezia.list
    apt-get update -qq
    ;;
esac

rm -f /var/cache/apt/archives/*.deb
apt-get install --download-only -y -qq --no-install-recommends ${pkgs}
cp /var/cache/apt/archives/*.deb '/repo/${group_dir}/'
cd '/repo/${group_dir}'
dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
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

    docker run --rm \
        -e DEBIAN_FRONTEND=noninteractive \
        -v "${REPO_DIR}:/repo" \
        -w /repo \
        ubuntu:24.04 \
        bash -lc "
set -euo pipefail
apt-get update -qq
env DEBIAN_FRONTEND=noninteractive \
  apt-get -o Dpkg::Use-Pty=0 -o APT::Color=0 -o Dpkg::Progress-Fancy=0 \
  install --no-download --no-install-recommends -y '/repo/${group_dir}'/*.deb
"
done

mv "$tmp_manifest" "$MANIFEST_OUT"
echo "Manifest: $MANIFEST_OUT"

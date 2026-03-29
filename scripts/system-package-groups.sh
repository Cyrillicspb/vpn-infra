#!/usr/bin/env bash
# system-package-groups.sh — список групп системных .deb-бандлов.

system_package_group_names() {
    local group="${1:-}"

    case "$group" in
        home-core)
            cat <<'EOF'
curl
wget
git
jq
rsync
unzip
nftables
dnsmasq
python3
python3-pip
python3-venv
python3-cryptography
wireguard-tools
iproute2
sqlite3
net-tools
conntrack
traceroute
fail2ban
unattended-upgrades
apt-transport-https
logrotate
cron
gnupg2
ca-certificates
sshpass
autossh
ncat
tmux
uuid-runtime
openssl
dkms
build-essential
iperf3
nmap
netcat-openbsd
EOF
            ;;
        home-docker)
            cat <<'EOF'
docker-ce
docker-ce-cli
containerd.io
docker-compose-plugin
EOF
            ;;
        home-awg)
            cat <<'EOF'
amneziawg-dkms
amneziawg-tools
EOF
            ;;
        vps-core)
            cat <<'EOF'
curl
wget
git
jq
wireguard-tools
openssl
gnupg2
ca-certificates
python3
python3-pip
net-tools
mosh
nftables
dnsmasq
fail2ban
EOF
            ;;
        vps-docker)
            cat <<'EOF'
docker-ce
docker-ce-cli
containerd.io
docker-compose-plugin
EOF
            ;;
        all)
            {
                system_package_group_names home-core
                system_package_group_names home-docker
                system_package_group_names home-awg
                system_package_group_names vps-core
                system_package_group_names vps-docker
            } | awk '!seen[$0]++'
            ;;
        *)
            echo "Unknown system package group: ${group}" >&2
            return 1
            ;;
    esac
}

system_package_bundle_asset_name() {
    local group="${1:-}"

    case "$group" in
        home-core) echo "system-packages-home-core.tar.gz" ;;
        home-docker) echo "system-packages-home-docker.tar.gz" ;;
        home-awg) echo "system-packages-home-awg.tar.gz" ;;
        vps-core) echo "system-packages-vps-core.tar.gz" ;;
        vps-docker) echo "system-packages-vps-docker.tar.gz" ;;
        *)
            echo "Unknown system package bundle group: ${group}" >&2
            return 1
            ;;
    esac
}

system_package_bundle_groups() {
    cat <<'EOF'
home-core
home-docker
home-awg
vps-core
vps-docker
EOF
}

system_package_group_repo_kind() {
    local group="${1:-}"

    case "$group" in
        home-core|vps-core) echo "ubuntu" ;;
        home-docker|vps-docker) echo "docker" ;;
        home-awg) echo "amnezia" ;;
        *)
            echo "Unknown system package repo kind for group: ${group}" >&2
            return 1
            ;;
    esac
}

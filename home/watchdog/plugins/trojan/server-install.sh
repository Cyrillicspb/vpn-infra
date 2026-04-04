#!/usr/bin/env bash
set -euo pipefail
cd /opt/vpn
docker compose --profile extra-stacks up -d trojan-server

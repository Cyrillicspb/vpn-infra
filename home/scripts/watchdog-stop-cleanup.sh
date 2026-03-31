#!/usr/bin/env bash
set -euo pipefail

# Очистка runtime-состояния watchdog после stop/restart.
# Нужна даже при KillMode=control-group: helper-процессы уже убиты systemd,
# но stale pid/state files и старый route table marked могут остаться.

rm -f \
  /run/vpn-active-socks-port \
  /run/vpn-active-stack \
  /run/vpn-active-tun \
  /run/tun2socks-*.pid \
  /run/nfqws-*.pid

ip route replace unreachable default table marked >/dev/null 2>&1 || true

#!/usr/bin/env bash
# generate-nftables.sh — генерирует /etc/nftables.conf для текущего SERVER_MODE
#
# Режим hosted: просто копирует home/nftables/nftables.conf (без изменений).
# Режим gateway: добавляет LAN-правила:
#   - HAIRPIN NAT (chain prerouting_nat): перенаправление AWG/WG портов из LAN
#   - fwmark для LAN split tunneling в prerouting
#   - kill switch для LAN-трафика в forward
#   - LAN accept в forward
#   - DNS для LAN-устройств в input
#   - masquerade LAN→tun в postrouting
#
# Использование:
#   sudo bash generate-nftables.sh [--check]
#
# --check: только проверить синтаксис, не применять

set -euo pipefail

OPT_VPN="/opt/vpn"
ENV_FILE="$OPT_VPN/.env"
NFTABLES_CONF="/etc/nftables.conf"
NFTABLES_BAK="/etc/nftables.conf.bak"
NFTABLES_BASE="$OPT_VPN/home/nftables/nftables.conf"
CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

# Загрузить .env
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
fi

SERVER_MODE="${SERVER_MODE:-hosted}"

# ── Режим gateway: генерируем расширенный конфиг ─────────────────────────────

LAN_IFACE="${LAN_IFACE:-}"
LAN_SUBNET="${LAN_SUBNET:-}"
ROUTER_EXTERNAL_IP="${ROUTER_EXTERNAL_IP:-}"
CONTROL_DIRECT_IPS=""

for candidate in "${VPS_IP:-}" "${XRAY_SERVER:-}" "${BACKUP_VPS_HOST:-}" "${VPS_IP2:-}" "${VPS_IP3:-}" "${VPS2_IP:-}"; do
    [[ -n "$candidate" ]] || continue
    candidate="${candidate//\'/}"
    if [[ "$candidate" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        if [[ ",$CONTROL_DIRECT_IPS," != *",$candidate,"* ]]; then
            CONTROL_DIRECT_IPS+="${CONTROL_DIRECT_IPS:+,}$candidate"
        fi
    fi
done

if [[ "$SERVER_MODE" == "gateway" ]]; then
    [[ -n "$LAN_IFACE" ]] || { echo "ERROR: LAN_IFACE not set in $ENV_FILE" >&2; exit 1; }
    [[ -n "$LAN_SUBNET" ]] || { echo "ERROR: LAN_SUBNET not set in $ENV_FILE" >&2; exit 1; }
fi

TMP_CONF="$(mktemp /tmp/nftables-gw-XXXXXX.conf)"
trap "rm -f '$TMP_CONF'" EXIT

python3 - "$NFTABLES_BASE" "$TMP_CONF" "$SERVER_MODE" "$LAN_IFACE" "$LAN_SUBNET" "$ROUTER_EXTERNAL_IP" "$CONTROL_DIRECT_IPS" << 'PYEOF'
import ipaddress
import sys
import re

src_file, dst_file, server_mode, lan_iface, lan_subnet, router_ip, control_direct_ips_raw = sys.argv[1:]

if server_mode == "gateway":
    try:
        lan_subnet = str(ipaddress.ip_network(lan_subnet, strict=False))
    except ValueError as exc:
        raise SystemExit(f"Invalid LAN_SUBNET {lan_subnet!r}: {exc}")

control_direct_ips = []
for raw in control_direct_ips_raw.split(","):
    raw = raw.strip()
    if not raw:
        continue
    try:
        control_direct_ips.append(str(ipaddress.ip_address(raw)))
    except ValueError as exc:
        raise SystemExit(f"Invalid control IP {raw!r}: {exc}")

with open(src_file) as f:
    content = f.read()

# ── Блоки для вставки ────────────────────────────────────────────────────────

control_elements = f"        elements = {{ {', '.join(control_direct_ips)} }}" if control_direct_ips else ""
control_direct_block = f"""
    # ── Control plane endpoints: всегда напрямую, без fwmark → tun ──────────

    set control_direct_ips {{
        type ipv4_addr
{control_elements}
    }}

"""

# 1. HAIRPIN NAT chain + set (вставляется перед закрывающей } таблицы inet vpn)
elem_line = f"        elements = {{ {router_ip} }}" if router_ip.strip() else ""
gateway_nat_block = f"""
    # ── Gateway Mode: HAIRPIN NAT ─────────────────────────────────────────────

    set router_external_ips {{
        type ipv4_addr
{elem_line}
    }}

    chain prerouting_nat {{
        type nat hook prerouting priority dstnat; policy accept;
        # HAIRPIN: клиенты из LAN используют IP роутера для подключения к AWG/WG
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @router_external_ips udp dport 51820 redirect to :51820
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @router_external_ips udp dport 51821 redirect to :51821
    }}

"""

# 2. LAN fwmark правила в prerouting (перед закрывающей } chain prerouting)
lan_prerouting = f"""
        # Gateway Mode: LAN split tunneling fwmark
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @control_direct_ips accept
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @dpi_direct       meta mark set 0x2 accept
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @blocked_static   meta mark set 0x1 accept
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @blocked_dynamic  meta mark set 0x1 accept
        # QUIC drop для dpi_direct из LAN (принудить браузер к TCP для nfqws bypass)
        iifname "{lan_iface}" ip daddr @dpi_direct udp dport 443 drop
"""

control_prerouting = """
        # Control plane endpoints должны идти напрямую, иначе VPS-трафик сам
        # маркируется как blocked и зацикливается в tun.
        iifname "br-vpn" ip daddr @control_direct_ips accept
"""

control_output = """
        # Control plane endpoints всегда напрямую, без fwmark.
        ip daddr @control_direct_ips accept
"""

# 3. LAN kill switch в forward (вставляется сразу после открывающей строки chain forward)
lan_forward_kill_switch = f"""
        # Gateway Mode: LAN kill switch — заблокированное не должно утечь через eth0
        ip saddr {lan_subnet} ip daddr @blocked_static  oifname != "tun*" drop
        ip saddr {lan_subnet} ip daddr @blocked_dynamic oifname != "tun*" drop
"""

# 4. LAN accept в forward (вставляется перед закрывающей } chain forward)
lan_forward_accept = f"""
        # Gateway Mode: LAN forward accept
        iifname "{lan_iface}" ip saddr {lan_subnet} accept
        oifname "{lan_iface}" ip daddr {lan_subnet} accept
"""

# 5. DNS для LAN-устройств в input (вставляется перед закрывающей } chain input)
lan_input_dns = f"""
        # Gateway Mode: DNS для LAN-устройств
        iifname "{lan_iface}" udp dport 53 accept
        iifname "{lan_iface}" tcp dport 53 accept
"""

# 6. LAN masquerade в postrouting (вставляется перед закрывающей } chain postrouting)
lan_postrouting = f"""
        # Gateway Mode: masquerade LAN direct traffic через upstream router.
        # Без этого direct internet для LAN становится асимметричным:
        # запросы идут через home-server, а ответы возвращаются клиенту мимо него.
        ip saddr {lan_subnet} ip daddr != {lan_subnet} oifname "{lan_iface}" masquerade
        # Gateway Mode: masquerade LAN трафик через tun (заблокированное → VPS)
        ip saddr {lan_subnet} oifname "tun*" masquerade
"""


# ── Вспомогательные функции вставки ──────────────────────────────────────────

def insert_before_chain_close(text: str, chain_name: str, insert_text: str) -> str:
    """Вставляет текст перед закрывающей } указанной chain."""
    lines = text.split("\n")
    result = []
    in_chain = False
    brace_count = 0
    inserted = False
    for line in lines:
        if not in_chain and re.search(rf"\bchain\s+{re.escape(chain_name)}\b", line):
            in_chain = True
            brace_count = line.count("{") - line.count("}")
            result.append(line)
            continue
        if in_chain and not inserted:
            opens = line.count("{")
            closes = line.count("}")
            brace_count += opens - closes
            if brace_count == 0:
                # Закрывающая скобка chain — вставляем перед ней
                result.append(insert_text.rstrip())
                inserted = True
                in_chain = False
        result.append(line)
    return "\n".join(result)


def insert_after_chain_open(text: str, chain_name: str, insert_text: str) -> str:
    """Вставляет текст после первой реальной строки внутри chain."""
    lines = text.split("\n")
    result = []
    in_chain = False
    brace_count = 0
    inserted = False
    for line in lines:
        result.append(line)
        if not in_chain and re.search(rf"\bchain\s+{re.escape(chain_name)}\b", line):
            in_chain = True
            brace_count = line.count("{") - line.count("}")
            continue
        if in_chain and not inserted:
            brace_count += line.count("{") - line.count("}")
            if brace_count == 0:
                in_chain = False
                continue
            # Первая строка с содержимым (не пустая, не только комментарий) — вставляем перед ней
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # Убираем только что добавленную строку, вставляем блок, потом строку
                result.pop()
                result.append(insert_text.rstrip())
                result.append(line)
                inserted = True
    return "\n".join(result)


# ── Применяем все вставки ─────────────────────────────────────────────────────

# Control plane set в таблицу inet vpn
lines = content.split("\n")
result = []
brace_count = 0
in_table = False
set_inserted = False
for line in lines:
    result.append(line)
    if not in_table and re.search(r"\btable\s+inet\s+vpn\b", line):
        in_table = True
        brace_count = line.count("{") - line.count("}")
        continue
    if in_table and not set_inserted:
        brace_count += line.count("{") - line.count("}")
        stripped = line.strip()
        if brace_count > 0 and stripped.startswith("set blocked_static"):
            result.insert(len(result) - 1, control_direct_block.rstrip())
            set_inserted = True
        elif brace_count == 0:
            in_table = False
content = "\n".join(result)

# Control plane bypass в prerouting/output — должен быть ДО fwmark правил
content = insert_after_chain_open(content, "prerouting", control_prerouting)
content = insert_after_chain_open(content, "output", control_output)

if server_mode == "gateway":
    # LAN fwmark правила в prerouting
    content = insert_before_chain_close(content, "prerouting", lan_prerouting)

    # LAN kill switch в начало forward (сразу после open chain)
    content = insert_after_chain_open(content, "forward", lan_forward_kill_switch)

    # LAN accept в конец forward
    content = insert_before_chain_close(content, "forward", lan_forward_accept)

    # DNS для LAN в input
    content = insert_before_chain_close(content, "input", lan_input_dns)

    # LAN masquerade в postrouting
    content = insert_before_chain_close(content, "postrouting", lan_postrouting)

    # HAIRPIN NAT chain + set — вставляем перед закрывающей } таблицы inet vpn
    lines = content.split("\n")
    result = []
    brace_count = 0
    in_table = False
    table_end_inserted = False
    for line in lines:
        if not in_table and re.search(r"\btable\s+inet\s+vpn\b", line):
            in_table = True
            brace_count = line.count("{") - line.count("}")
            result.append(line)
            continue
        if in_table and not table_end_inserted:
            opens = line.count("{")
            closes = line.count("}")
            brace_count += opens - closes
            if brace_count == 0:
                result.append(gateway_nat_block.rstrip())
                table_end_inserted = True
        result.append(line)

    content = "\n".join(result)

with open(dst_file, "w") as f:
    f.write(content)

print(f"Gateway nftables config generated: {dst_file}")
PYEOF

# ── Проверка синтаксиса ───────────────────────────────────────────────────────

if ! nft -c -f "$TMP_CONF" 2>&1; then
    echo "ERROR: сгенерированный nftables конфиг содержит ошибки синтаксиса" >&2
    echo "Конфиг для отладки: $TMP_CONF" >&2
    trap - EXIT   # не удалять TMP_CONF
    exit 1
fi

if $CHECK_ONLY; then
    echo "OK: ${SERVER_MODE} config valid"
    exit 0
fi

# ── Бэкап и применение ────────────────────────────────────────────────────────

[[ -f "$NFTABLES_CONF" ]] && cp -f "$NFTABLES_CONF" "$NFTABLES_BAK"
cp -f "$TMP_CONF" "$NFTABLES_CONF"
nft -f "$NFTABLES_CONF"
echo "nftables: ${SERVER_MODE} mode applied"

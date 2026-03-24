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

# ── Режим hosted: просто копируем базовый конфиг ─────────────────────────────

if [[ "$SERVER_MODE" != "gateway" ]]; then
    if $CHECK_ONLY; then
        nft -c -f "$NFTABLES_BASE" && echo "OK: hosted config valid" || exit 1
    else
        cp -f "$NFTABLES_BASE" "$NFTABLES_CONF"
        nft -f "$NFTABLES_CONF"
        echo "nftables: hosted mode applied"
    fi
    exit 0
fi

# ── Режим gateway: генерируем расширенный конфиг ─────────────────────────────

LAN_IFACE="${LAN_IFACE:?ERROR: LAN_IFACE not set in $ENV_FILE}"
LAN_SUBNET="${LAN_SUBNET:?ERROR: LAN_SUBNET not set in $ENV_FILE}"
ROUTER_EXTERNAL_IP="${ROUTER_EXTERNAL_IP:-}"

TMP_CONF="$(mktemp /tmp/nftables-gw-XXXXXX.conf)"
trap "rm -f '$TMP_CONF'" EXIT

python3 - "$NFTABLES_BASE" "$TMP_CONF" "$LAN_IFACE" "$LAN_SUBNET" "$ROUTER_EXTERNAL_IP" << 'PYEOF'
import sys
import re

src_file, dst_file, lan_iface, lan_subnet, router_ip = sys.argv[1:]

with open(src_file) as f:
    content = f.read()

# ── Блоки для вставки ────────────────────────────────────────────────────────

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
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @dpi_direct       meta mark set 0x2 accept
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @blocked_static   meta mark set 0x1 accept
        iifname "{lan_iface}" ip saddr {lan_subnet} ip daddr @blocked_dynamic  meta mark set 0x1 accept
        # QUIC drop для dpi_direct из LAN (принудить браузер к TCP для nfqws bypass)
        iifname "{lan_iface}" ip daddr @dpi_direct udp dport 443 drop
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
    echo "OK: gateway config valid"
    exit 0
fi

# ── Бэкап и применение ────────────────────────────────────────────────────────

[[ -f "$NFTABLES_CONF" ]] && cp -f "$NFTABLES_CONF" "$NFTABLES_BAK"
cp -f "$TMP_CONF" "$NFTABLES_CONF"
nft -f "$NFTABLES_CONF"
echo "nftables: gateway mode applied (LAN=$LAN_SUBNET iface=$LAN_IFACE)"

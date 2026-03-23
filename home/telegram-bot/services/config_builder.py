"""
services/config_builder.py — Генератор WireGuard / AmneziaWG конфигов

Алгоритм:
  1. Загружает AllowedIPs из /etc/vpn-routes/combined.cidr
  2. Вычитает excludes устройства
  3. Рендерит Jinja2-шаблон (.conf.j2)
  4. Считает версию по SHA256 содержимого (первые 8 символов)
  5. Генерирует QR-код если AllowedIPs ≤ 50 записей
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import subprocess
from pathlib import Path
from typing import Optional

import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
COMBINED_CIDR = Path("/etc/vpn-routes/combined.cidr")
QR_MAX_IPS    = int(os.getenv("QR_MAX_ALLOWED_IPS", "50"))

# Параметры протоколов (CLAUDE.md)
AWG_PARAMS = {"Jc": 4, "Jmin": 50, "Jmax": 1000, "S1": 30, "S2": 40,
              "PersistentKeepalive": 25, "MTU": 1320}
WG_PARAMS  = {"PersistentKeepalive": 25, "MTU": 1320}


_DEVICE_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{2,30}$')


def wg_genkey() -> tuple[str, str]:
    """Генерация пары ключей wg. Возвращает (private_key, public_key)."""
    try:
        privkey = subprocess.check_output(["wg", "genkey"], text=True, timeout=5).strip()
        pubkey  = subprocess.check_output(["wg", "pubkey"], input=privkey, text=True, timeout=5).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("wg genkey failed: %s", exc)
        raise RuntimeError("Не удалось сгенерировать WireGuard ключ") from exc
    return privkey, pubkey


def _rand32() -> int:
    return random.randint(1, 2 ** 32 - 1)


def _load_allowed_ips(protocol: str, excludes: list[str]) -> list[str]:
    dns_entries = [
        "10.177.1.1/32" if protocol == "awg" else "10.177.3.1/32",
        "1.1.1.1/32",
    ]
    if not COMBINED_CIDR.exists():
        logger.warning(f"combined.cidr не найден: {COMBINED_CIDR}")
        return dns_entries + ["0.0.0.0/0"]

    lines = COMBINED_CIDR.read_text().splitlines()
    allowed = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]

    if excludes:
        allowed = [ip for ip in allowed if ip not in excludes]

    result = dns_entries[:]
    for ip in allowed:
        if ip not in result:
            result.append(ip)
    return result


def _render(device: dict, allowed_ips: list[str]) -> str:
    protocol = device.get("protocol", "awg")
    template_name = "awg.conf.j2" if protocol == "awg" else "wg.conf.j2"

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape([]),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    tmpl = env.get_template(template_name)
    pubkey_env = "AWG_SERVER_PUBLIC_KEY" if protocol == "awg" else "WG_SERVER_PUBLIC_KEY"
    host = os.getenv("WG_HOST", "")
    port = os.getenv("WG_AWG_PORT", "51820") if protocol == "awg" else os.getenv("WG_WG_PORT", "51821")

    return tmpl.render(
        private_key      = device.get("private_key", "REPLACE_ME"),
        ip_address       = device.get("ip_address", "10.177.1.2"),
        dns              = "10.177.1.1" if protocol == "awg" else "10.177.3.1",
        mtu              = AWG_PARAMS["MTU"] if protocol == "awg" else WG_PARAMS["MTU"],
        protocol         = protocol,
        server_public_key= os.getenv(pubkey_env, ""),
        preshared_key    = device.get("preshared_key", ""),
        allowed_ips      = allowed_ips,
        endpoint         = f"{host}:{port}",
        persistent_keepalive = AWG_PARAMS["PersistentKeepalive"],
        # AWG-specific (H1-H4 должны совпадать с сервером — читаем из .env)
        jc=AWG_PARAMS["Jc"], jmin=AWG_PARAMS["Jmin"], jmax=AWG_PARAMS["Jmax"],
        s1=AWG_PARAMS["S1"], s2=AWG_PARAMS["S2"],
        h1=int(os.getenv("AWG_H1", "1")), h2=int(os.getenv("AWG_H2", "2")),
        h3=int(os.getenv("AWG_H3", "3")), h4=int(os.getenv("AWG_H4", "4")),
    )


def _version(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def _make_qr(content: str) -> Optional[bytes]:
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(version=None,
                           error_correction=qrcode.constants.ERROR_CORRECT_L,
                           box_size=6, border=2)
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug(f"QR: {exc}")
        return None


PLATFORM_SCRIPTS: dict[str, dict] = {
    "windows": {
        "ext": "ps1",
        "label": "Windows (.ps1)",
        "template": """\
# AmneziaWG / WireGuard installer for Windows
# Run as Administrator in PowerShell

$ErrorActionPreference = "Stop"
$configContent = @'
{conf}
'@

Write-Host "Installing WireGuard..." -ForegroundColor Cyan
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {{
    Write-Host "winget not found. Download WireGuard from https://www.wireguard.com/install/" -ForegroundColor Red
    exit 1
}}
winget install --id WireGuard.WireGuard -e --silent

$configDir = "$env:ProgramFiles\\WireGuard\\Data\\Configurations"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$configPath = "$configDir\\{name}.conf"
$configContent | Set-Content -Path $configPath -Encoding UTF8
Write-Host "Config saved to $configPath" -ForegroundColor Green

# Install tunnel service
& "$env:ProgramFiles\\WireGuard\\wireguard.exe" /installtunnelservice $configPath
Write-Host "Tunnel service installed. VPN is starting..." -ForegroundColor Green
Write-Host "Open WireGuard from the system tray to manage the connection." -ForegroundColor Cyan
""",
    },
    "macos": {
        "ext": "command",
        "label": "macOS (.command)",
        "template": """\
#!/bin/bash
# AmneziaWG / WireGuard installer for macOS
set -e

CONFIG='{conf_escaped}'
NAME="{name}"

echo "Installing WireGuard via Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Install it from https://brew.sh"
    exit 1
fi
brew install wireguard-tools

CONFIG_DIR="/usr/local/etc/wireguard"
sudo mkdir -p "$CONFIG_DIR"
echo "$CONFIG" | sudo tee "$CONFIG_DIR/$NAME.conf" > /dev/null
sudo chmod 600 "$CONFIG_DIR/$NAME.conf"
echo "Config saved. Starting VPN..."
sudo wg-quick up "$NAME"
echo "VPN started! To stop: sudo wg-quick down $NAME"
""",
    },
    "linux": {
        "ext": "sh",
        "label": "Linux (.sh)",
        "template": """\
#!/bin/bash
# AmneziaWG / WireGuard installer for Linux
set -e

CONFIG='{conf_escaped}'
NAME="{name}"

echo "Installing WireGuard..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y wireguard
elif command -v dnf &>/dev/null; then
    sudo dnf install -y wireguard-tools
elif command -v pacman &>/dev/null; then
    sudo pacman -S --noconfirm wireguard-tools
else
    echo "Unknown package manager. Install wireguard-tools manually."
    exit 1
fi

sudo mkdir -p /etc/wireguard
echo "$CONFIG" | sudo tee "/etc/wireguard/$NAME.conf" > /dev/null
sudo chmod 600 "/etc/wireguard/$NAME.conf"
echo "Starting VPN..."
sudo systemctl enable --now "wg-quick@$NAME"
echo "VPN started! Status: sudo systemctl status wg-quick@$NAME"
""",
    },
}


def build_installer(device_name: str, conf_text: str, platform: str) -> Optional[bytes]:
    """Сгенерировать скрипт-установщик с вшитым конфигом для указанной платформы."""
    if not _DEVICE_NAME_RE.match(device_name):
        raise ValueError(f"Invalid device name: {device_name!r}")
    info = PLATFORM_SCRIPTS.get(platform)
    if not info:
        return None
    conf_escaped = conf_text.replace("'", "'\\''")  # для bash single-quote escape
    # PowerShell: escape backtick and $ to prevent variable expansion inside here-string
    ps_conf = conf_text.replace("`", "``").replace("$", "`$")
    script = info["template"].format(
        conf=ps_conf if platform == "windows" else conf_text,
        conf_escaped=conf_escaped,
        name=device_name,
    )
    return script.encode("utf-8")


class ConfigBuilder:
    """Строит .conf + опционально QR для устройства."""

    async def ensure_keys(self, device: dict) -> dict:
        """Если у устройства нет ключей — генерируем. Возвращает обновлённый dict."""
        if device.get("private_key"):
            return device
        privkey, pubkey = wg_genkey()
        return {**device, "private_key": privkey, "public_key": pubkey}

    async def build(
        self,
        device: dict,
        excludes: Optional[list[str]] = None,
    ) -> tuple[str, Optional[bytes], str]:
        """
        Возвращает (conf_text, qr_png_or_None, version_hash).
        Роутеры (is_router=True) получают AllowedIPs = 0.0.0.0/0 — split tunneling
        выполняется на сервере через nftables.
        """
        excludes   = excludes or []
        protocol   = device.get("protocol", "awg")
        if device.get("is_router"):
            dns = "10.177.1.1/32" if protocol == "awg" else "10.177.3.1/32"
            allowed = [dns, "0.0.0.0/0"]
        else:
            allowed = _load_allowed_ips(protocol, excludes)
        conf_text  = _render(device, allowed)
        version    = _version(conf_text)
        qr: Optional[bytes] = _make_qr(conf_text) if len(allowed) <= QR_MAX_IPS else None
        return conf_text, qr, version

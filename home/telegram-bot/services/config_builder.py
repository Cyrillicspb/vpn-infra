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
import base64
from pathlib import Path
from typing import Optional

import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
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
    """Генерация пары ключей WireGuard без вызова внешнего `wg`."""
    try:
        private_key = x25519.X25519PrivateKey.generate()
        public_key = private_key.public_key()
        priv_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        privkey = base64.b64encode(priv_bytes).decode("ascii")
        pubkey = base64.b64encode(pub_bytes).decode("ascii")
    except Exception as exc:
        logger.error("WireGuard key generation failed: %s", exc)
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


def _conf_to_bat_echo(conf_text: str) -> str:
    """Конвертирует конфиг в серию bat echo-команд для встраивания в .bat файл."""
    lines = []
    for line in conf_text.splitlines():
        line = line.replace("%", "%%").replace("^", "^^").replace("&", "^&")
        line = line.replace("|", "^|").replace("<", "^<").replace(">", "^>")
        if line.strip():
            lines.append(f"echo {line}")
        else:
            lines.append("echo.")
    return "\n".join(lines)


PLATFORM_SCRIPTS: dict[str, dict] = {
    "windows": {
        "ext": "bat",
        "label": "Windows (.bat)",
        "template": """\
@echo off
setlocal enabledelayedexpansion
title VPN Setup - {name}

:: Проверка прав через fltmc
fltmc >nul 2>&1 || (
    powershell -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs"
    exit /b
)

:: Ручная установка: https://github.com/amnezia-vpn/amnezia-client/releases/latest (AWG)
::                   https://download.wireguard.com/windows-client/ (WG)

set PROTOCOL={protocol}
set TUNNEL_NAME={name}

echo ============================================
echo  Установка VPN туннеля: %TUNNEL_NAME%
echo ============================================
echo.

set CONF_FILE=%TEMP%\\%TUNNEL_NAME%.conf
(
{conf_bat}
) > "%CONF_FILE%"

if "%PROTOCOL%"=="awg" (
    echo [1/2] Установка AmneziaVPN...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install --id AmneziaVPN.AmneziaVPN -e --silent --accept-source-agreements --accept-package-agreements 2>nul
    )
    if exist "%ProgramFiles%\\AmneziaVPN\\AmneziaVPN.exe" (
        echo   OK: AmneziaVPN установлен
    ) else (
        echo   AmneziaVPN не найден через winget.
        echo   Скачайте вручную: https://github.com/amnezia-vpn/amnezia-client/releases/latest
        echo   После установки запустите этот скрипт ещё раз.
        pause
        exit /b 1
    )
    echo [2/2] Импорт конфига...
    copy /Y "%CONF_FILE%" "%APPDATA%\\AmneziaVPN\\" >nul 2>&1 || echo   Откройте AmneziaVPN и импортируйте файл %CONF_FILE% вручную
) else (
    echo [1/2] Установка WireGuard...
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install --id WireGuard.WireGuard -e --silent --accept-source-agreements --accept-package-agreements 2>nul
    )
    if exist "%ProgramFiles%\\WireGuard\\wireguard.exe" (
        echo   OK: WireGuard установлен
    ) else (
        echo   WireGuard не найден через winget.
        echo   Скачайте вручную: https://download.wireguard.com/windows-client/
        pause
        exit /b 1
    )
    echo [2/2] Установка туннеля...
    "%ProgramFiles%\\WireGuard\\wireguard.exe" /installtunnelservice "%CONF_FILE%"
    if !errorlevel! equ 0 (
        echo   OK: Туннель %TUNNEL_NAME% установлен и запущен
    ) else (
        echo   Импортируйте %CONF_FILE% вручную через интерфейс WireGuard
    )
)

del "%CONF_FILE%" >nul 2>&1
echo.
echo Готово! Подключение: %TUNNEL_NAME%
pause
""",
    },
    "macos": {
        "ext": "command",
        "label": "macOS (.command)",
        "template": """\
#!/bin/bash
set -e
TUNNEL_NAME="{name}"
PROTOCOL="{protocol}"

# Ручная установка: https://apps.apple.com/app/amneziavpn/id1600529900 (AWG)
#                   https://apps.apple.com/app/wireguard/id1451685025 (WG)

echo "============================================"
echo " Установка VPN туннеля: $TUNNEL_NAME"
echo "============================================"
echo

xattr -d com.apple.quarantine "$0" 2>/dev/null || true

CONF_FILE="/tmp/$TUNNEL_NAME.conf"
cat > "$CONF_FILE" << 'WGCONF'
{conf}
WGCONF

if [ "$PROTOCOL" = "awg" ]; then
    echo "[1/2] Проверка AmneziaVPN..."
    if ! [ -d "/Applications/AmneziaVPN.app" ]; then
        echo "  AmneziaVPN не установлен."
        echo "  Установите из App Store: https://apps.apple.com/app/amneziavpn/id1600529900"
        echo "  Затем запустите этот скрипт ещё раз."
        open "https://apps.apple.com/app/amneziavpn/id1600529900"
        read -p "  Нажмите Enter после установки..."
    fi
    echo "[2/2] Импорт конфига..."
    cp "$CONF_FILE" ~/Downloads/"$TUNNEL_NAME.conf"
    echo "  Файл сохранён: ~/Downloads/$TUNNEL_NAME.conf"
    echo "  Откройте AmneziaVPN -> Добавить туннель -> Из файла"
    open ~/Downloads/
else
    echo "[1/2] Проверка WireGuard..."
    if ! [ -d "/Applications/WireGuard.app" ]; then
        echo "  WireGuard не установлен."
        echo "  Установите из App Store: https://apps.apple.com/app/wireguard/id1451685025"
        open "https://apps.apple.com/app/wireguard/id1451685025"
        read -p "  Нажмите Enter после установки..."
    fi
    echo "[2/2] Импорт конфига..."
    cp "$CONF_FILE" ~/Downloads/"$TUNNEL_NAME.conf"
    echo "  Файл сохранён: ~/Downloads/$TUNNEL_NAME.conf"
    echo "  Откройте WireGuard -> Добавить туннель -> Из файла"
    open ~/Downloads/
fi

rm -f "$CONF_FILE"
echo
echo "Готово! Откройте приложение и активируйте туннель $TUNNEL_NAME"
read -p "Нажмите Enter для закрытия..."
""",
    },
    "linux": {
        "ext": "sh",
        "label": "Linux (.sh)",
        "template": """\
#!/bin/bash
set -euo pipefail
TUNNEL_NAME="{name}"
PROTOCOL="{protocol}"

# Ручная установка AWG: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module
# Ручная установка WG:  https://www.wireguard.com/install/

echo "============================================"
echo " Установка VPN туннеля: $TUNNEL_NAME"
echo "============================================"
echo

if [ "$(id -u)" -ne 0 ]; then
    echo "Запуск с sudo..."
    exec sudo bash "$0" "$@"
fi

mkdir -p /etc/wireguard
CONF_FILE="/etc/wireguard/$TUNNEL_NAME.conf"
cat > "$CONF_FILE" << 'WGCONF'
{conf}
WGCONF
chmod 600 "$CONF_FILE"

PKG_MANAGER=""
if command -v apt-get &>/dev/null; then PKG_MANAGER="apt"
elif command -v dnf &>/dev/null; then PKG_MANAGER="dnf"
elif command -v pacman &>/dev/null; then PKG_MANAGER="pacman"
fi

if [ "$PROTOCOL" = "awg" ]; then
    echo "[1/3] Установка AmneziaWG..."
    if ! command -v awg &>/dev/null; then
        if [ "$PKG_MANAGER" = "apt" ]; then
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:amnezia/ppa 2>/dev/null || true
            apt-get update -qq
            apt-get install -y amneziawg amneziawg-tools || {
                echo "  PPA недоступен. Установите вручную:"
                echo "  https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
                exit 1
            }
        elif [ "$PKG_MANAGER" = "dnf" ]; then
            dnf install -y amneziawg-tools 2>/dev/null || {
                echo "  Установите вручную: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
                exit 1
            }
        else
            echo "  Установите AmneziaWG вручную: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module"
            exit 1
        fi
    fi
    echo "[2/3] Загрузка модуля ядра..."
    modprobe amneziawg 2>/dev/null || true
    echo "[3/3] Запуск туннеля..."
    systemctl enable --now awg-quick@"$TUNNEL_NAME" 2>/dev/null \
        || awg-quick up "$TUNNEL_NAME"
else
    echo "[1/3] Установка WireGuard..."
    if ! command -v wg &>/dev/null; then
        case "$PKG_MANAGER" in
            apt)    apt-get install -y wireguard wireguard-tools ;;
            dnf)    dnf install -y wireguard-tools ;;
            pacman) pacman -S --noconfirm wireguard-tools ;;
            *)      echo "Установите wireguard-tools вручную"; exit 1 ;;
        esac
    fi
    echo "[2/3] (пропуск модуля)"
    echo "[3/3] Запуск туннеля..."
    systemctl enable --now wg-quick@"$TUNNEL_NAME" 2>/dev/null \
        || wg-quick up "$TUNNEL_NAME"
fi

echo
echo "Готово! Туннель $TUNNEL_NAME активен."
echo "Статус: wg show $TUNNEL_NAME"
""",
    },
}


def build_installer(device_name: str, conf_text: str, platform: str, protocol: str = "awg") -> Optional[bytes]:
    """Сгенерировать скрипт-установщик с вшитым конфигом для указанной платформы."""
    safe_name = re.sub(r'[^\w\-]', '_', device_name)
    info = PLATFORM_SCRIPTS.get(platform)
    if not info:
        return None
    if platform == "windows":
        conf_bat = _conf_to_bat_echo(conf_text)
        script = info["template"].format(
            name=safe_name,
            protocol=protocol,
            conf_bat=conf_bat,
        )
    else:
        script = info["template"].format(
            name=safe_name,
            protocol=protocol,
            conf=conf_text,
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

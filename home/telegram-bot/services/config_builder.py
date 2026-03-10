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

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
COMBINED_CIDR = Path("/etc/vpn-routes/combined.cidr")
QR_MAX_IPS    = int(os.getenv("QR_MAX_ALLOWED_IPS", "50"))

# Параметры протоколов (CLAUDE.md)
AWG_PARAMS = {"Jc": 4, "Jmin": 50, "Jmax": 1000, "S1": 30, "S2": 40,
              "PersistentKeepalive": 25, "MTU": 1320}
WG_PARAMS  = {"PersistentKeepalive": 25, "MTU": 1320}


def wg_genkey() -> tuple[str, str]:
    """Генерация пары ключей wg. Возвращает (private_key, public_key)."""
    privkey = subprocess.check_output(["wg", "genkey"], text=True).strip()
    pubkey  = subprocess.check_output(["wg", "pubkey"], input=privkey, text=True).strip()
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
    )
    tmpl = env.get_template(template_name)
    pubkey_env = "WG_SERVER_PUBKEY_AWG" if protocol == "awg" else "WG_SERVER_PUBKEY_WG"
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
        # AWG-specific
        jc=AWG_PARAMS["Jc"], jmin=AWG_PARAMS["Jmin"], jmax=AWG_PARAMS["Jmax"],
        s1=AWG_PARAMS["S1"], s2=AWG_PARAMS["S2"],
        h1=_rand32(), h2=_rand32(), h3=_rand32(), h4=_rand32(),
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
        """
        excludes   = excludes or []
        protocol   = device.get("protocol", "awg")
        allowed    = _load_allowed_ips(protocol, excludes)
        conf_text  = _render(device, allowed)
        version    = _version(conf_text)
        qr: Optional[bytes] = _make_qr(conf_text) if len(allowed) <= QR_MAX_IPS else None
        return conf_text, qr, version

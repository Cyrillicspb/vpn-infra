"""
services/config_builder.py — Генератор конфигурационных файлов WireGuard/AmneziaWG

Версия конфига = хеш содержимого (не инкрементальная)
QR-код только если AllowedIPs <= 50 записей
"""
import hashlib
import io
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
COMBINED_CIDR = "/etc/vpn-routes/combined.cidr"

# AmneziaWG параметры обфускации
AWG_PARAMS = {
    "Jc": 4,
    "Jmin": 50,
    "Jmax": 1000,
    "S1": 30,
    "S2": 40,
}


class ConfigBuilder:
    def __init__(self):
        self.wg_host = os.getenv("WG_HOST", "")
        self.awg_port = os.getenv("WG_AWG_PORT", "51820")
        self.wg_port = os.getenv("WG_WG_PORT", "51821")
        self.mtu = os.getenv("WG_MTU", "1320")

    async def build(self, device: dict) -> Tuple[str, Optional[bytes]]:
        """
        Генерация конфига для устройства.
        Возвращает (conf_text, qr_bytes_or_None).
        """
        protocol = device.get("protocol", "awg")
        template_file = TEMPLATES_DIR / ("awg.conf.j2" if protocol == "awg" else "wg.conf.j2")

        if not template_file.exists():
            raise FileNotFoundError(f"Шаблон не найден: {template_file}")

        # Загружаем AllowedIPs
        allowed_ips = self._load_allowed_ips(device)

        # Загружаем шаблон
        template = template_file.read_text()

        # Получаем ключи устройства
        private_key = device.get("private_key", "<PRIVATE_KEY_PLACEHOLDER>")
        server_public_key = self._get_server_public_key(protocol)

        # Подставляем переменные
        context = {
            "PRIVATE_KEY": private_key,
            "SERVER_PUBLIC_KEY": server_public_key,
            "CLIENT_IP": device.get("ip_address", "10.177.1.2"),
            "DNS": "10.177.1.1, 1.1.1.1" if protocol == "awg" else "10.177.3.1, 1.1.1.1",
            "MTU": self.mtu,
            "ENDPOINT": f"{self.wg_host}:{self.awg_port if protocol == 'awg' else self.wg_port}",
            "ALLOWED_IPS": ", ".join(allowed_ips),
            "KEEPALIVE": "25",
        }

        # AWG-специфичные параметры
        if protocol == "awg":
            for key, val in AWG_PARAMS.items():
                context[key] = str(val)
            # H1-H4: генерируем из device peer_id для детерминированности
            import random
            seed = hash(device.get("peer_id", device.get("device_name", "default")))
            rng = random.Random(seed)
            for h in ["H1", "H2", "H3", "H4"]:
                context[h] = str(rng.randint(1, 2**32 - 1))

        conf_text = template
        for key, val in context.items():
            conf_text = conf_text.replace(f"{{{{{key}}}}}", val)

        # Версия конфига = хеш содержимого
        version = hashlib.sha256(conf_text.encode()).hexdigest()[:12]

        # QR-код только если AllowedIPs <= 50
        qr_bytes = None
        if len(allowed_ips) <= 50:
            try:
                qr_bytes = self._generate_qr(conf_text)
            except Exception as e:
                logger.warning(f"QR-код не сгенерирован: {e}")

        return conf_text, qr_bytes

    def _load_allowed_ips(self, device: dict) -> list:
        """Загрузка AllowedIPs из combined.cidr с учётом исключений."""
        allowed = []

        # Базовые маршруты
        allowed.append("10.177.1.1/32")   # DNS
        allowed.append("1.1.1.1/32")       # Резервный DNS

        # Загружаем из combined.cidr
        if os.path.exists(COMBINED_CIDR):
            with open(COMBINED_CIDR) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        allowed.append(line)
        else:
            # Fallback: минимальный набор
            allowed.extend([
                "8.8.8.8/32",
                "8.8.4.4/32",
            ])

        # Убираем исключения устройства
        excludes = device.get("excludes", [])
        if excludes:
            allowed = [ip for ip in allowed if ip not in excludes]

        return list(dict.fromkeys(allowed))  # Дедупликация

    def _get_server_public_key(self, protocol: str) -> str:
        """Получение публичного ключа сервера."""
        key_file = f"/etc/wireguard/wg0-server.pub" if protocol == "awg" else "/etc/wireguard/wg1-server.pub"
        try:
            return Path(key_file).read_text().strip()
        except Exception:
            return "<SERVER_PUBLIC_KEY>"

    def _generate_qr(self, conf_text: str) -> bytes:
        """Генерация QR-кода."""
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(conf_text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def config_version(self, conf_text: str) -> str:
        return hashlib.sha256(conf_text.encode()).hexdigest()[:12]

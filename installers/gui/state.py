"""
InstallerState — хранит параметры установки между экранами.
Персистирует в ~/.vpn-installer-state.json (без секретов).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_FILE = Path.home() / ".vpn-installer-state.json"


@dataclass
class InstallerState:
    # VPS
    vps_ip: str = ""
    vps_ssh_port: str = "22"
    # Telegram
    telegram_admin_chat_id: str = ""
    # Network / mode
    server_mode: str = "A"        # "A" = hosting, "B" = home behind router
    lan_iface: str = ""           # detected default interface
    lan_ip: str = ""              # detected LAN IP
    cgnat_detected: bool = False
    external_ip: str = ""         # обнаруженный внешний IP (icanhazip.com)
    router_external_ip: str = ""  # только для режима B: внешний IP роутера
    # Options
    use_cloudflare: str = "n"
    use_ddns: str = "n"
    # In-memory only (never saved to disk)
    vps_root_password: str = ""
    telegram_bot_token: str = ""
    # Progress tracking
    current_step: int = 0
    setup_completed: bool = False

    @classmethod
    def load(cls) -> "InstallerState":
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                valid = {
                    k: v
                    for k, v in data.items()
                    if k in cls.__dataclass_fields__
                }
                return cls(**valid)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        """Persist non-sensitive fields only (no passwords/tokens)."""
        data = asdict(self)
        for secret in ("vps_root_password", "telegram_bot_token"):
            data.pop(secret, None)
        STATE_FILE.write_text(json.dumps(data, indent=2))
        STATE_FILE.chmod(0o600)

    def to_env(self) -> dict[str, str]:
        """Build env vars dict for setup.sh subprocess."""
        env: dict[str, str] = {}
        if self.vps_ip:
            env["VPS_IP"] = self.vps_ip
            env["XRAY_SERVER"] = self.vps_ip
        env["VPS_SSH_PORT"] = self.vps_ssh_port or "22"
        if self.vps_root_password:
            env["VPS_ROOT_PASSWORD"] = self.vps_root_password
        if self.telegram_bot_token:
            env["TELEGRAM_BOT_TOKEN"] = self.telegram_bot_token
        if self.telegram_admin_chat_id:
            env["TELEGRAM_ADMIN_CHAT_ID"] = self.telegram_admin_chat_id
        # Non-interactive mode: skip all raw read() prompts
        env["USE_CLOUDFLARE"] = self.use_cloudflare or "n"
        env["USE_DDNS"] = self.use_ddns or "n"
        # Non-interactive mode: skip all raw read() prompts
        env["VPN_NONINTERACTIVE"] = "1"
        # Mode B: gateway behind router
        if self.server_mode == "B":
            env["SERVER_MODE"] = "gateway"
            if self.lan_iface:
                env["LAN_IFACE"] = self.lan_iface
            if self.router_external_ip:
                env["ROUTER_EXTERNAL_IP"] = self.router_external_ip
        return env

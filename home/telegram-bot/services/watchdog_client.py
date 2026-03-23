"""
services/watchdog_client.py — HTTP клиент для watchdog API
"""
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class WatchdogError(Exception):
    pass


class WatchdogClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, timeout: int = 15) -> Any:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self._base_url}{path}",
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                if r.status == 200:
                    ct = r.headers.get("Content-Type", "")
                    if "json" in ct:
                        return await r.json()
                    return await r.read()
                raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
        except aiohttp.ClientError as exc:
            raise WatchdogError("Watchdog недоступен") from None

    async def _post(self, path: str, data: Optional[dict] = None, timeout: int = 30) -> Any:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self._base_url}{path}",
                json=data or {},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                if r.status in (200, 202):
                    ct = r.headers.get("Content-Type", "")
                    if "json" in ct:
                        return await r.json()
                    return await r.read()
                raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
        except aiohttp.ClientError as exc:
            raise WatchdogError("Watchdog недоступен") from None

    # -----------------------------------------------------------------------
    # Status / metrics
    # -----------------------------------------------------------------------
    async def get_status(self) -> dict:
        return await self._get("/status")

    async def get_metrics(self) -> str:
        data = await self._get("/metrics")
        return data.decode() if isinstance(data, bytes) else str(data)

    # -----------------------------------------------------------------------
    # Peers
    # -----------------------------------------------------------------------
    async def get_peers(self) -> dict:
        return await self._get("/peer/list")

    async def add_peer(self, name: str, protocol: str, public_key: str = "", ip: str = "") -> dict:
        data: dict = {"name": name, "protocol": protocol, "public_key": public_key}
        if ip:
            data["ip"] = ip
        return await self._post("/peer/add", data)

    async def remove_peer(self, peer_id: str, interface: Optional[str] = None) -> dict:
        data: dict = {"peer_id": peer_id}
        if interface:
            data["interface"] = interface
        return await self._post("/peer/remove", data)

    # -----------------------------------------------------------------------
    # Stacks
    # -----------------------------------------------------------------------
    async def switch_stack(self, stack: str) -> dict:
        return await self._post("/switch", {"stack": stack})

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------
    async def update_routes(self) -> dict:
        return await self._post("/routes/update")

    # -----------------------------------------------------------------------
    # Services
    # -----------------------------------------------------------------------
    async def restart_service(self, service: str) -> dict:
        return await self._post("/service/restart", {"service": service})

    async def update_service(self, service: str = "all") -> dict:
        return await self._post("/service/update", {"service": service})

    # -----------------------------------------------------------------------
    # Deploy
    # -----------------------------------------------------------------------
    async def deploy(self, version: Optional[str] = None, force: bool = False) -> dict:
        return await self._post("/deploy", {"version": version, "force": force})

    async def rollback(self) -> dict:
        return await self._post("/rollback")

    async def skip_version(self, version: str) -> dict:
        return await self._post("/deploy/skip", {"version": version})

    async def reload_plugins(self) -> dict:
        return await self._post("/reload-plugins")

    # -----------------------------------------------------------------------
    # VPS
    # -----------------------------------------------------------------------
    async def get_vps_list(self) -> dict:
        return await self._get("/vps/list")

    async def add_vps(self, ip: str, ssh_port: int = 443, tunnel_ip: str = "") -> dict:
        return await self._post("/vps/add", {"ip": ip, "ssh_port": ssh_port, "tunnel_ip": tunnel_ip})

    async def install_vps(self, ip: str, password: str, ssh_port: int = 22) -> dict:
        """Запустить полную установку VPS через add-vps.sh (202 Accepted, прогресс в Telegram)."""
        return await self._post(
            "/vps/install",
            {"ip": ip, "password": password, "ssh_port": ssh_port},
            timeout=15,
        )

    async def remove_vps(self, ip: str) -> dict:
        return await self._post("/vps/remove", {"ip": ip})

    # -----------------------------------------------------------------------
    # Graph / diagnostics / notify
    # -----------------------------------------------------------------------
    async def get_graph(self, panel: str = "tunnel", period: str = "1h") -> bytes:
        data = await self._post("/graph", {"panel": panel, "period": period}, timeout=45)
        return data if isinstance(data, bytes) else b""

    async def diagnose(self, device: str) -> dict:
        return await self._post(f"/diagnose/{device}")

    async def notify_clients(self, message: str) -> dict:
        return await self._post("/notify-clients", {"message": message})

    async def assess(self) -> dict:
        return await self._post("/assess")

    async def check_domain(self, domain: str) -> dict:
        return await self._post("/check", {"domain": domain})

    # -----------------------------------------------------------------------
    # DPI bypass (zapret lane)
    # -----------------------------------------------------------------------
    async def get_dpi_status(self) -> dict:
        return await self._get("/dpi/status")

    async def dpi_enable(self) -> dict:
        return await self._post("/dpi/enable")

    async def dpi_disable(self) -> dict:
        return await self._post("/dpi/disable")

    async def dpi_add_service(
        self,
        name: str = "",
        display: str = "",
        domains: Optional[list] = None,
        preset: Optional[str] = None,
    ) -> dict:
        data: dict = {}
        if preset:
            data["preset"] = preset
        else:
            data["name"] = name
            if display:
                data["display"] = display
            if domains:
                data["domains"] = domains
        return await self._post("/dpi/service/add", data)

    async def dpi_remove_service(self, name: str) -> dict:
        return await self._post("/dpi/service/remove", {"name": name})

    async def dpi_toggle_service(self, name: str, enabled: bool) -> dict:
        return await self._post("/dpi/service/toggle", {"name": name, "enabled": enabled})

    # -----------------------------------------------------------------------
    # Backup
    # -----------------------------------------------------------------------
    async def backup(self) -> dict:
        return await self._post("/backup", timeout=30)

    # -----------------------------------------------------------------------
    # Fail2ban
    # -----------------------------------------------------------------------
    async def get_fail2ban_status(self) -> dict:
        return await self._get("/fail2ban/status", timeout=40)

    async def fail2ban_unban(self, server: str, jail: str, ip: str) -> dict:
        return await self._post("/fail2ban/unban", {"server": server, "jail": jail, "ip": ip})

    # -----------------------------------------------------------------------
    # mTLS renew
    # -----------------------------------------------------------------------
    async def renew_cert(self) -> dict:
        return await self._post("/renew-cert", timeout=65)

    async def renew_ca(self) -> dict:
        return await self._post("/renew-ca", timeout=65)

    # -----------------------------------------------------------------------
    # NFT stats / rotation log / DPI test
    # -----------------------------------------------------------------------
    async def get_nft_stats(self) -> dict:
        return await self._get("/nft/stats")

    async def get_rotation_log(self) -> dict:
        return await self._get("/rotation-log")

    async def dpi_test(self, domains: list | None = None) -> dict:
        return await self._post("/dpi/test", {"domains": domains}, timeout=30)

    async def zapret_probe(self, mode: str = "quick") -> dict:
        return await self._post("/zapret/probe", {"mode": mode})

    async def get_zapret_history(self) -> dict:
        return await self._get("/zapret/history")

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
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str, timeout: int = 15) -> Any:
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    f"{self.base_url}{path}",
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    if r.status == 200:
                        ct = r.headers.get("Content-Type", "")
                        if "json" in ct:
                            return await r.json()
                        return await r.read()
                    raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
            except aiohttp.ClientError as exc:
                raise WatchdogError(f"Watchdog недоступен: {exc}") from exc

    async def _post(self, path: str, data: Optional[dict] = None, timeout: int = 30) -> Any:
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(
                    f"{self.base_url}{path}",
                    json=data or {},
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    if r.status in (200, 202):
                        ct = r.headers.get("Content-Type", "")
                        if "json" in ct:
                            return await r.json()
                        return await r.read()
                    raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
            except aiohttp.ClientError as exc:
                raise WatchdogError(f"Watchdog недоступен: {exc}") from exc

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

    async def add_peer(self, name: str, protocol: str, public_key: str = "") -> dict:
        return await self._post("/peer/add", {
            "name": name, "protocol": protocol, "public_key": public_key,
        })

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

    async def reload_plugins(self) -> dict:
        return await self._post("/reload-plugins")

    # -----------------------------------------------------------------------
    # VPS
    # -----------------------------------------------------------------------
    async def get_vps_list(self) -> dict:
        return await self._get("/vps/list")

    async def add_vps(self, ip: str, ssh_port: int = 443, tunnel_ip: str = "") -> dict:
        return await self._post("/vps/add", {"ip": ip, "ssh_port": ssh_port, "tunnel_ip": tunnel_ip})

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

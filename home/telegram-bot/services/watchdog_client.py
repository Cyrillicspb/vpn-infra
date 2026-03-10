"""
services/watchdog_client.py — HTTP клиент для watchdog API
"""
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class WatchdogClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}{path}",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                r.raise_for_status()
                return await r.json()

    async def _post(self, path: str, data: dict = None) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}{path}",
                json=data or {},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                r.raise_for_status()
                return await r.json()

    async def get_status(self) -> dict:
        return await self._get("/status")

    async def get_metrics(self) -> dict:
        return await self._get("/metrics")

    async def get_peers(self) -> dict:
        return await self._get("/peer/list")

    async def switch_stack(self, stack: str) -> dict:
        return await self._post("/switch", {"stack": stack})

    async def restart_service(self, service: str) -> dict:
        return await self._post(f"/service/restart?service={service}")

    async def update_routes(self) -> dict:
        return await self._post("/routes/update")

    async def deploy(self, version: Optional[str] = None, force: bool = False) -> dict:
        return await self._post("/deploy", {"version": version, "force": force})

    async def rollback(self) -> dict:
        return await self._post("/rollback")

    async def add_peer(self, name: str, protocol: str, public_key: str = "") -> dict:
        return await self._post("/peer/add", {"name": name, "protocol": protocol, "public_key": public_key})

    async def remove_peer(self, peer_id: str) -> dict:
        return await self._post("/peer/remove", {"peer_id": peer_id})

    async def get_vps_list(self) -> dict:
        return await self._get("/vps/list")

    async def diagnose(self, device: str) -> dict:
        return await self._post(f"/diagnose/{device}")

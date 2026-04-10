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

    def _session_kwargs(self, timeout: int) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        return {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=timeout),
        }

    async def close(self) -> None:
        # Backward-compatible no-op: requests use short-lived sessions.
        return None

    async def _get(self, path: str, timeout: int = 15) -> Any:
        try:
            async with aiohttp.ClientSession(**self._session_kwargs(timeout)) as session:
                async with session.get(f"{self._base_url}{path}") as r:
                    if r.status == 200:
                        ct = r.headers.get("Content-Type", "")
                        if "json" in ct:
                            return await r.json()
                        return await r.read()
                    raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
        except aiohttp.ClientError:
            raise WatchdogError("Watchdog недоступен") from None

    async def _post(self, path: str, data: Optional[dict] = None, timeout: int = 30) -> Any:
        try:
            async with aiohttp.ClientSession(**self._session_kwargs(timeout)) as session:
                async with session.post(
                    f"{self._base_url}{path}",
                    json=data or {},
                ) as r:
                    if r.status in (200, 202):
                        ct = r.headers.get("Content-Type", "")
                        if "json" in ct:
                            return await r.json()
                        return await r.read()
                    raise WatchdogError(f"HTTP {r.status}: {await r.text()}")
        except aiohttp.ClientError:
            raise WatchdogError("Watchdog недоступен") from None

    async def post(self, path: str, data: Optional[dict] = None, timeout: int = 30) -> Any:
        """Public wrapper for ad-hoc POST endpoints used by admin handlers."""
        path = path if path.startswith("/") else f"/{path}"
        return await self._post(path, data, timeout)

    @staticmethod
    def _normalize_decision_runtime_status(payload: Any) -> dict[str, Any]:
        data = dict(payload or {})
        data["desired_backend_path"] = dict(data.get("desired_backend_path") or {})
        data["applied_backend_path"] = dict(data.get("applied_backend_path") or {})
        data["backend_path_status"] = dict(data.get("backend_path_status") or {})
        data["backend_paths"] = list(data.get("backend_paths") or [])
        data["decision"] = dict(data.get("decision") or {})
        data["runtime"] = dict(data.get("runtime") or {})
        if "tier2_health" in data:
            data["tier2_health"] = dict(data.get("tier2_health") or {})
        return data

    # -----------------------------------------------------------------------
    # Status / metrics
    # -----------------------------------------------------------------------
    async def get_status(self) -> dict:
        return await self._get("/status")

    async def get_health(self) -> dict:
        return await self._get("/health")

    async def get_functional_status(self) -> dict:
        return await self._get("/functional/status")

    async def run_functional(self, tier: str = "standard") -> dict:
        return await self._post("/functional/run", {"tier": tier}, timeout=90)

    async def set_functional_mode(self, mode: str) -> dict:
        return await self._post("/functional/mode", {"mode": mode}, timeout=60)

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
    async def deploy(self, force: bool = False) -> dict:
        return await self._post("/deploy", {"force": force})

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

    async def get_backends(self) -> dict:
        return await self._get("/decision/backends")

    async def get_decision_runtime_status(self) -> dict:
        return self._normalize_decision_runtime_status(await self._get("/decision/status"))

    async def get_balancer_status(self) -> dict:
        return await self.get_decision_runtime_status()

    async def get_backend_paths(self) -> dict:
        return self._normalize_decision_runtime_status(await self._get("/decision/backend-paths"))

    async def choose_backend(self, backend_id: str = "", route_class: str = "vpn_default", mode: str = "auto") -> dict:
        return await self._post(
            "/decision/choose-backend",
            {"backend_id": backend_id, "route_class": route_class, "mode": mode},
        )

    async def apply_backend_decision(self, backend_id: str, reason: str = "decision_apply", decision_source: str = "decision_api") -> dict:
        return await self._post(
            "/decision/apply-backend",
            {"backend_id": backend_id, "reason": reason, "decision_source": decision_source},
            timeout=90,
        )

    async def switch_backend(self, backend_id: str) -> dict:
        return await self._post("/balancer/switch", {"backend_id": backend_id}, timeout=90)

    async def auto_select_backend(self) -> dict:
        return await self._post("/balancer/auto-select", timeout=90)

    async def add_vps(self, ip: str, ssh_port: int = 443, tunnel_ip: str = "") -> dict:
        return await self._post("/vps/add", {"ip": ip, "ssh_port": ssh_port, "tunnel_ip": tunnel_ip})

    async def add_backend(self, ip: str, ssh_port: int = 443, tunnel_ip: str = "", weight: int = 100) -> dict:
        return await self._post(
            "/backends/add",
            {"ip": ip, "ssh_port": ssh_port, "tunnel_ip": tunnel_ip, "weight": weight},
        )

    async def install_vps(self, ip: str, password: str, ssh_port: int = 22) -> dict:
        """Запустить полную установку VPS через add-vps.sh (202 Accepted, прогресс в Telegram)."""
        return await self._post(
            "/vps/install",
            {"ip": ip, "password": password, "ssh_port": ssh_port},
            timeout=15,
        )

    async def remove_vps(self, ip: str) -> dict:
        return await self._post("/vps/remove", {"ip": ip})

    async def remove_backend(self, ip: str) -> dict:
        return await self._post("/backends/remove", {"ip": ip})

    async def drain_backend(self, backend_id: str) -> dict:
        return await self._post("/backends/drain", {"id": backend_id})

    async def undrain_backend(self, backend_id: str) -> dict:
        return await self._post("/backends/undrain", {"id": backend_id})

    async def rebalance(self, route_class: str = "all") -> dict:
        return await self._post("/decision/reassign", {"route_class": route_class})

    async def reconcile_assignments(self, backend_id: str = "") -> dict:
        payload = {}
        if backend_id:
            payload["backend_id"] = backend_id
        return await self._post("/decision/reconcile-assignments", payload)

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

    async def check_domain(self, domain: str, chat_id: str = "", source_ip: str = "") -> dict:
        payload = {"domain": domain}
        if chat_id:
            payload["chat_id"] = chat_id
        if source_ip:
            payload["source_ip"] = source_ip
        return await self._post("/check", payload)

    async def resolve_domain_decision(self, domain: str, chat_id: str = "", source_ip: str = "") -> dict:
        payload = {"domain": domain}
        if chat_id:
            payload["chat_id"] = chat_id
        if source_ip:
            payload["source_ip"] = source_ip
        return await self._post("/decision/resolve-domain", payload)

    async def explain_domain_decision(self, domain: str, chat_id: str = "", source_ip: str = "") -> dict:
        return await self.resolve_domain_decision(domain, chat_id=chat_id, source_ip=source_ip)

    async def get_latency_learning(self) -> dict:
        return await self._get("/latency/learning")

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

    async def backup_export(self) -> dict:
        """POST /backup/export — полный экспорт с mTLS CA."""
        return await self._post("/backup/export")

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

    async def get_gateway_lan_clients(self) -> dict:
        return await self._get("/gateway/lan-clients")

    async def get_gateway_lan_prefs(self) -> dict:
        return await self._get("/gateway/lan-prefs")

    async def upsert_gateway_lan_client(self, name: str, src_ip: str, client_id: str = "", enabled: bool = True) -> dict:
        payload = {"name": name, "src_ip": src_ip, "enabled": enabled}
        if client_id:
            payload["id"] = client_id
        return await self._post("/gateway/lan-client/upsert", payload)

    async def remove_gateway_lan_client(self, client_id: str) -> dict:
        return await self._post("/gateway/lan-client/remove", {"id": client_id})

    async def add_gateway_lan_pref(self, lan_client_id: str, match_type: str, match_value: str, backend_id: str, enabled: bool = True) -> dict:
        return await self._post(
            "/gateway/lan-pref/add",
            {
                "lan_client_id": lan_client_id,
                "match_type": match_type,
                "match_value": match_value,
                "backend_id": backend_id,
                "enabled": enabled,
            },
        )

    async def remove_gateway_lan_pref(self, pref_id: str) -> dict:
        return await self._post("/gateway/lan-pref/remove", {"id": pref_id})

    async def dpi_test(self, domains: list | None = None) -> dict:
        return await self._post("/dpi/test", {"domains": domains}, timeout=30)

    async def zapret_probe(self, mode: str = "quick") -> dict:
        return await self._post("/zapret/probe", {"mode": mode})

    async def get_zapret_history(self) -> dict:
        return await self._get("/zapret/history")

#!/usr/bin/env python3
import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "watchdog" / "watchdog.py"


class _DummyFastAPI:
    def __init__(self, *args, **kwargs) -> None:
        self.state = types.SimpleNamespace()

    def get(self, *args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator

    def post(self, *args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator

    def add_exception_handler(self, *args, **kwargs) -> None:
        return None

    def on_event(self, *args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator


class _DummyLimiter:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def limit(self, *args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator


class _DummyHTTPBearer:
    def __init__(self, *args, **kwargs) -> None:
        pass


def _install_watchdog_import_stubs() -> None:
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _DummyFastAPI
    fastapi.HTTPException = Exception
    fastapi.Request = object
    fastapi.BackgroundTasks = object
    fastapi.Depends = lambda value=None: value
    sys.modules.setdefault("fastapi", fastapi)

    responses = types.ModuleType("fastapi.responses")
    responses.Response = object
    sys.modules.setdefault("fastapi.responses", responses)

    security = types.ModuleType("fastapi.security")
    security.HTTPAuthorizationCredentials = object
    security.HTTPBearer = _DummyHTTPBearer
    sys.modules.setdefault("fastapi.security", security)

    slowapi = types.ModuleType("slowapi")
    slowapi.Limiter = _DummyLimiter
    slowapi._rate_limit_exceeded_handler = lambda *args, **kwargs: None
    sys.modules.setdefault("slowapi", slowapi)

    slowapi_errors = types.ModuleType("slowapi.errors")
    slowapi_errors.RateLimitExceeded = Exception
    sys.modules.setdefault("slowapi.errors", slowapi_errors)

    slowapi_util = types.ModuleType("slowapi.util")
    slowapi_util.get_remote_address = lambda *args, **kwargs: "127.0.0.1"
    sys.modules.setdefault("slowapi.util", slowapi_util)

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    sys.modules.setdefault("pydantic", pydantic)

    for name in ("aiohttp", "psutil", "uvicorn"):
        sys.modules.setdefault(name, types.ModuleType(name))


_install_watchdog_import_stubs()
SPEC = importlib.util.spec_from_file_location("watchdog_module", MODULE_PATH)
watchdog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(watchdog)


class FunctionalHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._save_patcher = mock.patch.object(watchdog.state, "save", return_value=None)
        self._save_patcher.start()
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_STAGED
        watchdog.state.functional_execution_status = watchdog.FUNCTIONAL_EXEC_DISABLED
        watchdog.state.functional_execution_last_error = ""
        watchdog.state.functional_execution_auto_disabled_reason = ""
        watchdog.state.functional_results = {}
        watchdog.state.functional_summary = {}
        watchdog.state.functional_evidence_store = {}
        watchdog.state.functional_infra_checks = []
        watchdog.state.last_functional_run_by_tier = {}
        watchdog.state.responsiveness_summary = {}
        watchdog.state.latency_learning_last_apply_ts = 0.0
        watchdog.state.latency_catalog_alert_last_ts = 0.0

    def tearDown(self) -> None:
        self._save_patcher.stop()

    def test_latency_catalog_status_prefers_runtime_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            runtime = tmp / "latency-catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "services": {
                            "yandex": {
                                "display": "Yandex",
                                "domains": {"core": ["yandex.ru"]},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text('{"services":{}}', encoding="utf-8")
            with mock.patch.object(watchdog, "LATENCY_CATALOG_FILE", runtime):
                with mock.patch.object(watchdog, "LATENCY_CATALOG_FALLBACKS", [fallback]):
                    info = watchdog._latency_catalog_status()
        self.assertEqual(info["source"], "runtime")
        self.assertEqual(info["service_count"], 1)
        self.assertFalse(info["empty"])

    def test_stack_order_marks_tuic_and_trojan_after_production_stacks(self) -> None:
        self.assertIn("tuic", watchdog.STACK_ORDER)
        self.assertIn("trojan", watchdog.STACK_ORDER)
        self.assertGreater(watchdog.STACK_ORDER.index("trojan"), watchdog.STACK_ORDER.index("hysteria2"))
        self.assertGreater(watchdog.STACK_ORDER.index("tuic"), watchdog.STACK_ORDER.index("hysteria2"))
        self.assertEqual(watchdog.DEFAULT_STACK, "cloudflare-cdn" if watchdog._cloudflare_cdn_enabled() else "hysteria2")

    def test_backend_pool_is_derived_from_vps_list(self) -> None:
        watchdog.state.vps_list = [
            {"ip": "198.51.100.10", "ssh_port": 443, "tunnel_ip": "10.177.2.2"},
            {"ip": "203.0.113.20", "ssh_port": 22, "tunnel_ip": "10.177.2.6", "drain": True},
        ]
        watchdog.state.active_vps_idx = 0
        watchdog.state.backends = []
        watchdog.state.backend_assignments = {}
        watchdog._refresh_backend_pool()
        self.assertEqual(len(watchdog.state.backends), 2)
        self.assertEqual(watchdog.state.backends[0]["id"], "backend-198-51-100-10")
        self.assertTrue(any(item["drain"] for item in watchdog.state.backends))

    def test_backend_assignment_creates_ttl_lease(self) -> None:
        watchdog.state.vps_list = [{"ip": "198.51.100.10", "ssh_port": 443, "tunnel_ip": "10.177.2.2"}]
        watchdog.state.active_vps_idx = 0
        watchdog.state.active_backend_id = ""
        watchdog.state.backends = []
        watchdog.state.backend_assignments = {}
        watchdog.state.balancer_idle_ttl_seconds = 300
        with mock.patch.object(watchdog.state, "save", return_value=None):
            assignment = watchdog._ensure_backend_assignment("blocked_default")
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertEqual(assignment["route_class"], "blocked_default")
        self.assertEqual(assignment["backend_id"], "backend-198-51-100-10")
        self.assertGreater(assignment["expires_at_ts"], assignment["assigned_at_ts"])

    def test_run_quick_refreshes_runtime_sync_state_without_selfheal(self) -> None:
        watchdog.state.docker_health = {}
        watchdog.state.bot_runtime_drift = True
        watchdog.state.compose_runtime_drift = True
        watchdog.state.watchdog_runtime_drift = True
        watchdog.state.server_repo_drift = False
        call_order: list[str] = []

        async def _fake_check_containers() -> None:
            call_order.append("containers")
            watchdog.state.docker_health["telegram-bot"] = 1

        async def _fake_check_bot_sync(*, allow_selfheal: bool = True) -> None:
            call_order.append(f"bot:{allow_selfheal}")
            watchdog.state.bot_runtime_drift = False

        async def _fake_check_compose_sync(*, allow_selfheal: bool = True) -> None:
            call_order.append(f"compose:{allow_selfheal}")
            watchdog.state.compose_runtime_drift = False

        async def _fake_check_watchdog_sync(*, allow_selfheal: bool = True) -> None:
            call_order.append(f"watchdog:{allow_selfheal}")
            watchdog.state.watchdog_runtime_drift = False

        async def _fake_check_repo_sync() -> None:
            call_order.append("repo")
            watchdog.state.server_repo_drift = False

        with mock.patch.object(watchdog, "check_containers", side_effect=_fake_check_containers):
            with mock.patch.object(watchdog, "check_telegram_bot_runtime_sync", side_effect=_fake_check_bot_sync):
                with mock.patch.object(watchdog, "check_compose_runtime_sync", side_effect=_fake_check_compose_sync):
                    with mock.patch.object(watchdog, "check_watchdog_runtime_sync", side_effect=_fake_check_watchdog_sync):
                        with mock.patch.object(watchdog, "check_server_repo_sync", side_effect=_fake_check_repo_sync):
                            report = asyncio.run(watchdog.health_checker.run_quick())

        self.assertEqual(call_order, ["containers", "bot:False", "compose:False", "watchdog:False", "repo"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(checks["telegram_bot_runtime_sync"]["status"], "ok")
        self.assertEqual(checks["compose_runtime_sync"]["status"], "ok")
        self.assertEqual(checks["watchdog_runtime_sync"]["status"], "ok")

    def test_backend_path_snapshot_reports_hysteria2_desired_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            hysteria_dir = tmp / "etc" / "hysteria"
            backends_dir = hysteria_dir / "backends"
            backends_dir.mkdir(parents=True)
            active_config = hysteria_dir / "config.yaml"
            candidate_config = backends_dir / "backend-198-51-100-10.yaml"
            rendered = "server: 198.51.100.10:443\nsocks5:\n  listen: 127.0.0.1:1083\n"
            active_config.write_text(rendered, encoding="utf-8")
            candidate_config.write_text(rendered, encoding="utf-8")
            watchdog.state.backends = [
                {"id": "backend-198-51-100-10", "ip": "198.51.100.10", "drain": False, "status": "healthy"},
            ]
            watchdog.state.active_backend_id = "backend-198-51-100-10"
            watchdog.state.backend_assignments = {
                "blocked_default": {
                    "route_class": "blocked_default",
                    "backend_id": "backend-198-51-100-10",
                    "assigned_at_ts": 1.0,
                    "last_activity_ts": 1.0,
                    "expires_at_ts": 301.0,
                }
            }
            with mock.patch.object(watchdog, "_hysteria_backend_config_path", side_effect=lambda backend_id: backends_dir / f"{backend_id}.yaml"):
                with mock.patch.object(watchdog, "_hysteria_active_config_path", return_value=active_config):
                    rows = watchdog._backend_path_snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["family"], "hysteria2")
        self.assertTrue(rows[0]["desired"])
        self.assertTrue(rows[0]["applied"])
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(rows[0]["route_classes"], ["blocked_default"])

    def test_backend_path_snapshot_reports_verified_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            hysteria_dir = tmp / "etc" / "hysteria"
            backends_dir = hysteria_dir / "backends"
            backends_dir.mkdir(parents=True)
            active_config = hysteria_dir / "config.yaml"
            candidate_config = backends_dir / "backend-198-51-100-10.yaml"
            rendered = "server: 198.51.100.10:443\nsocks5:\n  listen: 127.0.0.1:1083\n"
            active_config.write_text(rendered, encoding="utf-8")
            candidate_config.write_text(rendered, encoding="utf-8")
            watchdog.state.backends = [
                {"id": "backend-198-51-100-10", "ip": "198.51.100.10", "drain": False, "status": "healthy"},
            ]
            watchdog.state.active_backend_id = "backend-198-51-100-10"
            watchdog.state.desired_backend_path = {"family": "hysteria2", "backend_id": "backend-198-51-100-10"}
            watchdog.state.applied_backend_path = {"family": "hysteria2", "backend_id": "backend-198-51-100-10"}
            watchdog.state.backend_path_health = {
                "backend-198-51-100-10": {
                    "family": "hysteria2",
                    "backend_id": "backend-198-51-100-10",
                    "verified": True,
                    "verify_reason": "",
                    "verified_at_ts": 123.0,
                    "http_code": "204",
                }
            }
            with mock.patch.object(watchdog, "_hysteria_backend_config_path", side_effect=lambda backend_id: backends_dir / f"{backend_id}.yaml"):
                with mock.patch.object(watchdog, "_hysteria_active_config_path", return_value=active_config):
                    rows = watchdog._backend_path_snapshot()
        self.assertTrue(rows[0]["verified"])
        self.assertEqual(rows[0]["http_code"], "204")
        self.assertEqual(rows[0]["verified_at_ts"], 123.0)

    def test_functional_preflight_accepts_ssh_socket_activation(self) -> None:
        async def _fake_run_cmd(cmd, timeout=0):
            if cmd[:3] == ["systemctl", "is-active", "dnsmasq"]:
                return 0, "active\n", ""
            if cmd[:3] == ["systemctl", "is-active", "nftables"]:
                return 0, "active\n", ""
            if cmd[:3] == ["systemctl", "is-active", "vpn-routes"]:
                return 0, "active\n", ""
            if cmd[:3] == ["systemctl", "is-active", "ssh"]:
                return 3, "inactive\n", ""
            if cmd[:3] == ["systemctl", "is-active", "ssh.socket"]:
                return 0, "active\n", ""
            if cmd[:2] == ["ss", "-ltn"]:
                return 0, "LISTEN 0 128 *:22 *:*", ""
            return 1, "", "unexpected"

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "functional-scenarios.yaml"
            manifest.write_text("scenarios: []\n", encoding="utf-8")
            with mock.patch.object(watchdog, "_functional_manifest_path", return_value=manifest):
                with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
                    checks = asyncio.run(watchdog._functional_preflight_checks())

        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["systemd:ssh"]["status"], "ok")
        self.assertIn("service=inactive; socket=active", by_name["systemd:ssh"]["detail"])

    def test_ensure_functional_bridge_refreshes_table_100_route(self) -> None:
        calls: list[list[str]] = []

        async def _fake_run_cmd(cmd, timeout=0):
            calls.append(cmd)
            if cmd[:4] == ["ip", "link", "show", watchdog.FUNCTIONAL_BRIDGE]:
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
            asyncio.run(watchdog._ensure_functional_bridge())

        self.assertIn(
            ["ip", "route", "replace", watchdog.FUNCTIONAL_BRIDGE_CIDR, "dev", watchdog.FUNCTIONAL_BRIDGE, "table", watchdog.FUNCTIONAL_ROUTE_TABLE],
            calls,
        )

    def test_health_alert_wraps_failed_check_names_in_code(self) -> None:
        report = {
            "score": 67.9,
            "status": "degraded",
            "checks": [
                {"name": "functional_lan_direct_internet", "status": "fail"},
                {"name": "dns_responding", "status": "ok"},
            ],
        }
        checker = watchdog.HealthChecker()
        checker._alert_dedup_ts = 0.0
        with mock.patch.object(watchdog, "alert") as mock_alert:
            with mock.patch("time.time", return_value=2000.0):
                checker._maybe_alert(report)

        mock_alert.assert_called_once()
        payload = mock_alert.call_args.args[0]
        self.assertIn("`functional_lan_direct_internet`", payload)
        self.assertIn("Health Score: 68/100", payload)

    def test_reconcile_backend_path_runtime_state_sets_hysteria2_active_backend(self) -> None:
        watchdog.state.backends = [{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy"}]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.execution_mode = "multi_backend"
        watchdog.state.execution_family = "hysteria2"
        watchdog.state.active_stack = "hysteria2"
        watchdog.state.desired_backend_path = {}
        watchdog.state.applied_backend_path = {}
        with mock.patch.object(watchdog, "_refresh_backend_pool", return_value=None):
            with mock.patch.object(watchdog.state, "save", return_value=None):
                changed = watchdog._reconcile_backend_path_runtime_state()
        self.assertTrue(changed)
        self.assertEqual(watchdog.state.desired_backend_path["backend_id"], "backend-a")
        self.assertEqual(watchdog.state.desired_backend_path["reason"], "startup_reconcile")
        self.assertEqual(watchdog.state.desired_backend_path["execution_mode"], "multi_backend")
        self.assertEqual(watchdog.state.applied_backend_path["backend_id"], "backend-a")
        self.assertEqual(watchdog.state.applied_backend_path["reason"], "startup_reconcile")
        self.assertEqual(watchdog.state.applied_backend_path["execution_mode"], "multi_backend")

    def test_reconcile_backend_path_runtime_state_normalizes_execution_mode_mismatch(self) -> None:
        watchdog.state.backends = [{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy"}]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.execution_mode = "multi_backend"
        watchdog.state.execution_family = "hysteria2"
        watchdog.state.active_stack = "hysteria2"
        watchdog.state.desired_backend_path = {"backend_id": "backend-a", "family": "hysteria2", "execution_mode": "single_active_backend"}
        watchdog.state.applied_backend_path = {"backend_id": "backend-a", "family": "hysteria2", "execution_mode": "single_active_backend"}
        with mock.patch.object(watchdog, "_refresh_backend_pool", return_value=None):
            with mock.patch.object(watchdog.state, "save", return_value=None):
                changed = watchdog._reconcile_backend_path_runtime_state()
        self.assertTrue(changed)
        self.assertEqual(watchdog.state.desired_backend_path["execution_mode"], "multi_backend")
        self.assertEqual(watchdog.state.applied_backend_path["execution_mode"], "multi_backend")

    def test_reconcile_backend_path_runtime_state_preserves_pending_desired_backend(self) -> None:
        watchdog.state.backends = [{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy"}]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.execution_mode = "multi_backend"
        watchdog.state.execution_family = "hysteria2"
        watchdog.state.active_stack = "hysteria2"
        watchdog.state.desired_backend_path = {"backend_id": "backend-pending", "family": "hysteria2", "execution_mode": "multi_backend"}
        watchdog.state.applied_backend_path = {}
        with mock.patch.object(watchdog, "_refresh_backend_pool", return_value=None):
            with mock.patch.object(watchdog.state, "save", return_value=None):
                changed = watchdog._reconcile_backend_path_runtime_state()
        self.assertTrue(changed)
        self.assertEqual(watchdog.state.desired_backend_path["backend_id"], "backend-pending")
        self.assertEqual(watchdog.state.applied_backend_path["backend_id"], "backend-a")

    def test_reconcile_backend_path_runtime_state_skips_non_hysteria_execution_family(self) -> None:
        watchdog.state.backends = [{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy"}]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.execution_family = "tuic"
        watchdog.state.active_stack = "hysteria2"
        watchdog.state.desired_backend_path = {}
        watchdog.state.applied_backend_path = {}
        with mock.patch.object(watchdog, "_refresh_backend_pool", return_value=None):
            changed = watchdog._reconcile_backend_path_runtime_state()
        self.assertFalse(changed)
        self.assertEqual(watchdog.state.desired_backend_path, {})
        self.assertEqual(watchdog.state.applied_backend_path, {})

    def test_functional_run_recovers_from_auto_disabled_after_preflight_ok(self) -> None:
        scenario = SimpleNamespace(id="dummy", enabled=True, tiers={"quick"}, routing_expectation="mixed")
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        watchdog.state.functional_execution_status = watchdog.FUNCTIONAL_EXEC_AUTO_DISABLED
        watchdog.state.functional_execution_last_error = "systemd:ssh=inactive"
        watchdog.state.functional_execution_auto_disabled_reason = "preflight_failed"
        watchdog.state.functional_results = {"__execution__": {"status": "fail"}}
        watchdog.state.functional_evidence_store = {"__execution__": {"status": "fail"}}

        async def _fake_preflight():
            return [
                {"name": "manifest", "status": "ok", "detail": "/tmp/functional-scenarios.yaml"},
                {"name": "systemd:ssh", "status": "ok", "detail": "service=active; socket=active"},
            ]

        async def _fake_run_scenario(_scenario):
            result = watchdog.CheckResult("functional_dummy", "ok", "ok", weight=1, tier="functional")
            evidence = {
                "id": "dummy",
                "status": "ok",
                "detail": "ok",
                "timestamp": "2026-04-04T21:00:00",
            }
            return result, evidence

        with mock.patch.object(watchdog, "_functional_preflight_checks", side_effect=_fake_preflight):
            with mock.patch.object(watchdog, "load_functional_scenarios", return_value=[scenario]):
                with mock.patch.object(watchdog, "_run_functional_scenario", side_effect=_fake_run_scenario):
                    with mock.patch.object(watchdog.state, "save", return_value=None):
                        results = asyncio.run(watchdog._run_functional_checks_for_tier("quick"))

        self.assertEqual([result.status for result in results], ["ok"])
        self.assertEqual(watchdog.state.functional_execution_status, watchdog.FUNCTIONAL_EXEC_HEALTHY)
        self.assertEqual(watchdog.state.functional_execution_last_error, "")
        self.assertEqual(watchdog.state.functional_execution_auto_disabled_reason, "")
        self.assertEqual(watchdog.state.functional_results["dummy"]["status"], "ok")

    def test_balancer_snapshot_reports_backend_path_reconciliation_status(self) -> None:
        watchdog.state.backends = [
            {"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy"},
        ]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.desired_backend_path = {
            "backend_id": "backend-a",
            "family": "hysteria2",
            "execution_mode": "single_active_backend",
            "route_classes": ["blocked_default"],
        }
        watchdog.state.applied_backend_path = {
            "backend_id": "backend-a",
            "family": "hysteria2",
            "execution_mode": "single_active_backend",
            "route_classes": ["blocked_default"],
        }
        watchdog.state.backend_path_health = {
            "backend-a": {
                "family": "hysteria2",
                "backend_id": "backend-a",
                "verified": True,
                "verify_reason": "",
                "verified_at_ts": 123.0,
                "http_code": "204",
            }
        }
        with mock.patch.object(watchdog, "_backend_path_snapshot", return_value=[{"backend_id": "backend-a", "verified": True}]):
            with mock.patch.object(watchdog, "_refresh_backend_pool", return_value=None):
                snapshot = watchdog._balancer_snapshot()
        self.assertEqual(
            snapshot["backend_path_status"],
            {
                "execution_mode": "multi_backend",
                "execution_family": "hysteria2",
                "desired_backend_id": "backend-a",
                "applied_backend_id": "backend-a",
                "active_backend_id": "backend-a",
                "desired_matches_applied": True,
                "applied_matches_active": True,
                "reconciled": True,
                "verified": True,
            },
        )

    def test_backend_path_state_uses_normalized_target_shape(self) -> None:
        watchdog.state.backend_assignments = {
            "vpn_default": {"route_class": "vpn_default", "backend_id": "backend-a", "assigned_at_ts": 1, "last_activity_ts": 1, "expires_at_ts": 2},
            "blocked_default": {"route_class": "blocked_default", "backend_id": "backend-a", "assigned_at_ts": 1, "last_activity_ts": 1, "expires_at_ts": 2},
        }
        with mock.patch.object(watchdog.state, "save", return_value=None):
            watchdog._set_desired_backend_path("backend-a", "manual_switch")
            watchdog._set_applied_backend_path("backend-a", "manual_switch")
        self.assertEqual(
            watchdog.state.desired_backend_path,
            {
                "backend_id": "backend-a",
                "family": "hysteria2",
                "execution_mode": "multi_backend",
                "route_classes": ["blocked_default", "vpn_default"],
                "reason": "manual_switch",
                "updated_at_ts": watchdog.state.desired_backend_path["updated_at_ts"],
            },
        )
        self.assertEqual(
            watchdog.state.applied_backend_path,
            {
                "backend_id": "backend-a",
                "family": "hysteria2",
                "execution_mode": "multi_backend",
                "route_classes": ["blocked_default", "vpn_default"],
                "reason": "manual_switch",
                "updated_at_ts": watchdog.state.applied_backend_path["updated_at_ts"],
            },
        )

    def test_verify_hysteria2_backend_path_accepts_rendered_and_live_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            hysteria_dir = tmp / "etc" / "hysteria"
            backends_dir = hysteria_dir / "backends"
            backends_dir.mkdir(parents=True)
            active_config = hysteria_dir / "config.yaml"
            candidate_config = backends_dir / "backend-198-51-100-10.yaml"
            rendered = "server: 198.51.100.10:443\nsocks5:\n  listen: 127.0.0.1:1083\n"
            active_config.write_text(rendered, encoding="utf-8")
            candidate_config.write_text(rendered, encoding="utf-8")
            watchdog.state.backends = [
                {"id": "backend-198-51-100-10", "ip": "198.51.100.10", "drain": False, "status": "healthy"},
            ]
            watchdog.state.active_backend_id = "backend-198-51-100-10"

            async def _fake_run_cmd(cmd, timeout=30):
                if cmd[:2] == ["systemctl", "is-active"]:
                    return 0, "active", ""
                if cmd[:2] == ["nc", "-z"]:
                    return 0, "", ""
                if cmd and cmd[0] == "curl":
                    if cmd[-1] == "http://www.gstatic.com/generate_204":
                        return 0, "204", ""
                    if cmd[-1] == "https://api.telegram.org":
                        return 0, "404", ""
                    return 1, "", "unexpected-url"
                return 0, "", ""

            with mock.patch.object(watchdog, "_hysteria_backend_config_path", side_effect=lambda backend_id: backends_dir / f"{backend_id}.yaml"):
                with mock.patch.object(watchdog, "_hysteria_active_config_path", return_value=active_config):
                    with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
                        result = asyncio.run(watchdog._verify_hysteria2_backend_path("backend-198-51-100-10"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["backend_id"], "backend-198-51-100-10")
        self.assertEqual(len(result["probes"]), 2)

    def test_apply_backend_decision_rolls_back_after_failed_verify(self) -> None:
        watchdog.state.active_backend_id = "backend-old"
        watchdog.state.backend_assignments = {}
        watchdog.state.balancer_idle_ttl_seconds = 300
        watchdog.state.desired_backend_path = {}
        watchdog.state.applied_backend_path = {"family": "hysteria2", "backend_id": "backend-old", "reason": "existing"}

        async def _fake_apply_active_backend(backend_id, reason):
            watchdog.state.desired_backend_path = {"family": "hysteria2", "backend_id": backend_id, "reason": reason}
            return {"backend_id": backend_id, "reason": reason}

        verify_results = [
            {"ok": False, "reason": "hysteria2_probe_failed", "backend_id": "backend-new"},
            {"ok": True, "backend_id": "backend-old"},
        ]

        async def _fake_verify(backend_id):
            return verify_results.pop(0)

        with mock.patch.object(watchdog, "_apply_active_backend", side_effect=_fake_apply_active_backend):
            with mock.patch.object(watchdog, "_verify_hysteria2_backend_path", side_effect=_fake_verify):
                result = asyncio.run(
                    watchdog._apply_backend_decision(
                        {"ok": True, "backend_id": "backend-new", "reason": "manual_switch"}
                    )
                )
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["rollback"]["status"], "rollback_completed")
        self.assertEqual(watchdog.state.active_backend_id, "backend-old")
        self.assertEqual(watchdog.state.desired_backend_path["backend_id"], "backend-new")
        self.assertEqual(watchdog.state.applied_backend_path["backend_id"], "backend-old")

    def test_apply_backend_decision_records_desired_applied_and_verify_state(self) -> None:
        watchdog.state.active_backend_id = "backend-old"
        watchdog.state.execution_family = "hysteria2"
        watchdog.state.desired_backend_path = {}
        watchdog.state.applied_backend_path = {}
        watchdog.state.backend_path_health = {}

        async def _fake_apply_active_backend(backend_id, reason):
            watchdog.state.desired_backend_path = {"family": "hysteria2", "backend_id": backend_id, "reason": reason}
            return {"backend_id": backend_id, "reason": reason}

        async def _fake_verify(backend_id):
            return {"ok": True, "backend_id": backend_id, "http_code": "204"}

        with mock.patch.object(watchdog, "_apply_active_backend", side_effect=_fake_apply_active_backend):
            with mock.patch.object(watchdog, "_verify_hysteria2_backend_path", side_effect=_fake_verify):
                result = asyncio.run(
                    watchdog._apply_backend_decision(
                        {"ok": True, "backend_id": "backend-new", "reason": "manual_switch"}
                    )
                )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(watchdog.state.desired_backend_path["backend_id"], "backend-new")
        self.assertEqual(watchdog.state.applied_backend_path["backend_id"], "backend-new")
        self.assertTrue(watchdog.state.backend_path_health["backend-new"]["verified"])

    def test_refresh_backend_pool_tracks_active_backend_id(self) -> None:
        watchdog.state.vps_list = [{"ip": "198.51.100.10", "ssh_port": 443, "tunnel_ip": "10.177.2.2"}]
        watchdog.state.active_vps_idx = 0
        watchdog.state.active_backend_id = ""
        watchdog.state.backends = []
        watchdog._refresh_backend_pool()
        self.assertEqual(watchdog.state.active_backend_id, "backend-198-51-100-10")
        self.assertEqual((watchdog.state.active_backend or {}).get("ip"), "198.51.100.10")

    def test_active_backend_helpers_prefer_runtime_backend_values(self) -> None:
        watchdog.state.backends = [
            {"id": "backend-203-0-113-20", "ip": "203.0.113.20", "tunnel_ip": "10.177.2.6", "ssh_port": 2202}
        ]
        watchdog.state.active_backend_id = "backend-203-0-113-20"
        self.assertEqual(watchdog._active_backend_ip(), "203.0.113.20")
        self.assertEqual(watchdog._active_backend_tunnel_ip(), "10.177.2.6")
        self.assertEqual(watchdog._active_backend_ssh_port(), "2202")

    def test_speedtest_iperf_vps_reports_missing_tier2_tunnel(self) -> None:
        async def _fake_run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
            if cmd[:2] == ["systemctl", "is-active"]:
                return 0, "active", ""
            if cmd[:5] == ["ip", "-4", "addr", "show", "dev"]:
                return 1, "", "Device not found"
            return 0, "", ""

        with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
            watchdog.state.tier2_health = {}
            speed, reason = asyncio.run(watchdog.speedtest_iperf_vps())

        self.assertEqual(speed, 0.0)
        self.assertIn("tun0", reason or "")

    def test_speedtest_iperf_vps_reports_closed_iperf_port(self) -> None:
        async def _fake_run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
            if cmd[:2] == ["systemctl", "is-active"]:
                return 0, "active", ""
            if cmd[:5] == ["ip", "-4", "addr", "show", "dev"]:
                return 0, "tun0: <POINTOPOINT,UP>\n    inet 10.177.2.1/30", ""
            if cmd[:2] == ["ping", "-c"]:
                return 0, "1 packets transmitted, 1 received", ""
            if cmd[:3] == ["nc", "-z", "-w"]:
                return 1, "", "timed out"
            return 0, "", ""

        with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
            watchdog.state.tier2_health = {}
            speed, reason = asyncio.run(watchdog.speedtest_iperf_vps())

        self.assertEqual(speed, 0.0)
        self.assertIn("5201", reason or "")

    def test_balancer_snapshot_exposes_decision_and_runtime_sections(self) -> None:
        watchdog.state.backends = [{"id": "backend-a", "ip": "198.51.100.10", "status": "healthy", "drain": False}]
        watchdog.state.active_backend_id = "backend-a"
        watchdog.state.backend_assignments = {}
        watchdog.state.desired_backend_path = {"family": "hysteria2", "backend_id": "backend-b"}
        watchdog.state.applied_backend_path = {"family": "hysteria2", "backend_id": "backend-a"}
        with mock.patch.object(watchdog, "_backend_path_snapshot", return_value=[]):
            snapshot = watchdog._balancer_snapshot()
        self.assertEqual(snapshot["decision"]["desired_backend_path"]["backend_id"], "backend-b")
        self.assertEqual(snapshot["runtime"]["applied_backend_path"]["backend_id"], "backend-a")

    def test_refresh_tier2_health_classifies_service_down(self) -> None:
        async def _fake_run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
            if cmd[:2] == ["systemctl", "is-active"]:
                return 3, "inactive", ""
            return 0, "", ""

        watchdog.state.tier2_health = {}
        with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
            result = asyncio.run(watchdog._refresh_tier2_health(force=True))
        self.assertEqual(result["last_failure_reason"], "tier2_service_down")
        self.assertFalse(result["service_active"])

    def test_route_class_for_domain_check_prefers_service_assignment(self) -> None:
        result = {
            "verdict": "vpn",
            "latency_service_id": "telegram",
            "in_blocked_static": True,
            "in_blocked_dynamic": False,
            "in_manual_vpn": False,
        }
        self.assertEqual(watchdog._route_class_for_domain_check(result), "service:telegram")

    def test_latency_catalog_health_warns_on_missing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text(
                json.dumps({"services": {"okko": {"display": "Okko", "domains": {"core": ["okko.tv"]}}}}),
                encoding="utf-8",
            )
            runtime = tmp / "latency-catalog.json"
            with mock.patch.object(watchdog, "LATENCY_CATALOG_FILE", runtime):
                with mock.patch.object(watchdog, "LATENCY_CATALOG_FALLBACKS", [fallback]):
                    result = watchdog._hc_latency_catalog_freshness()
        self.assertEqual(result.status, "warn")
        self.assertIn("fallback", result.detail)

    def test_load_functional_scenarios_reads_manifest(self) -> None:
        manifest = """
scenarios:
  - id: lan_direct
    enabled: true
    tiers: [quick]
    client_path: lan
    routing_expectation: direct
    probe_type: https_status
    targets:
      - host: example.com
        url: https://example.com
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "functional-scenarios.yaml"
            manifest_path.write_text(manifest, encoding="utf-8")
            with mock.patch.object(watchdog, "FUNCTIONAL_SCENARIOS_CANDIDATES", [manifest_path]):
                scenarios = watchdog.load_functional_scenarios()

        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0].id, "lan_direct")
        self.assertEqual(scenarios[0].tiers, ["quick"])

    def test_duplicate_manifest_ids_raise(self) -> None:
        manifest = """
scenarios:
  - id: dup
    enabled: true
    tiers: [quick]
    client_path: lan
    routing_expectation: direct
    probe_type: dns_only
    targets: [{host: example.com}]
  - id: dup
    enabled: true
    tiers: [quick]
    client_path: lan
    routing_expectation: direct
    probe_type: dns_only
    targets: [{host: example.org}]
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "functional-scenarios.yaml"
            manifest_path.write_text(manifest, encoding="utf-8")
            with mock.patch.object(watchdog, "FUNCTIONAL_SCENARIOS_CANDIDATES", [manifest_path]):
                with self.assertRaises(ValueError):
                    watchdog.load_functional_scenarios()

    def test_cached_functional_results_affect_health_score(self) -> None:
        checker = watchdog.HealthChecker()
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        watchdog.state.functional_results = {
            "lan_direct_internet": {
                "status": "fail",
                "detail": "cached fail",
                "weight": 10,
            }
        }
        report = checker._compute(watchdog._cached_functional_check_results(), "quick")
        self.assertEqual(report["score"], 0.0)
        self.assertEqual(report["summary"]["fail"], 1)
        self.assertIn("functional_results", report)

    def test_dpi_experimental_scenario_skips_when_disabled(self) -> None:
        scenario = watchdog.FunctionalScenario(
            id="dpi_only",
            enabled=True,
            description="dpi",
            tiers=["quick"],
            client_path="lan",
            routing_expectation="dpi_experimental",
            probe_type="dns_only",
            targets=[{"host": "youtube.com"}],
        )
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        watchdog.state.dpi_enabled = False
        watchdog.state.dpi_experimental_opt_in = False
        with mock.patch.object(watchdog, "load_functional_scenarios", return_value=[scenario]):
            with mock.patch.object(watchdog, "_functional_preflight_checks", return_value=[]):
                results = asyncio.run(watchdog._run_functional_checks_for_tier("quick"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[0].weight, 0)

    def test_staged_mode_clears_results_and_does_not_run_scenarios(self) -> None:
        scenario = watchdog.FunctionalScenario(
            id="lan_direct",
            enabled=True,
            description="lan",
            tiers=["quick"],
            client_path="lan",
            routing_expectation="direct",
            probe_type="dns_only",
            targets=[{"host": "example.com"}],
        )
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_STAGED
        with mock.patch.object(watchdog, "load_functional_scenarios", return_value=[scenario]):
            with mock.patch.object(watchdog, "_functional_preflight_checks", return_value=[]):
                results = asyncio.run(watchdog._run_functional_checks_for_tier("quick"))

        self.assertEqual(results, [])
        self.assertEqual(watchdog.state.functional_summary.get("status"), "staged")
        self.assertEqual(watchdog.state.functional_results, {})

    def test_cached_results_ignored_when_not_active(self) -> None:
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_STAGED
        watchdog.state.functional_results = {
            "lan_direct_internet": {"status": "fail", "detail": "cached fail", "weight": 10}
        }
        self.assertEqual(watchdog._cached_functional_check_results(), [])

    def test_active_mode_preflight_failure_auto_disables_execution(self) -> None:
        scenario = watchdog.FunctionalScenario(
            id="lan_direct",
            enabled=True,
            description="lan",
            tiers=["quick"],
            client_path="lan",
            routing_expectation="direct",
            probe_type="dns_only",
            targets=[{"host": "example.com"}],
        )
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        checks = [watchdog._functional_infra_check("manifest", False, "missing")]
        with mock.patch.object(watchdog, "load_functional_scenarios", return_value=[scenario]):
            with mock.patch.object(watchdog, "_functional_preflight_checks", return_value=checks):
                results = asyncio.run(watchdog._run_functional_checks_for_tier("quick"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "functional_execution")
        self.assertEqual(results[0].status, "fail")
        self.assertEqual(watchdog.state.functional_execution_status, watchdog.FUNCTIONAL_EXEC_AUTO_DISABLED)
        self.assertEqual(watchdog.state.functional_results.get("__execution__", {}).get("status"), "fail")

    def test_active_mode_without_scenarios_fails_execution(self) -> None:
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        with mock.patch.object(watchdog, "load_functional_scenarios", return_value=[]):
            with mock.patch.object(watchdog, "_functional_preflight_checks", return_value=[]):
                results = asyncio.run(watchdog._run_functional_checks_for_tier("quick"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "fail")
        self.assertEqual(watchdog.state.functional_execution_status, watchdog.FUNCTIONAL_EXEC_AUTO_DISABLED)

    def test_build_responsiveness_summary_aggregates_timings(self) -> None:
        summary = watchdog._build_responsiveness_summary(
            scenario_results={
                "complex_service_okko_like": {"status": "ok"},
                "lan_blocked_via_vps": {"status": "fail"},
            },
            evidence_store={
                "complex_service_okko_like": {
                    "scenario_class": "complex_media_service",
                    "client_path": "lan",
                    "targets": [
                        {
                            "host": "okko.tv",
                            "path_ok": True,
                            "probe_result": {"timings": {"dns_s": 0.02, "ttfb_s": 0.15, "total_s": 0.4}},
                        }
                    ],
                },
                "lan_blocked_via_vps": {
                    "scenario_class": "blocked_baseline",
                    "client_path": "lan",
                    "targets": [
                        {
                            "host": "api.telegram.org",
                            "path_ok": False,
                            "probe_result": {"timings": {"dns_s": 0.03, "ttfb_s": 0.25, "total_s": 0.6}},
                        }
                    ],
                },
            },
            functional_status=watchdog.FUNCTIONAL_EXEC_HEALTHY,
            functional_mode=watchdog.FUNCTIONAL_MODE_ACTIVE,
        )

        self.assertEqual(summary["status"], "degraded")
        self.assertAlmostEqual(summary["dns_bootstrap_latency_ms_avg"], 25.0)
        self.assertAlmostEqual(summary["first_https_latency_ms_avg"], 200.0)
        self.assertIn("lan_blocked_via_vps", summary["slow_scenarios"])
        self.assertIn("lan_blocked_via_vps:api.telegram.org", summary["path_failures"])

    def test_record_latency_learning_observation_rejects_broad_google_domains(self) -> None:
        with mock.patch.object(watchdog, "_latency_manual_vpn_domains", return_value=set()):
            with mock.patch.object(watchdog, "_latency_manual_direct_domains", return_value=set()):
                with mock.patch.object(
                    watchdog,
                    "_match_latency_catalog_domain",
                    return_value={
                        "service_id": "okko",
                        "display": "Okko",
                        "category": "media",
                        "role": "cdn",
                        "parent_domain": "googleapis.com",
                        "auto_promote_allowed": True,
                        "requires_direct_bootstrap": True,
                    },
                ):
                    promoted = watchdog._record_latency_learning_observation(
                        "www.googleapis.com",
                        source="functional",
                        reason="test",
                        route_verdict="blocked_vps",
                    )

        self.assertFalse(promoted)

    def test_load_latency_catalog_sanitizes_broad_google_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            runtime = tmp / "latency-catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "services": {
                            "okko": {
                                "display": "Okko",
                                "category": "media",
                                "domains": {
                                    "cdn": ["www.googleapis.com", "googleapis.com", "gstatic.com", "clients-static.okko.tv"],
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text('{"services":{}}', encoding="utf-8")
            with mock.patch.object(watchdog, "LATENCY_CATALOG_FILE", runtime):
                with mock.patch.object(watchdog, "LATENCY_CATALOG_FALLBACKS", [fallback]):
                    catalog = watchdog._load_latency_catalog()

        self.assertEqual(catalog["okko"]["domains"]["cdn"], ["clients-static.okko.tv"])

    def test_functional_failover_trigger_reason_targets_blocked_and_control_plane_failures(self) -> None:
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        watchdog.state.functional_execution_status = watchdog.FUNCTIONAL_EXEC_HEALTHY
        watchdog.state.functional_summary = {"timestamp": "2026-04-07T07:30:00"}
        watchdog.state.responsiveness_summary = {
            "status": "degraded",
            "by_class": {
                "blocked_baseline": {"ok": 0, "fail": 1},
                "complex_media_service": {"ok": 0, "fail": 1},
            },
        }
        reason = watchdog._functional_failover_trigger_reason(
            now_ts=datetime.fromisoformat("2026-04-07T07:31:00").timestamp()
        )
        self.assertEqual(reason, "functional_blocked_baseline")

    def test_functional_failover_trigger_reason_ignores_stale_or_noncritical_results(self) -> None:
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_ACTIVE
        watchdog.state.functional_execution_status = watchdog.FUNCTIONAL_EXEC_HEALTHY
        watchdog.state.functional_summary = {"timestamp": "2026-04-07T07:20:00"}
        watchdog.state.responsiveness_summary = {
            "status": "degraded",
            "by_class": {
                "complex_media_service": {"ok": 0, "fail": 1},
            },
        }
        self.assertIsNone(
            watchdog._functional_failover_trigger_reason(
                now_ts=datetime.fromisoformat("2026-04-07T07:31:00").timestamp()
            )
        )

    def test_active_stack_runtime_failover_reason_targets_recent_probe_failures(self) -> None:
        watchdog.state.active_stack_runtime_fail_streak = watchdog.ACTIVE_STACK_RUNTIME_FAILOVER_CONSECUTIVE_FAILURES
        watchdog.state.last_failover = None
        watchdog.state.active_stack_probe_summary = {
            "status": "degraded",
            "timestamp": "2026-04-07T07:30:00",
            "recent_socks_errors": 0,
        }
        reason = watchdog._active_stack_runtime_failover_reason(
            now_ts=datetime.fromisoformat("2026-04-07T07:31:00").timestamp()
        )
        self.assertEqual(reason, "active_stack_runtime_probe")

    def test_active_stack_runtime_failover_reason_targets_hysteria2_socks_errors(self) -> None:
        watchdog.state.active_stack_runtime_fail_streak = 0
        watchdog.state.last_failover = None
        watchdog.state.active_stack_probe_summary = {
            "status": "degraded",
            "timestamp": "2026-04-07T07:30:00",
            "recent_socks_errors": watchdog.HYSTERIA2_SOCKS_ERROR_THRESHOLD,
        }
        reason = watchdog._active_stack_runtime_failover_reason(
            now_ts=datetime.fromisoformat("2026-04-07T07:31:00").timestamp()
        )
        self.assertEqual(reason, "hysteria2_socks_timeouts")

    def test_run_active_stack_runtime_probes_marks_degraded_on_recent_hysteria_errors(self) -> None:
        watchdog.state.active_stack = "hysteria2"
        watchdog.state.active_stack_runtime_fail_streak = 0
        watchdog.state.active_stack_probe_summary = {}

        async def _fake_run_cmd(cmd, timeout=0):
            if cmd[:2] == ["nc", "-z"]:
                return 0, "", ""
            if cmd[:2] == ["journalctl", "-u"]:
                return 0, "\n".join(["SOCKS5 TCP error"] * watchdog.HYSTERIA2_SOCKS_ERROR_THRESHOLD), ""
            if cmd and cmd[0] == "curl":
                if cmd[-1] == "http://www.gstatic.com/generate_204":
                    return 0, "204", ""
                if cmd[-1] == "https://api.telegram.org":
                    return 0, "404", ""
            return 0, "", ""

        with mock.patch.object(watchdog.plugins, "get", return_value=SimpleNamespace(meta={})):
            with mock.patch.object(watchdog, "_get_stack_socks_port", return_value=1083):
                with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
                    summary = asyncio.run(watchdog._run_active_stack_runtime_probes())

        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["recent_socks_errors"], watchdog.HYSTERIA2_SOCKS_ERROR_THRESHOLD)
        self.assertEqual(watchdog.state.active_stack_runtime_fail_streak, 1)

    def test_blocked_site_probe_uses_stable_control_plane_endpoint(self) -> None:
        self.assertEqual(watchdog.BLOCKED_CHECK_URLS, ["https://api.telegram.org"])

    def test_collect_runtime_diagnostics_uses_blocked_probe_contract(self) -> None:
        watchdog.state.active_stack = "hysteria2"
        watchdog.plugins._registry = {
            "hysteria2": SimpleNamespace(meta={"tun_name": "tun-hysteria2"}),
        }

        async def _fake_run_cmd(cmd, timeout=0):
            if cmd[:3] == ["dig", "@127.0.0.1", "youtube.com"]:
                return 0, "142.250.0.1\n", ""
            if cmd[:6] == ["curl", "-s", "--max-time", "10", "--interface", "tun-hysteria2"]:
                self.assertEqual(cmd[-1], "https://api.telegram.org")
                return 0, "200", ""
            return 0, "", ""

        with mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run_cmd):
            with mock.patch.object(watchdog, "ping_vps", return_value=(True, 42.0)):
                results = asyncio.run(watchdog.post_diagnose(None, "test-device", True))

        self.assertTrue(results["blocked_sites_ok"])

    def test_path_verdict_prefers_latency_sensitive_direct(self) -> None:
        with mock.patch.object(watchdog, "_scenario_src_ip", return_value="192.168.1.201"):
            with mock.patch.object(watchdog, "_route_get_sync", return_value={"ok": "true", "line": "", "table": "", "dev": "", "via": ""}):
                with mock.patch.object(
                    watchdog,
                    "_is_ip_in_nft_set",
                    side_effect=lambda set_name, ip: set_name in {"latency_sensitive_direct", "blocked_static"},
                ):
                    verdict = watchdog._functional_path_verdict("1.2.3.4", "lan")

        self.assertEqual(verdict["verdict"], "latency_sensitive_direct")

    def test_record_latency_learning_observation_promotes_catalog_matched_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            catalog_path = tmp / "latency-catalog.json"
            catalog_path.write_text(
                """
{
  "services": {
    "kinopoisk": {
      "display": "Kinopoisk",
      "category": "media",
      "auto_promote_allowed": true,
      "requires_direct_bootstrap": true,
      "domains": {
        "cdn": ["yastatic.net"]
      }
    }
  }
}
""".strip(),
                encoding="utf-8",
            )
            candidates_path = tmp / "latency-candidates.json"
            learned_path = tmp / "latency-learned.txt"
            manual_direct = tmp / "manual-direct.txt"
            manual_vpn = tmp / "manual-vpn.txt"
            latency_manual = tmp / "latency-sensitive-direct.txt"

            with mock.patch.object(watchdog, "LATENCY_CATALOG_FILE", catalog_path):
                with mock.patch.object(watchdog, "LATENCY_CANDIDATES_FILE", candidates_path):
                    with mock.patch.object(watchdog, "LATENCY_LEARNED_FILE", learned_path):
                        with mock.patch.object(watchdog, "LATENCY_MANUAL_FILE", latency_manual):
                            with mock.patch.object(watchdog, "ROUTES_DIR", tmp):
                                for _ in range(watchdog.LATENCY_AUTO_PROMOTE_SCORE):
                                    promoted = watchdog._record_latency_learning_observation(
                                        "cdn.yastatic.net",
                                        source="check",
                                        reason="blocked path",
                                        route_verdict="vpn",
                                        blocked_static=True,
                                    )

            self.assertTrue(promoted)
            self.assertIn("cdn.yastatic.net", learned_path.read_text(encoding="utf-8"))

    def test_record_latency_learning_observation_skips_manual_vpn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            catalog_path = tmp / "latency-catalog.json"
            catalog_path.write_text(
                """
{
  "services": {
    "bank": {
      "display": "Bank",
      "category": "bank",
      "auto_promote_allowed": true,
      "requires_direct_bootstrap": true,
      "domains": {
        "primary": ["example-bank.ru"]
      }
    }
  }
}
""".strip(),
                encoding="utf-8",
            )
            manual_vpn = tmp / "manual-vpn.txt"
            manual_vpn.write_text("api.example-bank.ru\n", encoding="utf-8")
            candidates_path = tmp / "latency-candidates.json"
            learned_path = tmp / "latency-learned.txt"
            manual_direct = tmp / "manual-direct.txt"
            latency_manual = tmp / "latency-sensitive-direct.txt"

            with mock.patch.object(watchdog, "LATENCY_CATALOG_FILE", catalog_path):
                with mock.patch.object(watchdog, "LATENCY_CANDIDATES_FILE", candidates_path):
                    with mock.patch.object(watchdog, "LATENCY_LEARNED_FILE", learned_path):
                        with mock.patch.object(watchdog, "LATENCY_MANUAL_FILE", latency_manual):
                            with mock.patch.object(watchdog, "ROUTES_DIR", tmp):
                                promoted = watchdog._record_latency_learning_observation(
                                    "api.example-bank.ru",
                                    source="check",
                                    reason="blocked path",
                                    route_verdict="vpn",
                                    blocked_static=True,
                                )

            self.assertFalse(promoted)
            self.assertFalse(learned_path.exists())


if __name__ == "__main__":
    unittest.main()

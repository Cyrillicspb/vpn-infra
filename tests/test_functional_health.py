#!/usr/bin/env python3
import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
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
        watchdog.state.functional_mode = watchdog.FUNCTIONAL_MODE_STAGED
        watchdog.state.functional_execution_status = watchdog.FUNCTIONAL_EXEC_DISABLED
        watchdog.state.functional_execution_last_error = ""
        watchdog.state.functional_execution_auto_disabled_reason = ""
        watchdog.state.functional_results = {}
        watchdog.state.functional_summary = {}
        watchdog.state.functional_evidence_store = {}
        watchdog.state.functional_infra_checks = []
        watchdog.state.last_functional_run_by_tier = {}

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


if __name__ == "__main__":
    unittest.main()

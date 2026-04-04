#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "watchdog" / "decision_maker.py"
SPEC = importlib.util.spec_from_file_location("decision_maker_module", MODULE_PATH)
decision_maker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(decision_maker)


class DecisionMakerTests(unittest.TestCase):
    def test_build_domain_context_sets_verdict_and_service_fields(self) -> None:
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match={"display": "ChatGPT", "service_id": "openai"},
            sources=["blocked_static"],
        )
        self.assertEqual(context["verdict"], "vpn")
        self.assertEqual(context["latency_service"], "ChatGPT")
        self.assertEqual(context["latency_service_id"], "openai")

    def test_build_domain_context_carries_chat_id(self) -> None:
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
            chat_id="42",
        )
        self.assertEqual(context["chat_id"], "42")

    def test_build_decision_state_and_explain_domain_context(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100}],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-a",
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
        )
        explanation = decision_maker.explain_domain_context(context, decision_state, now=1000.0)
        self.assertEqual(explanation["route_class"], "blocked_default")
        self.assertEqual(explanation["effective_backend_id"], "backend-a")

    def test_resolve_route_returns_route_mode_and_backend(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100}],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-a",
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["route_mode"], "vpn")
        self.assertEqual(resolution["route_class"], "blocked_default")
        self.assertEqual(resolution["effective_backend_id"], "backend-a")

    def test_resolve_route_prefers_client_backend_preference(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[
                {"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 50.0, "health_score": 100.0, "weight": 100},
                {"id": "backend-b", "ip": "203.0.113.20", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100},
            ],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-b",
            client_preferences=[
                {"id": 1, "chat_id": "42", "match_type": "domain", "match_value": "example.com", "backend_id": "backend-a", "enabled": 1},
            ],
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
            chat_id="42",
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["decision_source"], "vpn_client_pref")
        self.assertEqual(resolution["effective_backend_id"], "backend-a")
        self.assertEqual((resolution.get("matched_preference") or {}).get("backend_id"), "backend-a")
        self.assertEqual(resolution["preference_status"], "applied")

    def test_resolve_route_falls_back_when_preferred_backend_unhealthy(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[
                {"id": "backend-a", "ip": "198.51.100.10", "drain": True, "status": "healthy", "last_rtt_ms": 50.0, "health_score": 100.0, "weight": 100},
                {"id": "backend-b", "ip": "203.0.113.20", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100},
            ],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-b",
            client_preferences=[
                {"id": 1, "chat_id": "42", "match_type": "domain", "match_value": "example.com", "backend_id": "backend-a", "enabled": 1},
            ],
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
            chat_id="42",
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["decision_source"], "fallback_after_unhealthy")
        self.assertEqual(resolution["effective_backend_id"], "backend-b")
        self.assertEqual(resolution["fallback_reason"], "preferred_backend_in_drain")
        self.assertEqual((resolution.get("matched_preference") or {}).get("backend_id"), "backend-a")
        self.assertEqual(resolution["preference_status"], "fallback")
        self.assertEqual(resolution["preference_reason"], "preferred_backend_in_drain")

    def test_resolve_route_keeps_matching_preference_but_ignores_it_for_direct_policy(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[
                {"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 50.0, "health_score": 100.0, "weight": 100},
            ],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-a",
            client_preferences=[
                {"id": 1, "chat_id": "42", "match_type": "domain", "match_value": "example.com", "backend_id": "backend-a", "enabled": 1},
            ],
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=False,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=True,
            latency_catalog_match=None,
            sources=["manual_direct"],
            chat_id="42",
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["route_mode"], "direct")
        self.assertEqual(resolution["decision_source"], "no_backend_required")
        self.assertEqual((resolution.get("matched_preference") or {}).get("backend_id"), "backend-a")
        self.assertEqual(resolution["preference_status"], "ignored_by_policy")
        self.assertEqual(resolution["preference_reason"], "route_mode_direct")

    def test_resolve_route_prefers_lan_client_backend_preference(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[
                {"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 50.0, "health_score": 100.0, "weight": 100},
                {"id": "backend-b", "ip": "203.0.113.20", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100},
            ],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-b",
            lan_client_preferences=[
                {"id": "lan-pref-1", "lan_client_id": "lan-192-168-1-10", "match_type": "domain", "match_value": "example.com", "backend_id": "backend-a", "enabled": True},
            ],
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
            source_ip="192.168.1.10",
            identity_type="lan_client",
            identity_id="lan-192-168-1-10",
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["decision_source"], "lan_client_pref")
        self.assertEqual(resolution["effective_backend_id"], "backend-a")
        self.assertEqual(resolution["preference_status"], "applied")

    def test_resolve_route_ignores_lan_preference_for_direct_policy(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 50.0, "health_score": 100.0, "weight": 100}],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-a",
            lan_client_preferences=[
                {"id": "lan-pref-1", "lan_client_id": "lan-192-168-1-10", "match_type": "domain", "match_value": "example.com", "backend_id": "backend-a", "enabled": True},
            ],
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=False,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=True,
            latency_catalog_match=None,
            sources=["manual_direct"],
            source_ip="192.168.1.10",
            identity_type="lan_client",
            identity_id="lan-192-168-1-10",
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["route_mode"], "direct")
        self.assertEqual(resolution["preference_status"], "ignored_by_policy")
        self.assertEqual(resolution["preference_reason"], "route_mode_direct")

    def test_route_class_for_domain_check_prefers_service_assignment(self) -> None:
        result = {
            "verdict": "vpn",
            "latency_service_id": "telegram",
            "in_blocked_static": True,
            "in_blocked_dynamic": False,
            "in_manual_vpn": False,
        }
        self.assertEqual(decision_maker.route_class_for_domain_check(result), "service:telegram")

    def test_select_backend_prefers_low_rtt_then_health_then_weight(self) -> None:
        backends = [
            {"id": "slow", "drain": False, "status": "healthy", "last_rtt_ms": 90.0, "health_score": 100.0, "weight": 100},
            {"id": "fast", "drain": False, "status": "healthy", "last_rtt_ms": 15.0, "health_score": 60.0, "weight": 100},
        ]
        chosen = decision_maker.select_backend("vpn_default", backends)
        self.assertEqual((chosen or {}).get("id"), "fast")

    def test_select_backend_uses_active_backend_in_single_active_execution_mode(self) -> None:
        backends = [
            {"id": "slow", "drain": False, "status": "healthy", "last_rtt_ms": 90.0, "health_score": 100.0, "weight": 100},
            {"id": "fast", "drain": False, "status": "healthy", "last_rtt_ms": 15.0, "health_score": 60.0, "weight": 100},
        ]
        chosen = decision_maker.select_backend(
            "vpn_default",
            backends,
            active_backend_id="slow",
            execution_mode="single_active_backend",
        )
        self.assertEqual((chosen or {}).get("id"), "slow")

    def test_explain_domain_route_returns_assignment_and_source(self) -> None:
        result = {"verdict": "vpn", "in_blocked_static": True, "in_blocked_dynamic": False, "in_manual_vpn": False}
        backends = [
            {"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 10.0, "health_score": 100.0, "weight": 100},
        ]
        explanation = decision_maker.explain_domain_route(
            result,
            backends,
            {},
            ttl_seconds=300,
            active_backend_id="backend-a",
            now=1000.0,
        )
        self.assertEqual(explanation["route_class"], "blocked_default")
        self.assertEqual(explanation["decision_source"], "new_assignment")
        self.assertEqual(explanation["effective_backend_id"], "backend-a")
        self.assertEqual((explanation.get("backend") or {}).get("ip"), "198.51.100.10")

    def test_resolve_route_reports_execution_mode(self) -> None:
        decision_state = decision_maker.build_decision_state(
            backends=[{"id": "backend-a", "ip": "198.51.100.10", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100}],
            assignments={},
            idle_ttl_seconds=300,
            active_backend_id="backend-a",
            execution_mode="single_active_backend",
        )
        context = decision_maker.build_domain_context(
            domain="example.com",
            ips=["203.0.113.10"],
            in_latency_sensitive_direct=False,
            in_blocked_static=True,
            in_blocked_dynamic=False,
            in_manual_vpn=False,
            in_manual_direct=False,
            latency_catalog_match=None,
            sources=["blocked_static"],
        )
        resolution = decision_maker.resolve_route(context, decision_state, now=1000.0)
        self.assertEqual(resolution["execution_mode"], "single_active_backend")

    def test_choose_manual_backend_rejects_drain(self) -> None:
        decision = decision_maker.choose_manual_backend(
            "backend-a",
            [{"id": "backend-a", "drain": True, "status": "healthy"}],
            active_backend_id="backend-b",
        )
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["reason"], "backend_in_drain")

    def test_choose_auto_backend_returns_decision_object(self) -> None:
        decision = decision_maker.choose_auto_backend(
            "vpn_default",
            [{"id": "backend-a", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100}],
            active_backend_id="",
        )
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["decision_source"], "auto_select")
        self.assertEqual(decision["backend_id"], "backend-a")

    def test_reassign_route_class_returns_forced_assignment(self) -> None:
        result = decision_maker.reassign_route_class(
            "vpn_default",
            [{"id": "backend-a", "drain": False, "status": "healthy", "last_rtt_ms": 5.0, "health_score": 100.0, "weight": 100}],
            {"vpn_default": {"route_class": "vpn_default", "backend_id": "old", "assigned_at_ts": 1, "last_activity_ts": 1, "expires_at_ts": 2}},
            ttl_seconds=300,
            now=1000.0,
        )
        self.assertEqual(result["status"], "reassigned")
        self.assertEqual(result["decision_source"], "forced_reassign")
        self.assertEqual((result.get("assignment") or {}).get("backend_id"), "backend-a")

    def test_reconcile_assignments_to_active_backend_rewrites_backend_ids(self) -> None:
        result = decision_maker.reconcile_assignments_to_active_backend(
            {
                "vpn_default": {"route_class": "vpn_default", "backend_id": "old", "assigned_at_ts": 1, "last_activity_ts": 1, "expires_at_ts": 2},
                "blocked_default": {"route_class": "blocked_default", "backend_id": "other", "assigned_at_ts": 1, "last_activity_ts": 1, "expires_at_ts": 2},
            },
            "backend-a",
            ttl_seconds=300,
            now=1000.0,
        )
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["changed"], 2)
        self.assertEqual(result["assignments"]["vpn_default"]["backend_id"], "backend-a")
        self.assertEqual(result["assignments"]["blocked_default"]["backend_id"], "backend-a")


if __name__ == "__main__":
    unittest.main()

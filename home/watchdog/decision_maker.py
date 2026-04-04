from __future__ import annotations

import time
import ipaddress
from typing import Any, Optional


def now_ts() -> float:
    return time.time()


def build_decision_state(
    backends: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    idle_ttl_seconds: int,
    active_backend_id: str,
    client_preferences: Optional[list[dict[str, Any]]] = None,
    lan_clients: Optional[list[dict[str, Any]]] = None,
    lan_client_preferences: Optional[list[dict[str, Any]]] = None,
    execution_mode: str = "single_active_backend",
) -> dict[str, Any]:
    return {
        "backends": backends,
        "assignments": assignments,
        "idle_ttl_seconds": idle_ttl_seconds,
        "active_backend_id": active_backend_id,
        "client_preferences": list(client_preferences or []),
        "lan_clients": list(lan_clients or []),
        "lan_client_preferences": list(lan_client_preferences or []),
        "execution_mode": str(execution_mode or "single_active_backend"),
    }


def build_domain_context(
    domain: str,
    ips: list[str],
    in_latency_sensitive_direct: bool,
    in_blocked_static: bool,
    in_blocked_dynamic: bool,
    in_manual_vpn: bool,
    in_manual_direct: bool,
    latency_catalog_match: Optional[dict[str, Any]],
    sources: list[str],
    chat_id: str = "",
    source_ip: str = "",
    identity_type: str = "unknown",
    identity_id: str = "",
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "domain": domain,
        "ips": ips,
        "chat_id": str(chat_id or ""),
        "source_ip": str(source_ip or ""),
        "identity_type": str(identity_type or "unknown"),
        "identity_id": str(identity_id or ""),
        "in_latency_sensitive_direct": bool(in_latency_sensitive_direct),
        "in_blocked_static": bool(in_blocked_static),
        "in_blocked_dynamic": bool(in_blocked_dynamic),
        "in_manual_vpn": bool(in_manual_vpn),
        "in_manual_direct": bool(in_manual_direct),
        "latency_catalog_match": latency_catalog_match,
        "sources": sources,
    }
    if in_latency_sensitive_direct:
        context["verdict"] = "latency_sensitive_direct"
    elif in_manual_vpn or in_blocked_static or in_blocked_dynamic:
        context["verdict"] = "vpn"
    elif in_manual_direct:
        context["verdict"] = "direct"
    else:
        context["verdict"] = "unknown"

    if latency_catalog_match:
        context["latency_service"] = latency_catalog_match.get("display")
        context["latency_service_id"] = latency_catalog_match.get("service_id")
    return context


def _matches_client_pref(pref: dict[str, Any], context: dict[str, Any]) -> bool:
    match_type = str(pref.get("match_type") or "").strip().lower()
    match_value = str(pref.get("match_value") or "").strip().lower()
    if not match_type or not match_value:
        return False
    if match_type == "service":
        return str(context.get("latency_service_id") or "").strip().lower() == match_value
    if match_type == "domain":
        domain = str(context.get("domain") or "").strip().lower()
        return domain == match_value or domain.endswith("." + match_value)
    if match_type == "cidr":
        try:
            network = ipaddress.ip_network(match_value, strict=False)
        except ValueError:
            return False
        for raw_ip in context.get("ips") or []:
            try:
                if ipaddress.ip_address(str(raw_ip)) in network:
                    return True
            except ValueError:
                continue
    return False


def _candidate_client_prefs(
    context: dict[str, Any],
    client_preferences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chat_id = str(context.get("chat_id") or "").strip()
    if not chat_id:
        return []
    return [
        pref for pref in client_preferences
        if str(pref.get("chat_id") or "").strip() == chat_id
        and int(pref.get("enabled", 1) or 0) == 1
        and _matches_client_pref(pref, context)
    ]


def _candidate_lan_prefs(
    context: dict[str, Any],
    lan_client_preferences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if str(context.get("identity_type") or "") != "lan_client":
        return []
    lan_client_id = str(context.get("identity_id") or "").strip()
    if not lan_client_id:
        return []
    return [
        pref for pref in lan_client_preferences
        if str(pref.get("lan_client_id") or "").strip() == lan_client_id
        and bool(pref.get("enabled", True))
        and _matches_client_pref(pref, context)
    ]


def match_client_backend_preference(
    context: dict[str, Any],
    client_preferences: list[dict[str, Any]],
    backends: list[dict[str, Any]],
) -> dict[str, Any]:
    if str(context.get("verdict") or "") != "vpn":
        return {"matched": False}
    candidates = _candidate_client_prefs(context, client_preferences)
    if not candidates:
        return {"matched": False}
    pref = sorted(candidates, key=lambda item: int(item.get("id", 0) or 0))[0]
    backend_id = str(pref.get("backend_id") or "")
    backend = backend_by_id(backends, backend_id)
    if not backend:
        return {
            "matched": True,
            "pref": pref,
            "usable": False,
            "reason": "preferred_backend_not_found",
        }
    if backend.get("drain"):
        return {
            "matched": True,
            "pref": pref,
            "backend": backend,
            "usable": False,
            "reason": "preferred_backend_in_drain",
        }
    if backend_effective_status(backend) not in {"healthy", "unknown"}:
        return {
            "matched": True,
            "pref": pref,
            "backend": backend,
            "usable": False,
            "reason": "preferred_backend_unhealthy",
        }
    return {
        "matched": True,
        "pref": pref,
        "backend": backend,
        "usable": True,
    }


def match_lan_backend_preference(
    context: dict[str, Any],
    lan_client_preferences: list[dict[str, Any]],
    backends: list[dict[str, Any]],
) -> dict[str, Any]:
    if str(context.get("verdict") or "") != "vpn":
        return {"matched": False}
    candidates = _candidate_lan_prefs(context, lan_client_preferences)
    if not candidates:
        return {"matched": False}
    pref = candidates[0]
    backend_id = str(pref.get("backend_id") or "")
    backend = backend_by_id(backends, backend_id)
    if not backend:
        return {"matched": True, "pref": pref, "usable": False, "reason": "preferred_backend_not_found"}
    if backend.get("drain"):
        return {"matched": True, "pref": pref, "backend": backend, "usable": False, "reason": "preferred_backend_in_drain"}
    if backend_effective_status(backend) not in {"healthy", "unknown"}:
        return {"matched": True, "pref": pref, "backend": backend, "usable": False, "reason": "preferred_backend_unhealthy"}
    return {"matched": True, "pref": pref, "backend": backend, "usable": True}


def normalize_assignment(raw: dict[str, Any], ttl_seconds: int, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    current = float(now if now is not None else now_ts())
    route_class = str(raw.get("route_class") or "").strip()
    backend_id = str(raw.get("backend_id") or "").strip()
    if not route_class or not backend_id:
        return None
    assigned_at_ts = float(raw.get("assigned_at_ts", current) or current)
    last_activity_ts = float(raw.get("last_activity_ts", assigned_at_ts) or assigned_at_ts)
    expires_at_ts = float(raw.get("expires_at_ts", last_activity_ts + ttl_seconds) or (last_activity_ts + ttl_seconds))
    return {
        "route_class": route_class,
        "backend_id": backend_id,
        "assigned_at_ts": assigned_at_ts,
        "last_activity_ts": last_activity_ts,
        "expires_at_ts": expires_at_ts,
    }


def backend_by_id(backends: list[dict[str, Any]], backend_id: str) -> Optional[dict[str, Any]]:
    for backend in backends:
        if backend.get("id") == backend_id:
            return backend
    return None


def backend_effective_status(backend: dict[str, Any]) -> str:
    if backend.get("drain"):
        return "drain"
    return str(backend.get("status") or "unknown")


def healthy_backends(backends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        backend
        for backend in backends
        if not backend.get("drain") and backend_effective_status(backend) in {"healthy", "unknown"}
    ]


def select_backend(
    route_class: str,
    backends: list[dict[str, Any]],
    active_backend_id: str = "",
    execution_mode: str = "multi_backend",
) -> Optional[dict[str, Any]]:
    del route_class
    if execution_mode == "single_active_backend" and active_backend_id:
        active_backend = backend_by_id(backends, active_backend_id)
        if active_backend and not active_backend.get("drain") and backend_effective_status(active_backend) in {"healthy", "unknown"}:
            return active_backend
    candidates = healthy_backends(backends)
    if not candidates:
        return None

    def _sort_key(backend: dict[str, Any]) -> tuple[float, float, int, str]:
        last_rtt = float(backend.get("last_rtt_ms", 0.0) or 0.0)
        if last_rtt <= 0:
            last_rtt = 10_000.0
        health_score = float(backend.get("health_score", 100.0) or 100.0)
        weight = int(backend.get("weight", 100) or 100)
        return (last_rtt, -health_score, -weight, str(backend.get("id", "")))

    return sorted(candidates, key=_sort_key)[0]


def prune_assignments(
    assignments: dict[str, dict[str, Any]],
    backends: list[dict[str, Any]],
    ttl_seconds: int,
    now: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    current = float(now if now is not None else now_ts())
    healthy_ids = {backend["id"] for backend in backends}
    cleaned: dict[str, dict[str, Any]] = {}
    for route_class, spec in assignments.items():
        assignment = normalize_assignment(spec, ttl_seconds, now=current)
        if not assignment:
            continue
        if assignment["backend_id"] not in healthy_ids:
            continue
        if assignment["expires_at_ts"] <= current:
            continue
        cleaned[route_class] = assignment
    return cleaned


def ensure_assignment(
    route_class: str,
    backends: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    ttl_seconds: int,
    active_backend_id: str = "",
    execution_mode: str = "multi_backend",
    force_reassign: bool = False,
    now: Optional[float] = None,
) -> tuple[Optional[dict[str, Any]], dict[str, dict[str, Any]], str]:
    current = float(now if now is not None else now_ts())
    working = dict(assignments)
    assignment = working.get(route_class)
    if assignment and not force_reassign:
        normalized = normalize_assignment(assignment, ttl_seconds, now=current)
        if normalized and normalized["expires_at_ts"] > current:
            normalized["last_activity_ts"] = current
            normalized["expires_at_ts"] = current + ttl_seconds
            working[route_class] = normalized
            return normalized, working, "existing_assignment"

    backend = select_backend(route_class, backends, active_backend_id=active_backend_id, execution_mode=execution_mode)
    if not backend:
        return None, working, "no_healthy_backend"

    new_assignment = {
        "route_class": route_class,
        "backend_id": str(backend["id"]),
        "assigned_at_ts": current,
        "last_activity_ts": current,
        "expires_at_ts": current + ttl_seconds,
    }
    working[route_class] = new_assignment
    return new_assignment, working, "new_assignment" if not force_reassign else "forced_reassign"


def balancer_snapshot(
    backends: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    idle_ttl_seconds: int,
    active_backend_id: str,
    execution_mode: str = "single_active_backend",
    now: Optional[float] = None,
) -> dict[str, Any]:
    current = float(now if now is not None else now_ts())
    assignment_rows: list[dict[str, Any]] = []
    for route_class, spec in sorted(assignments.items()):
        assignment_rows.append(
            {
                **spec,
                "ttl_seconds_left": max(0, int(float(spec.get("expires_at_ts", current)) - current)),
                "backend": backend_by_id(backends, str(spec.get("backend_id", ""))),
            }
        )
    healthy = len([b for b in backends if backend_effective_status(b) in {"healthy", "unknown"} and not b.get("drain")])
    return {
        "idle_ttl_seconds": idle_ttl_seconds,
        "execution_mode": execution_mode,
        "backend_count": len(backends),
        "healthy_backend_count": healthy,
        "active_backend_id": active_backend_id,
        "active_backend": backend_by_id(backends, active_backend_id),
        "assignments": assignment_rows,
    }


def route_class_for_domain_check(result: dict[str, Any]) -> Optional[str]:
    verdict = str(result.get("verdict") or "")
    if verdict != "vpn":
        return None
    service_id = str(result.get("latency_service_id") or "")
    if service_id:
        return f"service:{service_id}"
    if result.get("in_blocked_static") or result.get("in_blocked_dynamic") or result.get("in_manual_vpn"):
        return "blocked_default"
    return "vpn_default"


def explain_domain_route(
    result: dict[str, Any],
    backends: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    ttl_seconds: int,
    active_backend_id: str,
    execution_mode: str = "single_active_backend",
    now: Optional[float] = None,
) -> dict[str, Any]:
    current = float(now if now is not None else now_ts())
    route_class = route_class_for_domain_check(result)
    explanation: dict[str, Any] = {
        "route_class": route_class,
        "decision_source": "no_backend_required",
        "effective_backend_id": "",
        "active_backend_id": active_backend_id,
        "fallback_reason": "",
    }
    if not route_class:
        return explanation

    assignment, updated_assignments, decision_source = ensure_assignment(
        route_class,
        backends,
        assignments,
        ttl_seconds,
        active_backend_id=active_backend_id,
        execution_mode=execution_mode,
        now=current,
    )
    backend = backend_by_id(backends, str((assignment or {}).get("backend_id", ""))) if assignment else None
    explanation.update(
        {
            "route_class": route_class,
            "decision_source": decision_source,
            "effective_backend_id": str((assignment or {}).get("backend_id", "")),
            "backend_assignment": assignment,
            "backend": backend,
            "updated_assignments": updated_assignments,
        }
    )
    if assignment and active_backend_id and assignment.get("backend_id") != active_backend_id:
        explanation["fallback_reason"] = "active_backend_differs_from_decision"
    return explanation


def explain_domain_context(
    context: dict[str, Any],
    decision_state: dict[str, Any],
    now: Optional[float] = None,
) -> dict[str, Any]:
    return explain_domain_route(
        context,
        list(decision_state.get("backends") or []),
        dict(decision_state.get("assignments") or {}),
        int(decision_state.get("idle_ttl_seconds") or 300),
        str(decision_state.get("active_backend_id") or ""),
        str(decision_state.get("execution_mode") or "single_active_backend"),
        now=now,
    )


def resolve_route(
    context: dict[str, Any],
    decision_state: dict[str, Any],
    now: Optional[float] = None,
) -> dict[str, Any]:
    client_preferences = list(decision_state.get("client_preferences") or [])
    lan_client_preferences = list(decision_state.get("lan_client_preferences") or [])
    route_mode = str(context.get("verdict") or "unknown")
    pref_candidates = _candidate_client_prefs(context, client_preferences)
    lan_pref_candidates = _candidate_lan_prefs(context, lan_client_preferences)
    matched_preference = pref_candidates[0] if pref_candidates else None
    matched_lan_preference = lan_pref_candidates[0] if lan_pref_candidates else None
    preference_status = "none"
    preference_reason = ""
    client_pref_match = match_client_backend_preference(
        context,
        client_preferences,
        list(decision_state.get("backends") or []),
    )
    lan_pref_match = match_lan_backend_preference(
        context,
        lan_client_preferences,
        list(decision_state.get("backends") or []),
    )
    explanation = explain_domain_context(context, decision_state, now=now)
    if matched_lan_preference and route_mode != "vpn":
        preference_status = "ignored_by_policy"
        preference_reason = f"route_mode_{route_mode}"
        explanation["matched_preference"] = matched_lan_preference
    elif matched_preference and route_mode != "vpn":
        preference_status = "ignored_by_policy"
        preference_reason = f"route_mode_{route_mode}"
        explanation["matched_preference"] = matched_preference
    if lan_pref_match.get("matched") and lan_pref_match.get("usable"):
        pref = lan_pref_match.get("pref") or {}
        backend = lan_pref_match.get("backend") or {}
        explanation["decision_source"] = "lan_client_pref"
        explanation["effective_backend_id"] = str(backend.get("id") or "")
        explanation["backend"] = backend
        explanation["matched_preference"] = pref
        explanation["fallback_reason"] = ""
        preference_status = "applied"
        preference_reason = ""
    elif lan_pref_match.get("matched"):
        explanation["matched_preference"] = lan_pref_match.get("pref")
        explanation["fallback_reason"] = str(lan_pref_match.get("reason") or "")
        explanation["decision_source"] = "fallback_after_unhealthy"
        preference_status = "fallback"
        preference_reason = explanation["fallback_reason"]
    elif client_pref_match.get("matched") and client_pref_match.get("usable"):
        pref = client_pref_match.get("pref") or {}
        backend = client_pref_match.get("backend") or {}
        explanation["decision_source"] = "vpn_client_pref"
        explanation["effective_backend_id"] = str(backend.get("id") or "")
        explanation["backend"] = backend
        explanation["matched_preference"] = pref
        explanation["fallback_reason"] = ""
        preference_status = "applied"
        preference_reason = ""
    elif client_pref_match.get("matched"):
        explanation["matched_preference"] = client_pref_match.get("pref")
        explanation["fallback_reason"] = str(client_pref_match.get("reason") or "")
        explanation["decision_source"] = "fallback_after_unhealthy"
        preference_status = "fallback"
        preference_reason = explanation["fallback_reason"]
    return {
        "route_mode": route_mode,
        "route_class": explanation.get("route_class"),
        "decision_source": explanation.get("decision_source"),
        "effective_backend_id": explanation.get("effective_backend_id"),
        "execution_mode": decision_state.get("execution_mode"),
        "fallback_reason": explanation.get("fallback_reason", ""),
        "backend_assignment": explanation.get("backend_assignment"),
        "backend": explanation.get("backend"),
        "matched_preference": explanation.get("matched_preference"),
        "preference_status": preference_status,
        "preference_reason": preference_reason,
        "updated_assignments": explanation.get("updated_assignments"),
        "context": context,
    }


def choose_manual_backend(
    backend_id: str,
    backends: list[dict[str, Any]],
    active_backend_id: str,
) -> dict[str, Any]:
    backend = backend_by_id(backends, backend_id)
    if not backend:
        return {
            "ok": False,
            "decision_source": "manual_request",
            "backend_id": backend_id,
            "reason": "backend_not_found",
        }
    if backend.get("drain"):
        return {
            "ok": False,
            "decision_source": "manual_request",
            "backend_id": backend_id,
            "reason": "backend_in_drain",
            "backend": backend,
        }
    return {
        "ok": True,
        "decision_source": "manual_request",
        "backend_id": backend_id,
        "backend": backend,
        "reason": "manual_switch",
        "active_backend_id": active_backend_id,
    }


def choose_auto_backend(
    route_class: str,
    backends: list[dict[str, Any]],
    active_backend_id: str,
) -> dict[str, Any]:
    backend = select_backend(route_class, backends, active_backend_id=active_backend_id, execution_mode="multi_backend")
    if not backend:
        return {
            "ok": False,
            "decision_source": "auto_select",
            "route_class": route_class,
            "reason": "no_healthy_backend",
            "active_backend_id": active_backend_id,
        }
    return {
        "ok": True,
        "decision_source": "auto_select",
        "route_class": route_class,
        "backend_id": str(backend.get("id") or ""),
        "backend": backend,
        "reason": "auto_select",
        "active_backend_id": active_backend_id,
    }


def reassign_route_class(
    route_class: str,
    backends: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    ttl_seconds: int,
    active_backend_id: str = "",
    execution_mode: str = "multi_backend",
    now: Optional[float] = None,
) -> dict[str, Any]:
    working = dict(assignments)
    if route_class == "all":
        return {
            "status": "reassigned",
            "route_class": "all",
            "assignments": {},
            "decision_source": "decision_reassign_all",
        }

    working.pop(route_class, None)
    assignment, updated_assignments, decision_source = ensure_assignment(
        route_class,
        backends,
        working,
        ttl_seconds,
        active_backend_id=active_backend_id,
        execution_mode=execution_mode,
        force_reassign=True,
        now=now,
    )
    return {
        "status": "reassigned",
        "route_class": route_class,
        "assignment": assignment,
        "assignments": updated_assignments,
        "decision_source": decision_source,
    }


def reconcile_assignments_to_active_backend(
    assignments: dict[str, dict[str, Any]],
    active_backend_id: str,
    ttl_seconds: int,
    now: Optional[float] = None,
) -> dict[str, Any]:
    current = float(now if now is not None else now_ts())
    if not active_backend_id:
        return {
            "status": "noop",
            "reason": "no_active_backend",
            "assignments": dict(assignments),
            "changed": 0,
        }
    updated: dict[str, dict[str, Any]] = {}
    changed = 0
    for route_class, spec in dict(assignments).items():
        assignment = normalize_assignment(spec, ttl_seconds, now=current)
        if not assignment:
            continue
        if assignment.get("backend_id") != active_backend_id:
            assignment["backend_id"] = active_backend_id
            assignment["last_activity_ts"] = current
            assignment["expires_at_ts"] = current + ttl_seconds
            changed += 1
        updated[route_class] = assignment
    return {
        "status": "reconciled" if changed else "noop",
        "reason": "single_active_backend_reconcile",
        "assignments": updated,
        "changed": changed,
        "active_backend_id": active_backend_id,
    }

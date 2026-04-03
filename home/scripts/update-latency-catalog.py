#!/usr/bin/env python3
"""
update-latency-catalog.py — сборка runtime latency catalog.

Что делает:
  - читает fallback catalog из репозитория
  - опционально мерджит runtime overlay и remote JSON sources
  - пишет канонический /etc/vpn-routes/latency-catalog.json
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path


LOG = logging.getLogger("update-latency-catalog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCRIPT_DIR = Path(__file__).resolve().parent
ROUTES_DIR = Path("/etc/vpn-routes")
TARGET_CATALOG = ROUTES_DIR / "latency-catalog.json"
OVERLAY_CATALOG = ROUTES_DIR / "latency-catalog-overlay.json"
REMOTE_URLS = ROUTES_DIR / "latency-catalog-remote.urls"
FALLBACK_CANDIDATES = [
    Path("/opt/vpn/home/routes/latency-catalog-default.json"),
    SCRIPT_DIR.parent / "routes" / "latency-catalog-default.json",
]
HTTP_TIMEOUT = 30


def _normalize_domain(value: str) -> str:
    return str(value or "").strip().lower().lstrip("*.").strip(".")


def _is_domain(value: str) -> bool:
    value = _normalize_domain(value)
    return bool(value and "." in value and len(value) <= 253)


def _dedupe_suffixes(domains: list[str]) -> list[str]:
    result: list[str] = []
    for raw in domains:
        domain = _normalize_domain(raw)
        if not _is_domain(domain):
            continue
        if any(domain == parent or domain.endswith(f".{parent}") for parent in result):
            continue
        result.append(domain)
    return result


def _canonicalize(raw: dict) -> dict[str, dict]:
    services: dict[str, dict] = {}
    for service_id, spec in (raw.get("services") or {}).items():
        if not isinstance(spec, dict):
            continue
        roles: dict[str, list[str]] = {}
        for role, values in (spec.get("domains") or {}).items():
            domains = _dedupe_suffixes(list(values or []))
            if domains:
                roles[str(role)] = domains
        if not roles:
            continue
        services[str(service_id).strip().lower()] = {
            "display": str(spec.get("display") or service_id),
            "category": str(spec.get("category") or "misc"),
            "auto_promote_allowed": bool(spec.get("auto_promote_allowed", True)),
            "geo_sensitive": bool(spec.get("geo_sensitive", True)),
            "requires_direct_bootstrap": bool(spec.get("requires_direct_bootstrap", True)),
            "domains": roles,
        }
    return services


def _merge(*catalogs: dict[str, dict]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for catalog in catalogs:
        for service_id, spec in catalog.items():
            dst = merged.setdefault(
                service_id,
                {
                    "display": spec.get("display", service_id),
                    "category": spec.get("category", "misc"),
                    "auto_promote_allowed": bool(spec.get("auto_promote_allowed", True)),
                    "geo_sensitive": bool(spec.get("geo_sensitive", True)),
                    "requires_direct_bootstrap": bool(spec.get("requires_direct_bootstrap", True)),
                    "domains": {},
                },
            )
            dst["display"] = str(spec.get("display") or dst["display"])
            dst["category"] = str(spec.get("category") or dst["category"])
            dst["auto_promote_allowed"] = bool(spec.get("auto_promote_allowed", dst["auto_promote_allowed"]))
            dst["geo_sensitive"] = bool(spec.get("geo_sensitive", dst["geo_sensitive"]))
            dst["requires_direct_bootstrap"] = bool(
                spec.get("requires_direct_bootstrap", dst["requires_direct_bootstrap"])
            )
            for role, values in (spec.get("domains") or {}).items():
                dst["domains"][role] = _dedupe_suffixes(list(dst["domains"].get(role, [])) + list(values or []))
    return merged


def _load_json(path: Path) -> dict[str, dict]:
    return _canonicalize(json.loads(path.read_text(encoding="utf-8")))


def _fetch_json(url: str) -> dict[str, dict]:
    request = urllib.request.Request(url, headers={"User-Agent": "vpn-infra/update-latency-catalog"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return _canonicalize(json.loads(response.read().decode("utf-8", errors="replace")))


def build_catalog() -> dict:
    fallback = {}
    for path in FALLBACK_CANDIDATES:
        if path.exists():
            fallback = _load_json(path)
            break
    if not fallback:
        raise FileNotFoundError("latency catalog fallback not found")

    overlay = _load_json(OVERLAY_CATALOG) if OVERLAY_CATALOG.exists() else {}
    remotes: list[dict[str, dict]] = []
    if REMOTE_URLS.exists():
        for line in REMOTE_URLS.read_text(encoding="utf-8").splitlines():
            url = line.split("#")[0].strip()
            if not url:
                continue
            try:
                remotes.append(_fetch_json(url))
            except Exception as exc:
                LOG.warning("remote latency catalog %s failed: %s", url, exc)

    merged = _merge(fallback, *remotes, overlay)
    return {"services": merged}


def main() -> None:
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_catalog()
    TARGET_CATALOG.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    LOG.info("latency catalog written: services=%s path=%s", len(payload["services"]), TARGET_CATALOG)


if __name__ == "__main__":
    main()

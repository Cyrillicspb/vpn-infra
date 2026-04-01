#!/usr/bin/env python3
"""
update-dpi-presets.py — обновление каталогов DPI-bypass пресетов.

Источник:
  - v2fly/domain-list-community (фиксированный набор сервисов)

Что делает:
  - читает fallback пресеты из репозитория
  - подтягивает tier2 домены из v2fly
  - фильтрует шумные записи и дедуплицирует суффиксы
  - пишет /etc/vpn/dpi-presets.json
  - при наличии WATCHDOG_API_TOKEN делает hot-reload через watchdog API
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("update-dpi-presets")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_FALLBACK_PRESETS = SCRIPT_DIR.parent / "dpi" / "presets-default.json"
FALLBACK_PRESETS_CANDIDATES = [
    Path("/opt/vpn/home/dpi/presets-default.json"),
    REPO_FALLBACK_PRESETS,
]
TARGET_PRESETS = Path("/etc/vpn/dpi-presets.json")

WATCHDOG_URL = "http://127.0.0.1:8080/dpi/presets/reload"
WATCHDOG_TOKEN_FILE = Path("/opt/vpn/.env")

V2FLY_RAW_BASE = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data"
V2FLY_MAPPING = {
    "youtube": "youtube",
    "instagram": "instagram",
    "twitter": "twitter",
    "spotify": "spotify",
    "steam": "steam",
    "tiktok": "tiktok",
}

V2FLY_EXCLUDE_PREFIXES = ("api.", "auth.", "login.", "accounts.")
EXCLUDED_DOMAINS = {
    "ggpht.cn",
}
V2FLY_MAX_DOMAINS_PER_SERVICE = 15
HTTP_TIMEOUT = 30
SERVICE_DOMAIN_FRAGMENTS = {
    "youtube": ("youtube", "youtu", "ytimg", "googlevideo", "ggpht"),
    "instagram": ("instagram", "cdninstagram", "igcdn", "ig.me", "igtv", "igsonar", "fbcdn"),
    "twitter": ("twitter", "tweet", "twimg", "t.co", "x.com", "periscope", "pscp"),
    "spotify": ("spotify", "spoti", "scdn", "pscdn"),
    "steam": ("steam", "steampowered", "steamcommunity", "steamstatic", "steamcontent", "steamdeck", "steamgames", "s.team"),
    "tiktok": ("tiktok", "musical.ly", "byteoversea", "muscdn", "tik-tok"),
}


def _normalize_domain_name(value: str) -> str:
    return str(value or "").strip().lower().lstrip("*.").strip(".")


def _is_domain_like(value: str) -> bool:
    value = _normalize_domain_name(value)
    if not value or "." not in value or value.startswith("@"):
        return False
    return re.match(r"^[a-z0-9.-]+\.[a-z0-9-]+$", value) is not None


def _dedupe_domain_suffixes(domains: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        domain = _normalize_domain_name(raw)
        if not _is_domain_like(domain) or domain in seen:
            continue
        if any(domain == parent or domain.endswith(f".{parent}") for parent in result):
            continue
        result.append(domain)
        seen.add(domain)
    return result


def _canonicalize_preset_map(raw: dict[str, Any]) -> dict[str, dict]:
    presets: dict[str, dict] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        domains = _dedupe_domain_suffixes(list(spec.get("domains") or []))
        if not domains:
            continue
        presets[str(name)] = {
            "display": str(spec.get("display") or name),
            "domains": domains,
        }
    return presets


def _load_fallback_presets() -> dict[str, dict]:
    for path in FALLBACK_PRESETS_CANDIDATES:
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        presets = _canonicalize_preset_map(raw)
        if presets:
            return presets
    searched = ", ".join(str(path) for path in FALLBACK_PRESETS_CANDIDATES)
    raise FileNotFoundError(f"fallback presets not found: {searched}")


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "vpn-infra/update-dpi-presets"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_v2fly_domains(text: str) -> list[str]:
    domains: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line:
            line = line.split("@", 1)[0].strip()
        if ":" in line:
            prefix, value = line.split(":", 1)
            prefix = prefix.strip().lower()
            value = value.strip()
            if prefix in {"domain", "full"}:
                candidate = value
            else:
                continue
        else:
            candidate = line
        candidate = _normalize_domain_name(candidate)
        if not _is_domain_like(candidate):
            continue
        if candidate in EXCLUDED_DOMAINS:
            continue
        if candidate.startswith(V2FLY_EXCLUDE_PREFIXES):
            continue
        domains.append(candidate)
    return _dedupe_domain_suffixes(domains)


def _service_allows_domain(service: str, domain: str, core_domains: list[str]) -> bool:
    if domain in core_domains:
        return True
    fragments = SERVICE_DOMAIN_FRAGMENTS.get(service, ())
    return any(fragment in domain for fragment in fragments)


def _merge_service_domains(core: list[str], tier2: list[str]) -> list[str]:
    merged = list(_dedupe_domain_suffixes(core))
    for domain in tier2:
        if len(merged) >= V2FLY_MAX_DOMAINS_PER_SERVICE:
            break
        if domain in merged:
            continue
        if any(domain == parent or domain.endswith(f".{parent}") for parent in merged):
            continue
        merged.append(domain)
    return merged[:V2FLY_MAX_DOMAINS_PER_SERVICE]


def _load_watchdog_token() -> str:
    if not WATCHDOG_TOKEN_FILE.exists():
        return ""
    for line in WATCHDOG_TOKEN_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("WATCHDOG_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _reload_watchdog_presets() -> None:
    token = _load_watchdog_token()
    if not token:
        LOG.warning("WATCHDOG_API_TOKEN not found, skipping hot reload")
        return
    request = urllib.request.Request(
        WATCHDOG_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "vpn-infra/update-dpi-presets",
        },
        data=b"{}",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read().decode("utf-8", errors="replace").strip()
    LOG.info("watchdog preset reload response: %s", body or "ok")


def build_presets() -> dict[str, dict]:
    presets = _load_fallback_presets()
    for service, source_name in V2FLY_MAPPING.items():
        spec = presets.get(service)
        if not spec:
            continue
        url = f"{V2FLY_RAW_BASE}/{source_name}"
        try:
            core_domains = list(spec["domains"])
            tier2_domains = [
                domain
                for domain in _parse_v2fly_domains(_fetch_text(url))
                if _service_allows_domain(service, domain, core_domains)
            ]
            spec["domains"] = _merge_service_domains(list(spec["domains"]), tier2_domains)
            LOG.info("updated preset %s from %s (%s domains)", service, source_name, len(spec["domains"]))
        except urllib.error.URLError as exc:
            LOG.warning("failed to fetch %s from v2fly: %s", source_name, exc)
        except Exception as exc:
            LOG.warning("failed to process %s from v2fly: %s", source_name, exc)
    return presets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true", help="Print resulting JSON to stdout")
    parser.add_argument("--reload-watchdog", action="store_true", help="Call watchdog hot-reload after write")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    presets = build_presets()
    payload = json.dumps(presets, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if args.stdout:
        sys.stdout.write(payload)
        return 0

    TARGET_PRESETS.parent.mkdir(parents=True, exist_ok=True)
    TARGET_PRESETS.write_text(payload, encoding="utf-8")
    LOG.info("written %s (%s services)", TARGET_PRESETS, len(presets))

    if args.reload_watchdog:
        try:
            _reload_watchdog_presets()
        except Exception as exc:
            LOG.warning("watchdog preset reload failed: %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

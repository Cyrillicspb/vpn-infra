#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "scripts" / "update-routes.py"
SPEC = importlib.util.spec_from_file_location("update_routes", MODULE_PATH)
update_routes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update_routes)


class StableHashPayloadTests(unittest.TestCase):
    def test_ignores_dnsmasq_timestamp_only_changes(self) -> None:
        content_a = (
            "# vpn-domains.conf — Автогенерировано update-routes.py\n"
            "# Обновлено: 2026-03-31T00:00:00+00:00\n"
            "# Доменов: 1\n\n"
            "server=/example.com/10.177.2.2\n"
            "nftset=/example.com/4#inet#vpn#blocked_dynamic\n"
        )
        content_b = content_a.replace(
            "2026-03-31T00:00:00+00:00",
            "2026-03-31T01:23:45+00:00",
        )

        payload_a = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content=content_a,
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="",
        )
        payload_b = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content=content_b,
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="",
        )

        self.assertEqual(payload_a, payload_b)

    def test_detects_nft_only_changes(self) -> None:
        payload_a = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="",
        )
        payload_b = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.2/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="",
        )

        self.assertNotEqual(payload_a, payload_b)

    def test_detects_direct_dnsmasq_changes(self) -> None:
        payload_a = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="server=/.ru/77.88.8.8\n",
            dnsmasq_latency_content="",
        )
        payload_b = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="server=/.ru/77.88.8.1\n",
            dnsmasq_latency_content="",
        )

        self.assertNotEqual(payload_a, payload_b)

    def test_detects_latency_dnsmasq_changes(self) -> None:
        payload_a = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="server=/okko.tv/77.88.8.8\n",
        )
        payload_b = update_routes.build_stable_hash_payload(
            allowed_cidrs=["1.1.1.0/24"],
            nft_cidrs=["1.1.1.1/32"],
            dnsmasq_domains_content="",
            dnsmasq_force_content="",
            dnsmasq_direct_content="",
            dnsmasq_latency_content="server=/okko.tv/77.88.8.1\n",
        )

        self.assertNotEqual(payload_a, payload_b)


class DnsmasqDpiExclusionTests(unittest.TestCase):
    def test_render_dnsmasq_config_excludes_dpi_subdomains(self) -> None:
        content, written, excluded = update_routes.render_dnsmasq_config(
            domains=[
                "googlevideo.com",
                "rr5---sn-a5meknzr.googlevideo.com",
                "ytimg.com",
                "i.ytimg.com",
                "youtube.com.tr",
                "example.com",
            ],
            out_name="vpn-domains.conf",
            vps_dns="10.177.2.2",
            exclude_domains={"googlevideo.com", "ytimg.com", "youtube.com"},
        )

        self.assertIn("server=/example.com/10.177.2.2", content)
        self.assertNotIn("googlevideo.com", content)
        self.assertNotIn("rr5---sn-a5meknzr.googlevideo.com", content)
        self.assertNotIn("ytimg.com", content)
        self.assertNotIn("i.ytimg.com", content)
        self.assertNotIn("youtube.com.tr", content)
        self.assertEqual(written, 1)
        self.assertEqual(excluded, 5)

    def test_load_active_dpi_domains_requires_experimental_opt_in(self) -> None:
        payload = {
            "dpi_enabled": True,
            "dpi_experimental_opt_in": False,
            "dpi_services": [
                {"name": "youtube", "enabled": True, "domains": ["youtube.com", "googlevideo.com"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(update_routes, "WATCHDOG_STATE", state_path):
                self.assertEqual(update_routes.load_active_dpi_domains(), set())

            payload["dpi_experimental_opt_in"] = True
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(update_routes, "WATCHDOG_STATE", state_path):
                self.assertEqual(
                    update_routes.load_active_dpi_domains(),
                    {"youtube.com", "googlevideo.com"},
                )

    def test_build_latency_sensitive_domains_merges_catalog_and_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text(
                json.dumps(
                    {
                        "services": {
                            "catalog-service": {
                                "display": "Catalog Service",
                                "category": "media",
                                "requires_direct_bootstrap": True,
                                "domains": {
                                    "primary": ["catalog.example"],
                                    "cdn": ["cdn.catalog.example"]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            runtime = tmp / "latency-catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "services": {
                            "runtime-service": {
                                "display": "Runtime Service",
                                "category": "work",
                                "requires_direct_bootstrap": True,
                                "domains": {
                                    "primary": ["runtime.example"]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            learned = tmp / "latency-learned.txt"
            learned.write_text("learned.example\n", encoding="utf-8")
            manual = tmp / "latency-sensitive-direct.txt"
            manual.write_text("manual.example\n", encoding="utf-8")

            with mock.patch.object(update_routes, "REPO_LATENCY_CATALOG", fallback):
                with mock.patch.object(update_routes, "LATENCY_CATALOG", runtime):
                    with mock.patch.object(update_routes, "LATENCY_LEARNED", learned):
                        with mock.patch.object(update_routes, "LATENCY_DIRECT", manual):
                            domains = update_routes.build_latency_sensitive_domains(
                                manual_direct_domains={"extra.example"}
                            )

        self.assertIn("catalog.example", domains)
        self.assertIn("runtime.example", domains)
        self.assertIn("learned.example", domains)
        self.assertIn("manual.example", domains)
        self.assertIn("extra.example", domains)

    def test_build_latency_sensitive_domains_filters_broad_google_domains_from_runtime_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text('{"services":{}}', encoding="utf-8")
            runtime = tmp / "latency-catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "services": {
                            "contaminated-service": {
                                "display": "Contaminated",
                                "category": "media",
                                "requires_direct_bootstrap": True,
                                "domains": {
                                    "cdn": [
                                        "www.googleapis.com",
                                        "googleapis.com",
                                        "gstatic.com",
                                        "safe.example",
                                    ]
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            learned = tmp / "latency-learned.txt"
            learned.write_text("www.googleapis.com\nlearned-safe.example\n", encoding="utf-8")
            manual = tmp / "latency-sensitive-direct.txt"
            manual.write_text("gstatic.com\nmanual-safe.example\n", encoding="utf-8")

            with mock.patch.object(update_routes, "REPO_LATENCY_CATALOG", fallback):
                with mock.patch.object(update_routes, "LATENCY_CATALOG", runtime):
                    with mock.patch.object(update_routes, "LATENCY_LEARNED", learned):
                        with mock.patch.object(update_routes, "LATENCY_DIRECT", manual):
                            domains = update_routes.build_latency_sensitive_domains(
                                manual_direct_domains={"googleapis.com", "extra-safe.example"}
                            )

        self.assertNotIn("www.googleapis.com", domains)
        self.assertNotIn("googleapis.com", domains)
        self.assertNotIn("gstatic.com", domains)
        self.assertIn("safe.example", domains)
        self.assertIn("learned-safe.example", domains)
        self.assertIn("manual-safe.example", domains)
        self.assertIn("extra-safe.example", domains)

    def test_load_latency_catalog_sanitizes_broad_google_domains_from_runtime_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fallback = tmp / "latency-catalog-default.json"
            fallback.write_text('{"services":{}}', encoding="utf-8")
            runtime = tmp / "latency-catalog.json"
            runtime.write_text(
                json.dumps(
                    {
                        "services": {
                            "okko": {
                                "display": "Okko",
                                "category": "media",
                                "requires_direct_bootstrap": True,
                                "domains": {
                                    "cdn": ["www.googleapis.com", "googleapis.com", "gstatic.com", "clients-static.okko.tv"],
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(update_routes, "REPO_LATENCY_CATALOG", fallback):
                with mock.patch.object(update_routes, "LATENCY_CATALOG", runtime):
                    catalog = update_routes.load_latency_catalog()

        self.assertEqual(catalog["okko"]["domains"]["cdn"], ["clients-static.okko.tv"])

    def test_render_dnsmasq_latency_sensitive_uses_separate_set(self) -> None:
        content, written = update_routes.render_dnsmasq_latency_sensitive(
            ["okko.tv", "static.okko.tv", "yastatic.net", "invalid domain"]
        )

        self.assertIn("server=/okko.tv/77.88.8.8", content)
        self.assertIn("nftset=/okko.tv/4#inet#vpn#latency_sensitive_direct", content)
        self.assertIn("nftset=/static.okko.tv/4#inet#vpn#latency_sensitive_direct", content)
        self.assertIn("nftset=/yastatic.net/4#inet#vpn#latency_sensitive_direct", content)
        self.assertNotIn("invalid domain", content)
        self.assertEqual(written, 3)

    def test_latency_sensitive_domains_do_not_include_broad_shared_google_cdns(self) -> None:
        domains = update_routes.build_latency_sensitive_domains()

        self.assertNotIn("www.googleapis.com", domains)
        self.assertNotIn("googleapis.com", domains)
        self.assertNotIn("gstatic.com", domains)

    def test_render_dnsmasq_direct_marks_ru_domains_direct_first(self) -> None:
        content = update_routes.render_dnsmasq_direct()

        self.assertIn("server=/.ru/77.88.8.8", content)
        self.assertIn("nftset=/.ru/4#inet#vpn#latency_sensitive_direct", content)
        self.assertIn("nftset=/.xn--p1acf/4#inet#vpn#latency_sensitive_direct", content)


if __name__ == "__main__":
    unittest.main()

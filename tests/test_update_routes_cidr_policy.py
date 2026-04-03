#!/usr/bin/env python3
import importlib.util
import ipaddress
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "scripts" / "update-routes.py"
SPEC = importlib.util.spec_from_file_location("update_routes", MODULE_PATH)
update_routes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update_routes)


class AllowedIpsNormalizationTests(unittest.TestCase):
    def test_splits_only_forbidden_prefixes(self) -> None:
        networks = {
            ipaddress.ip_network("38.0.0.0/7"),
            ipaddress.ip_network("52.0.0.0/8"),
            ipaddress.ip_network("1.0.0.0/9"),
            ipaddress.ip_network("1.192.0.0/10"),
        }

        normalized, stats = update_routes.normalize_allowed_networks(networks)

        self.assertTrue(all(net.prefixlen >= 9 for net in normalized))
        self.assertEqual(stats["forbidden_count"], 0)
        self.assertGreater(stats["conditional_count"], 0)
        self.assertEqual(stats["expansion_ratio"], 1.0)

    def test_preserves_exact_address_space_when_splitting(self) -> None:
        networks = {
            ipaddress.ip_network("10.0.0.0/11"),
            ipaddress.ip_network("10.32.0.0/12"),
        }

        collapsed = update_routes.aggregate_networks(networks)
        normalized, _ = update_routes.normalize_allowed_networks(networks)

        self.assertEqual(
            update_routes.total_address_count(collapsed),
            update_routes.total_address_count(normalized),
        )

    def test_reports_prefix_distribution(self) -> None:
        networks = {
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("11.0.0.0/9"),
            ipaddress.ip_network("11.128.0.0/10"),
        }

        normalized, stats = update_routes.normalize_allowed_networks(networks)

        self.assertEqual(stats["after_distribution"], {9: 3, 10: 1})


class BlockedStaticNormalizationTests(unittest.TestCase):
    def test_splits_too_broad_prefixes_for_nft_blocked_static(self) -> None:
        networks = {
            ipaddress.ip_network("46.0.0.0/8"),
            ipaddress.ip_network("5.0.0.0/11"),
        }

        normalized, stats = update_routes.normalize_nft_blocked_networks(networks)

        self.assertTrue(all(net.prefixlen >= 11 for net in normalized))
        self.assertEqual(stats["too_broad_count"], 0)
        self.assertGreater(stats["split_count"], 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "scripts" / "update-routes.py"
SPEC = importlib.util.spec_from_file_location("update_routes", MODULE_PATH)
update_routes = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update_routes)


class DnsRouteHygieneTests(unittest.TestCase):
    def test_public_dns_anycast_ips_are_not_treated_as_cdn_subnets(self) -> None:
        forbidden = {"1.1.1.1/32", "1.0.0.1/32", "8.8.8.8/32", "8.8.4.4/32"}

        self.assertTrue(forbidden.isdisjoint(update_routes.CDN_SUBNETS))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "home"
    / "telegram-bot"
    / "services"
    / "config_builder.py"
)
SPEC = importlib.util.spec_from_file_location("config_builder", MODULE_PATH)
config_builder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(config_builder)


class ConfigBuilderRoutesTests(unittest.TestCase):
    def test_server_routes_are_included_before_combined_cidr_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            combined_cidr = Path(tmpdir) / "combined.cidr"
            combined_cidr.write_text(
                "203.0.113.0/24\n198.51.100.0/24\n192.168.1.200/32\n",
                encoding="utf-8",
            )
            original = config_builder.COMBINED_CIDR
            config_builder.COMBINED_CIDR = combined_cidr
            try:
                allowed = config_builder._load_allowed_ips(
                    "wg",
                    ["198.51.100.0/24"],
                    ["192.168.1.200/32", "10.10.10.0/24"],
                )
            finally:
                config_builder.COMBINED_CIDR = original

        self.assertEqual(
            allowed,
            [
                "10.177.3.1/32",
                "192.168.1.200/32",
                "10.10.10.0/24",
                "203.0.113.0/24",
            ],
        )

    def test_builder_does_not_inject_public_dns_host_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            combined_cidr = Path(tmpdir) / "combined.cidr"
            combined_cidr.write_text("203.0.113.0/24\n", encoding="utf-8")
            original = config_builder.COMBINED_CIDR
            config_builder.COMBINED_CIDR = combined_cidr
            try:
                allowed = config_builder._load_allowed_ips("awg", [], [])
            finally:
                config_builder.COMBINED_CIDR = original

        self.assertEqual(allowed, ["10.177.1.1/32", "203.0.113.0/24"])
        self.assertNotIn("1.1.1.1/32", allowed)
        self.assertNotIn("8.8.8.8/32", allowed)


if __name__ == "__main__":
    unittest.main()

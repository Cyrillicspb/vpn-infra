#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_mobile_endpoint_uses_literal_ingress_ip_when_wg_host_is_hostname(self) -> None:
        env = {
            "WG_HOST": "myhome.duckdns.org",
            "ROUTER_EXTERNAL_IP": "198.51.100.44",
            "EXTERNAL_IP": "198.51.100.55",
            "AWG_SERVER_PUBLIC_KEY": "server-pub",
        }
        device = {
            "platform": "ios",
            "protocol": "awg",
            "private_key": "client-priv",
            "ip_address": "10.177.1.9",
        }
        with mock.patch.dict(config_builder.os.environ, env, clear=False):
            rendered = config_builder._render(device, ["10.177.1.1/32", "203.0.113.0/24"])

        self.assertIn("Endpoint = 198.51.100.44:51820", rendered)

    def test_desktop_endpoint_keeps_ddns_hostname(self) -> None:
        env = {
            "WG_HOST": "myhome.duckdns.org",
            "ROUTER_EXTERNAL_IP": "198.51.100.44",
            "WG_SERVER_PUBLIC_KEY": "server-pub",
        }
        device = {
            "platform": "windows",
            "protocol": "wg",
            "private_key": "client-priv",
            "ip_address": "10.177.3.9",
        }
        with mock.patch.dict(config_builder.os.environ, env, clear=False):
            rendered = config_builder._render(device, ["10.177.3.1/32", "203.0.113.0/24"])

        self.assertIn("Endpoint = myhome.duckdns.org:51821", rendered)

    def test_wireguard_tunnel_name_is_ascii_safe_for_unicode_device_names(self) -> None:
        tunnel_name = config_builder.make_wireguard_tunnel_name("Телефон сына", "wg", export_date="2026-04-14")

        self.assertRegex(tunnel_name, r"^[a-z0-9_-]+$")
        self.assertTrue(tunnel_name.startswith("wg-"))
        self.assertLessEqual(len(tunnel_name), 15)
        self.assertIn("260414", tunnel_name)

    def test_wireguard_conf_filename_uses_canonical_home_contract(self) -> None:
        filename = config_builder.make_wireguard_conf_filename("MacBook Pro 14", "wg", export_date="2026-04-14")

        self.assertEqual(filename, "vpn-home-wg-macbook-pro-14-2026-04-14.conf")

    def test_build_installer_uses_safe_ascii_tunnel_name(self) -> None:
        installer = config_builder.build_installer("Телефон сына", "[Interface]\n", "windows", protocol="wg")

        self.assertIsNotNone(installer)
        script = installer.decode("utf-8")
        self.assertIn("set TUNNEL_NAME=wg-", script)
        self.assertNotIn("Телефон", script)

    def test_direct_export_filename_is_ascii_and_includes_backend_and_date(self) -> None:
        filename = config_builder.make_direct_export_filename(
            "MacBook Pro 14",
            "vless-reality-vision",
            "backend-eu-1",
            "json",
            export_date="2026-04-14",
        )
        self.assertEqual(
            filename,
            "vpn-backend-vless-reality-vision-backend-eu-1-macbook-pro-14-2026-04-14.json",
        )

    def test_qr_filename_uses_canonical_ascii_contract(self) -> None:
        filename = config_builder.make_qr_filename("Phone 1", "home", "awg", export_date="2026-04-14")
        self.assertEqual(filename, "vpn-qr-home-awg-phone-1-2026-04-14.png")


if __name__ == "__main__":
    unittest.main()

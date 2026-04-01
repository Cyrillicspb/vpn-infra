#!/usr/bin/env python3
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "watchdog" / "plugins" / "zapret" / "client.py"
SPEC = importlib.util.spec_from_file_location("zapret_client", MODULE_PATH)
zapret_client = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(zapret_client)


class ZapretGatewayRulesTests(unittest.TestCase):
    def test_gateway_mode_includes_lan_interface(self) -> None:
        env = {
            "SERVER_MODE": "gateway",
            "LAN_IFACE": "br0",
            "NET_INTERFACE": "eth9",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(zapret_client, "ETH_IFACE", "eth9"):
                script = zapret_client.build_nft_forward_script()

        self.assertIn('iifname { "wg0", "wg1", "br0" }', script)
        self.assertIn('oifname "eth9"', script)

    def test_hosted_mode_keeps_wireguard_only(self) -> None:
        env = {
            "SERVER_MODE": "hosted",
            "LAN_IFACE": "br0",
            "NET_INTERFACE": "eth9",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(zapret_client, "ETH_IFACE", "eth9"):
                script = zapret_client.build_nft_forward_script()

        self.assertIn('iifname { "wg0", "wg1" }', script)
        self.assertNotIn('"br0"', script)


if __name__ == "__main__":
    unittest.main()

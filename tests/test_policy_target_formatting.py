#!/usr/bin/env python3
import ast
import importlib.util
import ipaddress
import tempfile
import unittest
from pathlib import Path


CLIENT_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "home"
    / "telegram-bot"
    / "handlers"
    / "client.py"
)


def _load_normalize_policy_target():
    source = CLIENT_MODULE_PATH.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(CLIENT_MODULE_PATH))
    func_node = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_normalize_policy_target"
    )
    isolated = ast.Module(body=[func_node], type_ignores=[])
    code = compile(isolated, filename=str(CLIENT_MODULE_PATH), mode="exec")
    namespace = {"ipaddress": ipaddress}
    exec(code, namespace)
    return namespace["_normalize_policy_target"]


normalize_policy_target = _load_normalize_policy_target()

CONFIG_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "home"
    / "telegram-bot"
    / "services"
    / "config_builder.py"
)
CONFIG_SPEC = importlib.util.spec_from_file_location("config_builder", CONFIG_MODULE_PATH)
config_builder = importlib.util.module_from_spec(CONFIG_SPEC)
assert CONFIG_SPEC.loader is not None
CONFIG_SPEC.loader.exec_module(config_builder)


class PolicyTargetFormattingTests(unittest.TestCase):
    def test_policy_target_preserves_host_ip_without_mask(self) -> None:
        self.assertEqual(normalize_policy_target("192.168.1.202"), "192.168.1.202")

    def test_policy_target_preserves_explicit_cidr(self) -> None:
        self.assertEqual(normalize_policy_target("192.168.1.0/24"), "192.168.1.0/24")

    def test_builder_converts_plain_ip_to_host_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            combined_cidr = Path(tmpdir) / "combined.cidr"
            combined_cidr.write_text("192.168.1.202/32\n203.0.113.0/24\n", encoding="utf-8")
            original = config_builder.COMBINED_CIDR
            config_builder.COMBINED_CIDR = combined_cidr
            try:
                allowed = config_builder._load_allowed_ips(
                    "wg",
                    ["192.168.1.202"],
                    ["198.51.100.77"],
                )
            finally:
                config_builder.COMBINED_CIDR = original

        self.assertEqual(
            allowed,
            [
                "10.177.3.1/32",
                "198.51.100.77/32",
                "203.0.113.0/24",
            ],
        )


if __name__ == "__main__":
    unittest.main()

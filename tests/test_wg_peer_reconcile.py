import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from home.watchdog import watchdog


class WgPeerReconcileTests(IsolatedAsyncioTestCase):
    async def test_reconcile_restores_missing_runtime_peer_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vpn_bot.db"
            db_path.write_text("", encoding="utf-8")

            def _fake_read_expected() -> list[dict[str, str]]:
                return [
                    {
                        "device_name": "iPhone WG",
                        "protocol": "wg",
                        "public_key": "pub-key-1",
                        "ip_address": "10.177.3.2",
                    }
                ]

            runtime_dumps = [
                [],
                [
                    {
                        "interface": "wg1",
                        "public_key": "pub-key-1",
                        "allowed_ips": "10.177.3.2/32",
                    }
                ],
            ]

            async def _fake_runtime_dump() -> list[dict]:
                return runtime_dumps.pop(0)

            calls: list[list[str]] = []

            async def _fake_run(cmd: list[str], timeout: int = 30, check: bool = False):
                calls.append(cmd)
                return 0, "", ""

            with mock.patch.object(watchdog, "_read_expected_device_peers", side_effect=_fake_read_expected), \
                 mock.patch.object(watchdog, "_runtime_peer_dump", side_effect=_fake_runtime_dump), \
                 mock.patch.object(watchdog, "run_cmd", side_effect=_fake_run), \
                 mock.patch.object(watchdog, "alert"):
                await watchdog.reconcile_wg_runtime_from_db()

            self.assertIn(
                ["wg", "set", "wg1", "peer", "pub-key-1", "allowed-ips", "10.177.3.2/32"],
                calls,
            )
            self.assertIn(["wg-quick", "save", "wg1"], calls)

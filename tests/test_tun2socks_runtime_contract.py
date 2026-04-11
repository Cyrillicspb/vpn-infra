from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from home.watchdog.plugins.base import BasePlugin


class _DummyPlugin(BasePlugin):
    name = "dummy"

    async def start(self) -> int:
        return 0

    async def stop(self) -> int:
        return 0

    async def test(self) -> int:
        return 0

    async def activate(self) -> int:
        return 0

    async def deactivate(self) -> int:
        return 0


class Tun2SocksRuntimeContractTests(IsolatedAsyncioTestCase):
    async def test_stop_keeps_env_file_and_resets_failed_unit(self) -> None:
        plugin = _DummyPlugin()
        tun_name = "tun-demo"
        env_path = plugin.tun2socks_env_path(tun_name)
        meta_path = plugin.tun2socks_meta_path(tun_name)

        with mock.patch("home.watchdog.plugins.base.TUN2SOCKS_STACK_RUNTIME_DIR", Path(self.id())):
            env_path = plugin.tun2socks_env_path(tun_name)
            meta_path = plugin.tun2socks_meta_path(tun_name)
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("TUN2SOCKS_IFACE=tun-demo\n", encoding="utf-8")
            meta_path.write_text("{}", encoding="utf-8")

            async def _fake_run(cmd: list[str], timeout: int = 30, check: bool = False) -> tuple[int, str, str]:
                return 0, "", ""

            with mock.patch.object(plugin, "run_cmd", side_effect=_fake_run) as run_cmd:
                ok = await plugin.stop_tun2socks_service(tun_name)

            self.assertTrue(ok)
            self.assertTrue(env_path.exists())
            self.assertFalse(meta_path.exists())
            self.assertEqual(
                run_cmd.await_args_list[0].args[0],
                ["systemctl", "stop", "tun2socks-stack@tun-demo.service"],
            )
            self.assertEqual(
                run_cmd.await_args_list[1].args[0],
                ["systemctl", "reset-failed", "tun2socks-stack@tun-demo.service"],
            )

#!/usr/bin/env python3
"""
VLESS+REALITY+XHTTP плагин для watchdog.
Использует xray-client-xhttp и порт 1081.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from base import BasePlugin

SOCKS_PORT = 1081
TUN_IFACE = "tun-xhttp"
TUN_TMP   = "tun-xhttp-tmp"
CONTAINER_NAME = "xray-client-xhttp"


class RealityXhttpPlugin(BasePlugin):
    name = "reality-xhttp"
    pid_file = Path("/run/tun2socks-xhttp.pid")
    _pid_file_tmp = Path("/run/tun2socks-xhttp-tmp.pid")

    async def start(self, temp_port: str = "") -> int:
        tun_name = TUN_TMP if temp_port else TUN_IFACE
        pf = self._pid_file_tmp if temp_port else self.pid_file

        # Запускаем xray-client-xhttp
        rc, stdout, _ = await self.run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME]
        )
        if "true" not in stdout.lower():
            rc, _, err = await self.run_cmd(["docker", "start", CONTAINER_NAME], timeout=15)
            if rc != 0:
                print(json.dumps({"status": "error", "message": f"Не удалось запустить {CONTAINER_NAME}: {err}"}))
                return 1

        # Ждём SOCKS порт
        for _ in range(30):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
            if rc == 0:
                break

        # Запускаем tun2socks с PID tracking
        proc = await self.start_process([
            "/usr/local/bin/tun2socks",
            "-device", tun_name,
            "-proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
            "-loglevel", "warn",
        ], pid_file=pf)
        if proc is None:
            return 1

        for _ in range(15):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["ip", "link", "show", tun_name])
            if rc == 0:
                await self.run_cmd(["ip", "link", "set", tun_name, "up"])
                print(json.dumps({"status": "started", "tun": tun_name}))
                return 0

        print(json.dumps({"status": "error", "message": "tun not created"}))
        await self.stop_process(pf)
        return 1

    async def stop(self) -> int:
        await self.stop_process(self.pid_file)
        await self.stop_process(self._pid_file_tmp)
        # Fallback для процессов запущенных до PID tracking
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"], timeout=5)
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"], timeout=5)
        return 0

    async def test(self) -> int:
        rc, _, _ = await self.run_cmd(["ip", "link", "show", TUN_IFACE])
        if rc != 0:
            print(json.dumps({"status": "fail", "throughput_mbps": 0, "reason": "tun down"}))
            return 1
        start_time = time.time()
        rc, stdout, _ = await self.run_cmd(
            ["curl", "-s", "--max-time", "10",
             "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
             "-o", "/dev/null", "-w", "%{http_code}",
             "https://youtube.com"],
            timeout=15,
        )
        if rc == 0 and stdout.strip() in ["200", "301", "302"]:
            elapsed = time.time() - start_time
            throughput = 10.0 / max(elapsed, 0.1)
            print(json.dumps({"status": "ok", "throughput_mbps": round(min(throughput, 100), 2)}))
            return 0
        print(json.dumps({"status": "fail", "throughput_mbps": 0}))
        return 1

    async def activate(self) -> int:
        return 0

    async def deactivate(self) -> int:
        return 0


_p = RealityXhttpPlugin()


async def start(temp_port: str = "") -> int:
    return await _p.start(temp_port)

async def stop() -> int:
    return await _p.stop()

async def test() -> int:
    return await _p.test()

async def rotate() -> int:
    await _p.stop()
    await asyncio.sleep(2)
    return await _p.start()


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    temp_port = ""
    for arg in sys.argv[2:]:
        if arg.startswith("--temp-port="):
            temp_port = arg.split("=")[1]
    if cmd == "start":
        sys.exit(await start(temp_port))
    elif cmd == "stop":
        sys.exit(await stop())
    elif cmd == "test":
        sys.exit(await test())
    elif cmd == "rotate":
        sys.exit(await rotate())
    else:
        print(f"Неизвестная команда: {cmd}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Hysteria2 плагин для watchdog.
Hysteria2 client запущен через systemd (hysteria2.service), SOCKS5 :1083.
Плагин управляет только tun2socks поверх SOCKS5.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from base import BasePlugin

SOCKS_PORT = 1083
TUN_IFACE  = "tun-hysteria2"
TUN_TMP    = "tun-hysteria2-t"   # max 15 chars


class Hysteria2Plugin(BasePlugin):
    name = "hysteria2"
    pid_file = Path("/run/tun2socks-hysteria2.pid")
    _pid_file_tmp = Path("/run/tun2socks-hysteria2-t.pid")

    async def start(self, temp_port: str = "") -> int:
        tun_name = TUN_TMP if temp_port else TUN_IFACE
        pf = self._pid_file_tmp if temp_port else self.pid_file

        # Убеждаемся что hysteria2 systemd service запущен
        rc, _, _ = await self.run_cmd(["systemctl", "is-active", "hysteria2"])
        if rc != 0:
            await self.run_cmd(["systemctl", "start", "hysteria2"], timeout=15)

        # Ждём SOCKS порт :1083
        for _ in range(30):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
            if rc == 0:
                break
        else:
            print(json.dumps({"status": "error", "message": "SOCKS port not ready"}))
            return 1

        # Запускаем tun2socks с PID tracking
        proc = await self.start_process([
            "/usr/local/bin/tun2socks",
            "-device", tun_name,
            "-proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
            "-loglevel", "warning",
        ], pid_file=pf)
        if proc is None:
            return 1

        # Ждём появления TUN интерфейса
        for _ in range(15):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["ip", "link", "show", tun_name])
            if rc == 0:
                await self.run_cmd(["ip", "link", "set", tun_name, "up"])
                print(json.dumps({"status": "started", "tun": tun_name}))
                return 0

        print(json.dumps({"status": "error", "message": "tun interface not created"}))
        await self.stop_process(pf)
        return 1

    async def stop(self) -> int:
        await self.stop_process(self.pid_file)
        await self.stop_process(self._pid_file_tmp)
        # Fallback для процессов запущенных до PID tracking
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"], timeout=5)
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"], timeout=5)
        # hysteria2.service не останавливаем — может понадобиться при следующем старте
        return 0

    async def test(self) -> int:
        """Тест работоспособности стека через SOCKS5 с timeout."""
        rc, stdout, _ = await self.run_cmd(
            ["curl", "-s", "--max-time", "10",
             "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
             "-o", "/dev/null", "-w", "%{http_code}",
             "http://www.gstatic.com/generate_204"],
            timeout=15,
        )
        if rc != 0 or stdout.strip() not in ["200", "204", "301", "302"]:
            print(json.dumps({"status": "fail", "throughput_mbps": 0}))
            return 1

        # Замеряем throughput (1 MB, с timeout)
        start_t = time.time()
        rc2, _, _ = await self.run_cmd(
            ["curl", "-s", "--max-time", "15",
             "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
             "-o", "/dev/null",
             "https://speed.cloudflare.com/__down?bytes=1048576"],
            timeout=20,
        )
        elapsed = time.time() - start_t
        throughput = round((1048576 * 8 / 1_000_000) / max(elapsed, 0.1), 2)
        print(json.dumps({"status": "ok", "throughput_mbps": throughput}))
        return 0

    async def activate(self) -> int:
        return 0

    async def deactivate(self) -> int:
        return 0


_p = Hysteria2Plugin()


async def start(temp_port: str = "") -> int:
    return await _p.start(temp_port)

async def stop() -> int:
    return await _p.stop()

async def test() -> int:
    return await _p.test()

async def rotate() -> int:
    """Ротация: перезапуск tun2socks (make-before-break через temp tun)."""
    await _p.stop()
    await asyncio.sleep(1)
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

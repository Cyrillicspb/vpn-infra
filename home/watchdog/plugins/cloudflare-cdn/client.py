#!/usr/bin/env python3
"""
Cloudflare CDN плагин для watchdog (Workers вариант).
Поток: tun2socks → xray-client-cdn (VLESS+XHTTP+TLS) → Cloudflare Worker → VPS Xray XHTTP :8080

Настройка: CF_CDN_HOSTNAME в .env → config-cdn.json генерируется setup.sh / deploy.sh
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from base import BasePlugin

SOCKS_PORT = 1082
TUN_IFACE  = "tun-cf-cdn"    # 10 chars — в лимите Linux 15
TUN_TMP    = "tun-cf-cdn-t"  # 12 chars
CONTAINER_NAME = "xray-client-cdn"


class CloudflareCdnPlugin(BasePlugin):
    name = "cloudflare-cdn"
    pid_file = Path("/run/tun2socks-cf-cdn.pid")
    _pid_file_tmp = Path("/run/tun2socks-cf-cdn-t.pid")

    async def start(self, temp_port: str = "") -> int:
        """Запуск CDN стека: xray-client-cdn + tun2socks."""
        tun_name = TUN_TMP if temp_port else TUN_IFACE

        # 1. Запускаем xray-client-cdn если не запущен
        rc, stdout, _ = await self.run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME]
        )
        if "true" not in stdout.lower():
            rc, _, err = await self.run_cmd(["docker", "start", CONTAINER_NAME], timeout=15)
            if rc != 0:
                print(json.dumps({"status": "error", "message": f"Не удалось запустить {CONTAINER_NAME}: {err}"}))
                return 1

        # Ждём SOCKS5 порт
        for _ in range(30):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
            if rc == 0:
                break
        else:
            print(json.dumps({"status": "error", "message": f"SOCKS5 :{SOCKS_PORT} недоступен"}))
            return 1

        # 2. Запускаем tun2socks через systemd для auto-restart и journald
        ok, pid = await self.start_tun2socks_service(tun_name, SOCKS_PORT, timeout=20)
        if not ok:
            return 1

        # Ждём tun интерфейс
        for _ in range(15):
            await asyncio.sleep(1)
            rc, _, _ = await self.run_cmd(["ip", "link", "show", tun_name])
            if rc == 0:
                await self.run_cmd(["ip", "link", "set", tun_name, "up"])
                print(json.dumps({"status": "started", "tun": tun_name, "pid": pid}))
                return 0

        print(json.dumps({"status": "error", "message": "tun interface not created"}))
        await self.stop_tun2socks_service(tun_name)
        return 1

    async def stop(self) -> int:
        """Остановка CDN стека."""
        await self.stop_tun2socks_service(TUN_IFACE)
        await self.stop_tun2socks_service(TUN_TMP)
        # Fallback для процессов, запущенных до перехода на systemd-managed tun2socks
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"], timeout=5)
        await self.run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"], timeout=5)
        await asyncio.sleep(1)
        return 0

    async def test(self) -> int:
        """Тест CDN стека: проверка connectivity + реальный замер throughput."""
        rc, _, _ = await self.run_cmd(["ip", "link", "show", TUN_IFACE])
        if rc != 0:
            print(json.dumps({"status": "fail", "throughput_mbps": 0, "reason": "tun down"}))
            return 1
        rc, stdout, _ = await self.run_cmd(
            ["curl", "-s", "--max-time", "10",
             "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
             "-o", "/dev/null", "-w", "%{http_code}",
             "https://youtube.com"],
            timeout=15,
        )
        if rc != 0 or stdout.strip() not in ["200", "301", "302"]:
            print(json.dumps({"status": "fail", "throughput_mbps": 0}))
            return 1

        # Реальный замер throughput (1 MB)
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


_p = CloudflareCdnPlugin()


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

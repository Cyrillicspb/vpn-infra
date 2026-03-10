#!/usr/bin/env python3
"""
Cloudflare CDN плагин для watchdog.
Управляет cloudflared + xray-client + tun2socks.
"""
import asyncio
import json
import subprocess
import sys
import time

SOCKS_PORT = 1080
TUN_IFACE = "tun-cloudflare-cdn"
CONTAINER_CLOUDFLARED = "cloudflared"
CONTAINER_XRAY = "xray-client"


async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def start(temp_port: str = ""):
    tun_name = f"tun-cf-tmp" if temp_port else TUN_IFACE
    socks_port = int(temp_port) if temp_port else SOCKS_PORT

    # 1. Запускаем cloudflared (домашний)
    await run_cmd(["docker", "start", CONTAINER_CLOUDFLARED], timeout=15)
    await asyncio.sleep(3)

    # 2. Запускаем xray-client
    rc, stdout, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_XRAY])
    if "true" not in stdout.lower():
        await run_cmd(["docker", "start", CONTAINER_XRAY], timeout=15)

    # Ждём SOCKS порт
    for _ in range(30):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
        if rc == 0:
            break

    # 3. tun2socks
    proc = subprocess.Popen([
        "/usr/local/bin/tun2socks",
        "-device", tun_name,
        "-proxy", f"socks5://127.0.0.1:{socks_port}",
        "-loglevel", "warning",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name])
        if rc == 0:
            await run_cmd(["ip", "link", "set", tun_name, "up"])
            print(json.dumps({"status": "started", "tun": tun_name}))
            return 0

    proc.terminate()
    print(json.dumps({"status": "error", "message": "tun not created"}))
    return 1


async def stop():
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"])
    # cloudflared останавливаем только при явном запросе (он нужен для тестирования)
    return 0


async def test() -> int:
    """Тест CDN стека — самый устойчивый, тестируем последним."""
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "15",
         "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "https://youtube.com"],
        timeout=20,
    )
    if rc == 0 and stdout.strip() in ["200", "301", "302"]:
        print(json.dumps({"status": "ok", "throughput_mbps": 5.0}))
        return 0
    print(json.dumps({"status": "fail", "throughput_mbps": 0}))
    return 1


async def rotate():
    await stop()
    await asyncio.sleep(2)
    return await start()


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

if __name__ == "__main__":
    asyncio.run(main())

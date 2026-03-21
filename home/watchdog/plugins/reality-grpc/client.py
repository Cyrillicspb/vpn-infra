#!/usr/bin/env python3
"""
VLESS+REALITY+gRPC плагин для watchdog.
Аналог reality/client.py, но использует xray-client-2 и порт 1081.
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

SOCKS_PORT = 1081
TUN_IFACE = "tun-grpc"      # max 15 chars (Linux netdev limit)
TUN_TMP   = "tun-grpc-tmp"
CONTAINER_NAME = "xray-client-2"


async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def start(temp_port: str = ""):
    tun_name = TUN_TMP if temp_port else TUN_IFACE
    socks_port = SOCKS_PORT  # always use own SOCKS port; temp_port is just a signal for tmp tun name

    # Запускаем xray-client-2
    rc, stdout, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME])
    if "true" not in stdout.lower():
        await run_cmd(["docker", "start", CONTAINER_NAME], timeout=15)

    # Ждём SOCKS порт
    for _ in range(30):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
        if rc == 0:
            break

    # Запускаем tun2socks
    proc = subprocess.Popen([
        "/usr/local/bin/tun2socks",
        "-device", tun_name,
        "-proxy", f"socks5h://127.0.0.1:{socks_port}",
        "-loglevel", "warning",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name])
        if rc == 0:
            await run_cmd(["ip", "link", "set", tun_name, "up"])
            print(json.dumps({"status": "started", "tun": tun_name}))
            return 0

    print(json.dumps({"status": "error", "message": "tun not created"}))
    proc.terminate()
    return 1


async def stop():
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"])
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"])
    return 0


async def test() -> int:
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10",
         "--proxy", f"socks5h://127.0.0.1:{SOCKS_PORT}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "https://youtube.com"],
        timeout=15,
    )
    if rc == 0 and stdout.strip() in ["200", "301", "302"]:
        print(json.dumps({"status": "ok", "throughput_mbps": 8.0}))
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

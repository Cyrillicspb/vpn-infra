#!/usr/bin/env python3
"""
VLESS+REALITY плагин для watchdog.
Управляет xray-client Docker контейнером и tun2socks.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
SOCKS_PORT = 1080
TUN_IFACE = "tun-reality"    # 11 chars, within Linux 15-char limit
TUN_TMP   = "tun-reality-t"  # 13 chars
CONTAINER_NAME = "xray-client"


async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def start(temp_port: str = ""):
    """Запуск REALITY стека: xray-client + tun2socks."""
    tun_name = TUN_TMP if temp_port else TUN_IFACE
    socks_port = SOCKS_PORT  # always use own SOCKS port; temp_port is just a signal for tmp tun name

    # 1. Запускаем xray-client если не запущен
    rc, stdout, _ = await run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME])
    if "true" not in stdout.lower():
        rc, _, err = await run_cmd(["docker", "start", CONTAINER_NAME], timeout=15)
        if rc != 0:
            print(json.dumps({"status": "error", "message": f"Не удалось запустить {CONTAINER_NAME}: {err}"}))
            return 1

    # Ждём SOCKS5 порт
    for _ in range(30):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
        if rc == 0:
            break

    # 2. Запускаем tun2socks
    proc = subprocess.Popen([
        "/usr/local/bin/tun2socks",
        "-device", tun_name,
        "-proxy", f"socks5://127.0.0.1:{socks_port}",
        "-loglevel", "warning",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Ждём tun интерфейс
    for _ in range(15):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name])
        if rc == 0:
            # Поднимаем интерфейс
            await run_cmd(["ip", "link", "set", tun_name, "up"])
            print(json.dumps({"status": "started", "tun": tun_name, "pid": proc.pid}))
            return 0

    print(json.dumps({"status": "error", "message": "tun interface not created"}))
    proc.terminate()
    return 1


async def stop():
    """Остановка REALITY стека."""
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"])
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"])
    await asyncio.sleep(1)
    return 0


async def test() -> int:
    """Тест работоспособности стека."""
    start_time = time.time()
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10",
         "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "https://youtube.com"],
        timeout=15,
    )

    if rc == 0 and stdout.strip() in ["200", "301", "302"]:
        elapsed = time.time() - start_time
        # Грубая оценка throughput
        throughput = 10.0 / max(elapsed, 0.1)
        print(json.dumps({"status": "ok", "throughput_mbps": round(min(throughput, 100), 2)}))
        return 0

    print(json.dumps({"status": "fail", "throughput_mbps": 0}))
    return 1


async def rotate():
    """Ротация соединения."""
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
    else:
        print(f"Неизвестная команда: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

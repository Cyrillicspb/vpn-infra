#!/usr/bin/env python3
"""
Cloudflare CDN плагин для watchdog (Workers вариант).
Поток: tun2socks → xray-client-cdn (VLESS+WS+TLS) → Cloudflare Worker → VPS Xray WS :8080

Настройка: CF_CDN_HOSTNAME в .env → config-cdn.json генерируется setup.sh / deploy.sh
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

SOCKS_PORT = 1082
TUN_IFACE  = "tun-cf-cdn"    # 10 chars — в лимите Linux 15
TUN_TMP    = "tun-cf-cdn-t"  # 12 chars
CONTAINER_NAME = "xray-client-cdn"


async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def start(temp_port: str = ""):
    """Запуск CDN стека: xray-client-cdn + tun2socks."""
    tun_name = TUN_TMP if temp_port else TUN_IFACE

    # 1. Запускаем xray-client-cdn если не запущен
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
    else:
        print(json.dumps({"status": "error", "message": f"SOCKS5 :{SOCKS_PORT} недоступен"}))
        return 1

    # 2. Запускаем tun2socks
    proc = subprocess.Popen([
        "/usr/local/bin/tun2socks",
        "-device", tun_name,
        "-proxy", f"socks5h://127.0.0.1:{SOCKS_PORT}",
        "-loglevel", "warning",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Ждём tun интерфейс
    for _ in range(15):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name])
        if rc == 0:
            await run_cmd(["ip", "link", "set", tun_name, "up"])
            print(json.dumps({"status": "started", "tun": tun_name, "pid": proc.pid}))
            return 0

    print(json.dumps({"status": "error", "message": "tun interface not created"}))
    proc.terminate()
    return 1


async def stop():
    """Остановка CDN стека."""
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"])
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"])
    await asyncio.sleep(1)
    return 0


async def test() -> int:
    """Тест CDN стека через SOCKS5 :1082."""
    start_time = time.time()
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "15",
         "--proxy", f"socks5h://127.0.0.1:{SOCKS_PORT}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "https://youtube.com"],
        timeout=20,
    )
    if rc == 0 and stdout.strip() in ["200", "301", "302"]:
        elapsed = time.time() - start_time
        throughput = 10.0 / max(elapsed, 0.1)
        print(json.dumps({"status": "ok", "throughput_mbps": round(min(throughput, 100), 2)}))
        return 0
    print(json.dumps({"status": "fail", "throughput_mbps": 0}))
    return 1


async def rotate():
    """Ротация CDN соединения."""
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

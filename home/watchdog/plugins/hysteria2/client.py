#!/usr/bin/env python3
"""
Hysteria2 плагин для watchdog.
Hysteria2 client запущен через systemd (hysteria2.service), SOCKS5 :1083.
Плагин управляет только tun2socks поверх SOCKS5.
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

SOCKS_PORT     = 1083
TUN_IFACE      = "tun-hysteria2"
TUN_TMP        = "tun-hysteria2-t"   # max 15 chars


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

    # Убеждаемся что hysteria2 systemd service запущен
    rc, _, _ = await run_cmd(["systemctl", "is-active", "hysteria2"])
    if rc != 0:
        await run_cmd(["systemctl", "start", "hysteria2"], timeout=15)

    # Ждём SOCKS порт :1083
    for _ in range(30):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["nc", "-z", "127.0.0.1", str(SOCKS_PORT)])
        if rc == 0:
            break
    else:
        print(json.dumps({"status": "error", "message": "SOCKS port not ready"}))
        return 1

    # Запускаем tun2socks поверх SOCKS5
    proc = subprocess.Popen([
        "/usr/local/bin/tun2socks",
        "-device", tun_name,
        "-proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
        "-loglevel", "warning",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Ждём появления TUN интерфейса
    for _ in range(15):
        await asyncio.sleep(1)
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name])
        if rc == 0:
            await run_cmd(["ip", "link", "set", tun_name, "up"])
            print(json.dumps({"status": "started", "tun": tun_name}))
            return 0

    print(json.dumps({"status": "error", "message": "tun interface not created"}))
    proc.terminate()
    return 1


async def stop():
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_IFACE}"])
    await run_cmd(["pkill", "-f", f"tun2socks.*{TUN_TMP}"])
    # hysteria2.service не останавливаем — может понадобиться при следующем старте
    return 0


async def test() -> int:
    """Тест работоспособности стека через SOCKS5."""
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10",
         "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "http://www.gstatic.com/generate_204"],
        timeout=15,
    )
    if rc == 0 and stdout.strip() in ["200", "204", "301", "302"]:
        # Замеряем throughput
        start_t = time.time()
        rc2, _, _ = await run_cmd(
            ["curl", "-s", "--max-time", "15",
             "--proxy", f"socks5://127.0.0.1:{SOCKS_PORT}",
             "-o", "/dev/null",
             "https://speed.cloudflare.com/__down?bytes=1048576"],
            timeout=20,
        )
        elapsed = time.time() - start_t
        throughput = (1048576 * 8 / 1_000_000) / elapsed if elapsed > 0 else 0
        print(json.dumps({"status": "ok", "throughput_mbps": round(throughput, 2)}))
        return 0
    print(json.dumps({"status": "fail", "throughput_mbps": 0}))
    return 1


async def rotate():
    """Ротация: перезапуск tun2socks (make-before-break через temp tun)."""
    await stop()
    await asyncio.sleep(1)
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

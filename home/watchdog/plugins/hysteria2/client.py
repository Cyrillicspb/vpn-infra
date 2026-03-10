#!/usr/bin/env python3
"""
Hysteria2 плагин для watchdog.
Управляет Hysteria2 клиентом на домашнем сервере.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
CONFIG_FILE = PLUGIN_DIR / "client.yaml"
TUN_IFACE = "tun-hysteria2"
PID_FILE = "/var/run/hysteria2-client.pid"


async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def start(temp_port: str = ""):
    """Запуск Hysteria2 клиента."""
    # Проверяем уже запущен ли
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            print(json.dumps({"status": "already_running", "pid": pid}))
            return 0
        except ProcessLookupError:
            pass

    # Подставляем переменные окружения в конфиг
    config_content = CONFIG_FILE.read_text()
    for key in ["HYSTERIA2_SERVER", "HYSTERIA2_AUTH", "HYSTERIA2_OBFS_PASSWORD"]:
        config_content = config_content.replace(f"${{{key}}}", os.getenv(key, ""))

    # Временный конфиг
    tmp_config = "/tmp/hysteria2-client.yaml"
    with open(tmp_config, "w") as f:
        f.write(config_content)

    # Запускаем hysteria2
    proc = subprocess.Popen(
        ["/usr/local/bin/hysteria", "client", "--config", tmp_config],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    # Ждём поднятия tun интерфейса
    for _ in range(30):
        await asyncio.sleep(1)
        rc, stdout, _ = await run_cmd(["ip", "link", "show", TUN_IFACE])
        if rc == 0:
            print(json.dumps({"status": "started", "pid": proc.pid, "tun": TUN_IFACE}))
            return 0

    print(json.dumps({"status": "error", "message": "tun interface not created"}))
    return 1


async def stop():
    """Остановка Hysteria2 клиента."""
    if not os.path.exists(PID_FILE):
        return 0
    with open(PID_FILE) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.unlink(PID_FILE)
    return 0


async def test() -> int:
    """Тест работоспособности стека."""
    # Проверяем доступность VPS через Hysteria2
    rc, stdout, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10",
         "--proxy", "socks5://127.0.0.1:1083",
         "-o", "/dev/null", "-w", "%{http_code}",
         "https://youtube.com"],
        timeout=15,
    )

    if rc == 0 and stdout.strip() in ["200", "301", "302"]:
        # Замеряем throughput
        start = time.time()
        rc2, _, _ = await run_cmd(
            ["curl", "-s", "--max-time", "15",
             "--proxy", "socks5://127.0.0.1:1083",
             "-o", "/dev/null",
             "https://speed.cloudflare.com/__down?bytes=1048576"],
            timeout=20,
        )
        elapsed = time.time() - start
        throughput = (1048576 * 8 / 1_000_000) / elapsed if elapsed > 0 else 0

        print(json.dumps({"status": "ok", "throughput_mbps": round(throughput, 2)}))
        return 0
    else:
        print(json.dumps({"status": "fail", "throughput_mbps": 0}))
        return 1


async def rotate():
    """Ротация соединения (make-before-break)."""
    # Перезапускаем hysteria2 клиент
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

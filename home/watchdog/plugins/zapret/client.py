#!/usr/bin/env python3
"""
zapret плагин для watchdog.
Управляет nfqws (netfilter queue worker) — Linux-аналог GoodbyeDPI.

Режим работы:
  - direct_mode: трафик идёт через eth0 напрямую (без VPS).
  - nfqueue перехватывает пакеты FORWARD chain (от wg0/wg1 к eth0)
    и применяет DPI-bypass техники (fake/split/TTL манипуляции).
  - Параметры выбираются через Thompson Sampling (probe.py).

Команды:
  start [--temp]  — запуск (--temp: только nfqws+nft, без изменения routing)
  stop            — остановка
  test            — проверка работоспособности
  rotate          — re-probe + перезапуск с новыми параметрами
  probe [full]    — запуск адаптивного поиска параметров
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
NFQWS_BIN   = "/usr/local/bin/nfqws"
NFQUEUE_NUM = 200           # Основная очередь — должна совпадать с nft правилом zapret_main
PID_FILE    = Path("/run/nfqws-main.pid")
ETH_IFACE   = os.getenv("NET_INTERFACE", "eth0")
PLUGIN_DIR  = Path(__file__).parent

# Серверы для замера пропускной способности — российские ISP, не блокируются
DIRECT_TEST_SERVERS = [
    "http://speedtest.corbina.ru/speedtest/random4000x4000.jpg",   # Beeline/Corbina ~4 MB
    "http://speedtest.corbina.ru/speedtest/random1000x1000.jpg",   # Beeline/Corbina ~1 MB
    "https://speedtest.megafon.ru/speedtest/random1000x1000.jpg",  # МегаФон ~1 MB
]


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
async def run_cmd(cmd: list, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", "timeout"


def _get_best_preset() -> dict:
    """Получить лучший пресет от probe.py (или дефолтный если probe не запускался)."""
    try:
        result = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "best"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    # Дефолтный пресет (C01: fake+badsum+ttl8 — наиболее универсальный)
    return {
        "id": "C01",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=8", "--dpi-desync-fooling=badsum"],
        "desc": "fake+badsum+ttl8 (default)",
    }


def _nfqws_args(preset: dict) -> list[str]:
    return [
        NFQWS_BIN,
        "--daemon",
        f"--pidfile={PID_FILE}",
        "--user=daemon",
        f"--qnum={NFQUEUE_NUM}",
    ] + preset["args"]


async def _stop_nfqws() -> None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            await run_cmd(["kill", str(pid)], timeout=3)
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    # На случай если pidfile устарел
    await run_cmd(["pkill", "-f", f"nfqws.*--pidfile={PID_FILE}"], timeout=3)


async def _nft_add_rules() -> None:
    """
    Добавить nft таблицу zapret_main:
      Перехватывает TCP 443/80 от VPN клиентов (wg0/wg1) выходящих через eth0.
    """
    nft_script = f"""
table inet zapret_main {{
    chain forward {{
        type filter hook forward priority filter + 1;
        iifname {{ "wg0", "wg1" }} oifname "{ETH_IFACE}" tcp dport {{ 80, 443 }} queue num {NFQUEUE_NUM} bypass
    }}
}}
"""
    proc = await asyncio.create_subprocess_exec(
        "nft", "-f", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate(input=nft_script.encode())
    if proc.returncode != 0:
        print(f"[zapret] nft add rules: {err.decode().strip()}", file=sys.stderr)


async def _nft_del_rules() -> None:
    """Удалить nft таблицу zapret_main."""
    await run_cmd(["nft", "delete", "table", "inet", "zapret_main"], timeout=5)


def _check_binary() -> bool:
    return Path(NFQWS_BIN).exists()


def _check_nfqueue_module() -> bool:
    """Проверить что ядерный модуль nfnetlink_queue загружен."""
    try:
        with open("/proc/modules") as f:
            return "nfnetlink_queue" in f.read()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Основные функции
# ---------------------------------------------------------------------------
async def start(temp: bool = False) -> int:
    """
    Запустить zapret стек.
    temp=True: только nfqws + nft rules (без изменения routing) — для тестирования.
    temp=False: полный старт (watchdog сам изменит routing через _do_switch).
    """
    if not _check_binary():
        print(json.dumps({"status": "error", "message": f"{NFQWS_BIN} не найден. Запусти install.sh"}))
        return 1

    # Загрузить ядерный модуль если нужно
    if not _check_nfqueue_module():
        await run_cmd(["modprobe", "nfnetlink_queue"], timeout=5)
        await asyncio.sleep(0.5)

    # Выбор лучшего пресета через Thompson Sampling
    # Если probe ещё не запускался — сначала запустить quick probe
    needs_probe = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "probe.py"), "needs-probe"],
        capture_output=True, timeout=3,
    ).returncode == 0

    if needs_probe:
        print("[zapret] Первый запуск — быстрый probe параметров...", flush=True)
        rc = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "quick"],
            timeout=120,
        ).returncode
        if rc != 0:
            print("[zapret] probe не удался, используем дефолтный пресет", file=sys.stderr)

    preset = _get_best_preset()
    print(f"[zapret] Используем пресет {preset['id']}: {preset['desc']}", flush=True)

    # Остановить предыдущий экземпляр
    await _stop_nfqws()

    # Запустить nfqws
    rc, _, err = await run_cmd(_nfqws_args(preset), timeout=5)
    if rc != 0:
        print(json.dumps({"status": "error", "message": f"nfqws start: {err.strip()}"}))
        return 1

    # Подождать запуска daemon
    await asyncio.sleep(1)
    if not PID_FILE.exists():
        print(json.dumps({"status": "error", "message": "nfqws не запустился (нет pidfile)"}))
        return 1

    # Добавить nft правила
    await _nft_add_rules()

    mode = "temp" if temp else "main"
    print(json.dumps({
        "status": "started",
        "preset": preset["id"],
        "desc": preset["desc"],
        "mode": mode,
        "direct_mode": True,
    }))
    return 0


async def stop() -> int:
    await _stop_nfqws()
    await _nft_del_rules()
    print(json.dumps({"status": "stopped"}))
    return 0


async def _measure_direct_throughput() -> float:
    """
    Замерить пропускную способность прямого соединения через eth0.
    Использует российские ISP серверы (не блокируются в России).
    Zapret не создаёт тун-интерфейс — пропускная способность = прямой интернет.
    """
    for url in DIRECT_TEST_SERVERS:
        rc, out, _ = await run_cmd(
            ["curl", "-sL", "--max-time", "12", "--interface", ETH_IFACE,
             "-o", "/dev/null", "-w", "%{speed_download} %{http_code}", url],
            timeout=17,
        )
        if not out.strip():
            continue
        parts = out.strip().split()
        http_code = parts[1] if len(parts) >= 2 else "0"
        if http_code != "200":
            continue
        try:
            mbps = round(float(parts[0]) * 8 / 1_000_000, 1)
            if mbps >= 1.0:
                return mbps
        except Exception:
            continue
    return 0.0


async def test() -> int:
    """
    Проверка работоспособности zapret стека.

    Режимы:
      - Активный (nfqws запущен): проверяем pid + nft таблицу + замеряем скорость.
      - Standby (nfqws не запущен): проверяем бинарник + ядерный модуль + замеряем скорость.

    ВАЖНО: curl --interface eth0 к заблокированным сайтам НЕ работает как тест —
    трафик идёт через OUTPUT chain, а nft запрет_main перехватывает только FORWARD chain
    (трафик от wg0/wg1 клиентов). Поэтому тест connectivity здесь не выполняется;
    реальная проверка DPI bypass происходит в probe.py через SO_MARK + OUTPUT chain.
    """
    # Проверить наличие бинарника — базовое требование
    if not _check_binary():
        print(json.dumps({"status": "fail", "throughput_mbps": 0,
                          "message": f"{NFQWS_BIN} не найден — запустите install.sh"}))
        return 1

    # Проверить ядерный модуль
    if not _check_nfqueue_module():
        print(json.dumps({"status": "fail", "throughput_mbps": 0,
                          "message": "nfnetlink_queue не загружен"}))
        return 1

    if PID_FILE.exists():
        # ── Активный режим ────────────────────────────────────────────────────
        try:
            pid = int(PID_FILE.read_text().strip())
            rc, _, _ = await run_cmd(["kill", "-0", str(pid)], timeout=3)
            if rc != 0:
                # Записать неудачу в Thompson Sampling
                subprocess.run(
                    [sys.executable, str(PLUGIN_DIR / "probe.py"), "record", "0"],
                    capture_output=True, timeout=3,
                )
                print(json.dumps({"status": "fail", "throughput_mbps": 0,
                                  "message": "nfqws процесс мёртв"}))
                return 1
        except Exception:
            pass

        # Проверить nft таблицу
        rc_nft, _, _ = await run_cmd(
            ["nft", "list", "table", "inet", "zapret_main"], timeout=3,
        )
        if rc_nft != 0:
            print(json.dumps({"status": "fail", "throughput_mbps": 0,
                              "message": "nft таблица zapret_main отсутствует"}))
            return 1

        # Замер пропускной способности
        throughput = await _measure_direct_throughput()

        # Записать успех в Thompson Sampling
        subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "record", "1"],
            capture_output=True, timeout=3,
        )

        preset = _get_best_preset()
        print(json.dumps({
            "status": "ok",
            "throughput_mbps": max(throughput, 1.0),
            "preset": preset["id"],
            "mode": "active",
        }))
        return 0

    else:
        # ── Standby режим: бинарник и модуль уже проверены выше ──────────────
        throughput = await _measure_direct_throughput()
        preset = _get_best_preset()
        print(json.dumps({
            "status": "ok",
            "throughput_mbps": max(throughput, 1.0),
            "preset": preset["id"],
            "mode": "standby",
        }))
        return 0


async def rotate() -> int:
    """
    Ротация: запуск quick probe → перезапуск с новым лучшим пресетом.
    """
    print("[zapret] Ротация: запуск quick probe...", flush=True)
    await stop()

    rc = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "probe.py"), "quick"],
        timeout=120,
    ).returncode
    if rc != 0:
        print("[zapret] probe не удался при ротации", file=sys.stderr)

    return await start()


async def probe(full: bool = False) -> int:
    """Запустить адаптивный поиск параметров."""
    mode = "full" if full else "quick"
    print(f"[zapret] Запуск {mode} probe...", flush=True)
    rc = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "probe.py"), mode],
        timeout=300 if full else 120,
    ).returncode
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    temp = "--temp" in sys.argv

    if cmd == "start":
        sys.exit(await start(temp=temp))
    elif cmd == "stop":
        sys.exit(await stop())
    elif cmd == "test":
        sys.exit(await test())
    elif cmd == "rotate":
        sys.exit(await rotate())
    elif cmd == "probe":
        full = len(sys.argv) > 2 and sys.argv[2] == "full"
        sys.exit(await probe(full=full))
    else:
        print(f"Неизвестная команда: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

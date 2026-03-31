#!/usr/bin/env python3
"""
zapret плагин для watchdog.
Управляет nfqws (netfilter queue worker) — Linux-аналог GoodbyeDPI.

Режим работы:
  - nfqws перехватывает TCP 80/443 в FORWARD chain (от wg0/wg1 к eth0)
    и применяет DPI-bypass техники (fakedsplit/TTL манипуляции).
  - Параметры выбираются через Thompson Sampling (probe.py).
  - zapret НЕ является частью основного стека failover (tier-2, hysteria2 и т.д.).
    Он работает ПАРАЛЛЕЛЬНО: поднят всегда, трафик заводится отдельным решением.

Жизненный цикл:
  start     — запускает nfqws-демон (готов обрабатывать очередь), probe если нужно.
              БЕЗ активации nft FORWARD правил. Мониторинг и probe работают.
  activate  — добавляет nft FORWARD правила: трафик WireGuard-клиентов идёт через nfqws.
  deactivate— убирает nft FORWARD правила (nfqws продолжает работать).
  stop      — останавливает nfqws + убирает все nft правила.
  test      — проверка работоспособности (pid жив, модуль загружен, скорость).
  rotate    — quick probe → перезапуск с новым лучшим пресетом.
  probe [full] — адаптивный поиск параметров.

Команды:
  start             — запуск nfqws без активации трафика
  activate          — активировать nft FORWARD (завести трафик)
  deactivate        — убрать nft FORWARD (трафик идёт мимо)
  stop              — полная остановка
  test              — проверка состояния
  rotate            — re-probe + перезапуск с новым пресетом
  probe [full]      — адаптивный поиск параметров
"""
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from base import BasePlugin, child_env

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

HISTORY_FILE = PLUGIN_DIR / "preset_history.log"


class ZapretPlugin(BasePlugin):
    name = "zapret"
    pid_file = PID_FILE

    def __init__(self) -> None:
        self._history_lock = asyncio.Lock()

    # ---------------------------------------------------------------------------
    # Внутренние утилиты
    # ---------------------------------------------------------------------------

    async def _get_best_preset(self) -> dict:
        """Получить лучший пресет от probe.py (или дефолтный если probe не запускался)."""
        rc, out, _ = await self.run_cmd(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "best"],
            timeout=5,
        )
        if rc == 0 and out.strip():
            try:
                return json.loads(out.strip())
            except Exception:
                pass
        # Дефолтный пресет (C01: fakedsplit+midsld+autottl+badsum — проверен на этом ISP)
        return {
            "id": "C01",
            "args": [
                "--dpi-desync=fakedsplit",
                "--dpi-desync-split-pos=midsld",
                "--dpi-desync-autottl",
                "--dpi-desync-fooling=badsum",
            ],
            "desc": "fakedsplit+midsld+autottl+badsum (default)",
        }

    def _nfqws_args(self, preset: dict) -> list[str]:
        return [
            NFQWS_BIN,
            "--daemon",
            f"--pidfile={PID_FILE}",
            f"--qnum={NFQUEUE_NUM}",
        ] + preset["args"]

    async def _stop_nfqws(self) -> None:
        await self.stop_process(PID_FILE)
        # Дополнительная зачистка — pkill по паттерну
        await self.run_cmd(["pkill", "-f", f"nfqws.*qnum={NFQUEUE_NUM}[^0-9]"], timeout=3)

    async def _is_forward_active(self) -> bool:
        """Проверить, добавлены ли nft FORWARD правила (трафик заведён)."""
        rc, _, _ = await self.run_cmd(
            ["nft", "list", "table", "inet", "zapret_main"], timeout=3
        )
        return rc == 0

    async def _nft_add_forward_rules(self) -> bool:
        """
        Добавить nft таблицу zapret_main (FORWARD chain).
        Перехватывает TCP 443/80 от VPN клиентов (wg0/wg1) выходящих через eth0.
        Вызывается ТОЛЬКО при явной активации трафика через zapret.
        Возвращает True при успехе.
        """
        nft_script = f"""\
table inet zapret_main {{
    chain forward {{
        type filter hook forward priority filter + 1;
        iifname {{ "wg0", "wg1" }} oifname "{ETH_IFACE}" tcp dport {{ 80, 443 }} queue num {NFQUEUE_NUM} bypass
    }}
}}
"""
        proc = await asyncio.create_subprocess_exec(
            "nft", "-f", "-",
            env=child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(input=nft_script.encode())
        if proc.returncode != 0:
            print(f"[zapret] nft add forward rules: {err.decode().strip()}", file=sys.stderr)
            return False
        return True

    async def _nft_del_forward_rules(self) -> None:
        """Удалить nft таблицу zapret_main (убрать FORWARD правила)."""
        await self.run_cmd(["nft", "delete", "table", "inet", "zapret_main"], timeout=5)

    async def _log_preset_history(self, preset: dict) -> None:
        """Записать активацию пресета в историю (с lock для thread safety)."""
        async with self._history_lock:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            line = f"{ts}  {preset['id']}  {preset.get('desc', '')}"
            try:
                with HISTORY_FILE.open("a") as f:
                    f.write(line + "\n")
                # Ограничить 200 строками
                lines = HISTORY_FILE.read_text().splitlines()
                if len(lines) > 200:
                    HISTORY_FILE.write_text("\n".join(lines[-200:]) + "\n")
            except Exception:
                pass

    def _check_binary(self) -> bool:
        return Path(NFQWS_BIN).exists()

    def _check_nfqueue_module(self) -> bool:
        """Проверить что ядерный модуль nfnetlink_queue загружен."""
        try:
            with open("/proc/modules") as f:
                return "nfnetlink_queue" in f.read()
        except Exception:
            return False

    async def _measure_direct_throughput(self) -> float:
        """
        Замерить пропускную способность прямого соединения через eth0.
        Использует российские ISP серверы (не блокируются в России).
        """
        for url in DIRECT_TEST_SERVERS:
            rc, out, _ = await self.run_cmd(
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

    # ---------------------------------------------------------------------------
    # Основные методы
    # ---------------------------------------------------------------------------

    async def start(self) -> int:
        """
        Запустить nfqws-демон.

        Запускает nfqws (готов обрабатывать пакеты из очереди 200),
        но НЕ добавляет nft FORWARD правила — трафик WireGuard-клиентов
        пока не заводится через nfqws. Мониторинг и Thompson Sampling probe работают.

        Для активации трафика вызвать: activate()
        """
        if not self._check_binary():
            print(json.dumps({"status": "error", "message": f"{NFQWS_BIN} не найден. Запусти install.sh"}))
            return 1

        # Загрузить ядерный модуль если нужно
        if not self._check_nfqueue_module():
            await self.run_cmd(["modprobe", "nfnetlink_queue"], timeout=5)
            await asyncio.sleep(0.5)

        # Quick probe при первом запуске
        rc_probe, _, _ = await self.run_cmd(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "needs-probe"],
            timeout=3,
        )
        if rc_probe == 0:
            print("[zapret] Первый запуск — быстрый probe параметров...", flush=True)
            rc, _, _ = await self.run_cmd(
                [sys.executable, str(PLUGIN_DIR / "probe.py"), "quick"],
                timeout=120,
            )
            if rc != 0:
                print("[zapret] probe не удался, используем дефолтный пресет", file=sys.stderr)

        preset = await self._get_best_preset()
        await self._log_preset_history(preset)
        print(f"[zapret] Используем пресет {preset['id']}: {preset['desc']}", flush=True)

        # Остановить предыдущий экземпляр
        await self._stop_nfqws()

        # Запустить nfqws (daemon mode — пишет собственный pidfile)
        rc, _, err = await self.run_cmd(self._nfqws_args(preset), timeout=5)
        if rc != 0:
            print(json.dumps({"status": "error", "message": f"nfqws start: {err.strip()}"}))
            return 1

        # Подождать запуска daemon
        await asyncio.sleep(1)
        if not PID_FILE.exists():
            print(json.dumps({"status": "error", "message": "nfqws не запустился (нет pidfile)"}))
            return 1

        # nft FORWARD правила НЕ добавляются — трафик не заводится до явного activate()
        print(json.dumps({
            "status": "started",
            "preset": preset["id"],
            "desc": preset["desc"],
            "traffic_active": False,   # трафик не заведён, только демон запущен
        }))
        return 0

    async def activate(self) -> int:
        """
        Активировать перехват трафика WireGuard-клиентов через nfqws.

        Добавляет nft FORWARD правило: TCP 80/443 от wg0/wg1 идёт через очередь 200.
        nfqws должен быть уже запущен (start).
        """
        if not PID_FILE.exists():
            print(json.dumps({"status": "error", "message": "nfqws не запущен, сначала start"}))
            return 1

        try:
            pid = int(PID_FILE.read_text().strip())
            rc, _, _ = await self.run_cmd(["kill", "-0", str(pid)], timeout=3)
            if rc != 0:
                print(json.dumps({"status": "error", "message": "nfqws процесс не отвечает"}))
                return 1
        except Exception:
            pass

        ok = await self._nft_add_forward_rules()
        if not ok:
            print(json.dumps({"status": "error", "message": "nft add forward rules failed"}))
            return 1

        preset = await self._get_best_preset()
        print(json.dumps({
            "status": "activated",
            "preset": preset["id"],
            "desc": preset["desc"],
            "traffic_active": True,
        }))
        return 0

    async def deactivate(self) -> int:
        """
        Убрать перехват трафика, оставив nfqws-демон запущенным.
        Трафик WireGuard-клиентов перестаёт идти через nfqws.
        nfqws продолжает работать (мониторинг, probe).
        """
        await self._nft_del_forward_rules()
        print(json.dumps({"status": "deactivated", "traffic_active": False}))
        return 0

    async def stop(self) -> int:
        """Полная остановка: убить nfqws + убрать все nft правила."""
        await self._stop_nfqws()
        await self._nft_del_forward_rules()
        print(json.dumps({"status": "stopped"}))
        return 0

    async def test(self) -> int:
        """
        Проверка работоспособности zapret.

        Режимы (определяются по наличию pidfile):
          - running+active:  pid жив + nft таблица есть → трафик идёт через nfqws
          - running+standby: pid жив, nft таблицы нет → демон запущен, трафик не заведён
          - stopped:         pidfile нет → только проверка бинарника и модуля

        ВАЖНО: тест connectivity здесь не делаем — трафик сервера через OUTPUT chain,
        а nft zapret_main перехватывает FORWARD chain (трафик WG-клиентов).
        DPI bypass тестируется в probe.py через SO_MARK + OUTPUT chain.
        """
        if not self._check_binary():
            print(json.dumps({"status": "fail", "throughput_mbps": 0,
                              "message": f"{NFQWS_BIN} не найден — запустите install.sh"}))
            return 1

        if not self._check_nfqueue_module():
            print(json.dumps({"status": "fail", "throughput_mbps": 0,
                              "message": "nfnetlink_queue не загружен"}))
            return 1

        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                rc, _, _ = await self.run_cmd(["kill", "-0", str(pid)], timeout=3)
                if rc != 0:
                    await self.run_cmd(
                        [sys.executable, str(PLUGIN_DIR / "probe.py"), "record", "0"],
                        timeout=3,
                    )
                    print(json.dumps({"status": "fail", "throughput_mbps": 0,
                                      "message": "nfqws процесс мёртв"}))
                    return 1
            except Exception:
                pass

            forward_active = await self._is_forward_active()
            throughput = await self._measure_direct_throughput()

            if forward_active:
                await self.run_cmd(
                    [sys.executable, str(PLUGIN_DIR / "probe.py"), "record", "1"],
                    timeout=3,
                )

            preset = await self._get_best_preset()
            mode = "active" if forward_active else "running"
            print(json.dumps({
                "status": "ok",
                "throughput_mbps": max(throughput, 1.0),
                "preset": preset["id"],
                "mode": mode,            # active=трафик заведён, running=демон запущен без трафика
                "traffic_active": forward_active,
            }))
            return 0

        else:
            throughput = await self._measure_direct_throughput()
            preset = await self._get_best_preset()
            print(json.dumps({
                "status": "ok",
                "throughput_mbps": max(throughput, 1.0),
                "preset": preset["id"],
                "mode": "standby",
                "traffic_active": False,
            }))
            return 0

    async def rotate(self) -> int:
        """
        Ротация пресета: quick probe → перезапуск с новым лучшим пресетом.
        Сохраняет текущее состояние активации трафика.
        """
        print("[zapret] Ротация: запуск quick probe...", flush=True)
        was_active = await self._is_forward_active()
        await self.stop()

        rc, _, _ = await self.run_cmd(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), "quick"],
            timeout=120,
        )
        if rc != 0:
            print("[zapret] probe не удался при ротации", file=sys.stderr)

        rc = await self.start()
        if rc == 0 and was_active:
            rc = await self.activate()
        return rc

    async def probe(self, full: bool = False) -> int:
        """Запустить адаптивный поиск параметров."""
        mode = "full" if full else "quick"
        print(f"[zapret] Запуск {mode} probe...", flush=True)
        rc, _, _ = await self.run_cmd(
            [sys.executable, str(PLUGIN_DIR / "probe.py"), mode],
            timeout=300 if full else 120,
        )
        return rc


# ---------------------------------------------------------------------------
# Модульные обёртки (вызываются watchdog как subprocess)
# ---------------------------------------------------------------------------
_p = ZapretPlugin()


async def start() -> int:
    return await _p.start()

async def activate() -> int:
    return await _p.activate()

async def deactivate() -> int:
    return await _p.deactivate()

async def stop() -> int:
    return await _p.stop()

async def test() -> int:
    return await _p.test()

async def rotate() -> int:
    return await _p.rotate()

async def probe(full: bool = False) -> int:
    return await _p.probe(full=full)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "start":
        sys.exit(await start())
    elif cmd == "activate":
        sys.exit(await activate())
    elif cmd == "deactivate":
        sys.exit(await deactivate())
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

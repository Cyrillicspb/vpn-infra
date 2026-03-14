#!/usr/bin/env python3
"""
zapret Adaptive Parameter Probe — Thompson Sampling Multi-Armed Bandit.

Логика:
  1. DISCOVER mode: последовательно тестирует все PRESETS, записывает результаты.
  2. EXPLOIT mode: Thompson Sampling выбирает пресет для текущего запуска.
  3. NIGHTLY mode: полный re-probe всех пресетов, обновляет ранжирование.
  4. При 3 последовательных failure текущего пресета → экстренный re-probe.

Хранение состояния: /opt/vpn/watchdog/plugins/zapret/probe_state.json
"""
import asyncio
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------
NFQWS_BIN    = "/usr/local/bin/nfqws"
STATE_FILE   = Path("/opt/vpn/watchdog/plugins/zapret/probe_state.json")
NFQUEUE_NUM  = 200
ETH_IFACE    = os.getenv("NET_INTERFACE", "eth0")

# URLs для тестирования — заблокированные сайты через прямой выход
TEST_URLS = [
    "https://www.youtube.com",
    "https://www.instagram.com",
    "https://t.me",
]

# Таймаут одного теста (секунды)
TEST_TIMEOUT = 8

# ---------------------------------------------------------------------------
# Пресеты параметров zapret (17 конфигов, проверенных против ТСПУ/РКН)
# ---------------------------------------------------------------------------
# Формат: {"id": str, "args": list[str], "desc": str}
#
# Техники:
#   fake       — отправить фейковый TLS ClientHello (DPI обрабатывает его,
#                пока настоящий проходит следом)
#   split2     — разделить пакет после позиции 1 (DPI не успевает сшить)
#   synfake    — фейк в фазе SYN (для продвинутых DPI)
#   badsum     — фейковый пакет с неверной контрольной суммой (DPI принимает,
#                хост-назначение отбрасывает — прозрачно для реального соединения)
#   badseq     — неверный sequence number в фейке
#   md5sig     — фейк с TCP MD5 signature (многие DPI игнорируют md5 пакеты)
#   ttl N      — TTL фейкового пакета: должен достичь DPI но НЕ сервера
#                (обычно 6–10 hop до DPI провайдера)
# ---------------------------------------------------------------------------
PRESETS: list[dict] = [
    # ── Основные fake + badsum (работают против большинства ТСПУ) ───────────
    {
        "id": "C01",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=8", "--dpi-desync-fooling=badsum"],
        "desc": "fake+badsum+ttl8 (базовый)",
    },
    {
        "id": "C02",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=6", "--dpi-desync-fooling=badsum"],
        "desc": "fake+badsum+ttl6 (ближний DPI)",
    },
    {
        "id": "C03",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=10", "--dpi-desync-fooling=badsum"],
        "desc": "fake+badsum+ttl10 (дальний DPI)",
    },
    # ── fake + md5sig (DPI не проверяет MD5, хост отбрасывает молча) ────────
    {
        "id": "C04",
        "args": ["--dpi-desync=fake", "--dpi-desync-fooling=md5sig"],
        "desc": "fake+md5sig (без TTL)",
    },
    {
        "id": "C05",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=8", "--dpi-desync-fooling=md5sig"],
        "desc": "fake+md5sig+ttl8",
    },
    # ── fake + badseq ────────────────────────────────────────────────────────
    {
        "id": "C06",
        "args": ["--dpi-desync=fake", "--dpi-desync-ttl=8", "--dpi-desync-fooling=badseq"],
        "desc": "fake+badseq+ttl8",
    },
    # ── fake + split2 (двойной удар: фейк + фрагментация) ───────────────────
    {
        "id": "C07",
        "args": [
            "--dpi-desync=fake,split2",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badsum",
        ],
        "desc": "fake+split2+badsum+ttl8",
    },
    {
        "id": "C08",
        "args": [
            "--dpi-desync=fake,split2",
            "--dpi-desync-ttl=6",
            "--dpi-desync-fooling=badsum",
        ],
        "desc": "fake+split2+badsum+ttl6",
    },
    {
        "id": "C09",
        "args": [
            "--dpi-desync=fake,split2",
            "--dpi-desync-fooling=md5sig",
        ],
        "desc": "fake+split2+md5sig",
    },
    {
        "id": "C10",
        "args": [
            "--dpi-desync=fake,split2",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badseq",
        ],
        "desc": "fake+split2+badseq+ttl8",
    },
    # ── synfake (продвинутый — фейк в SYN, до TLS handshake) ────────────────
    {
        "id": "C11",
        "args": [
            "--dpi-desync=synfake",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badsum",
        ],
        "desc": "synfake+badsum+ttl8",
    },
    {
        "id": "C12",
        "args": [
            "--dpi-desync=fake,synfake",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badsum",
        ],
        "desc": "fake+synfake+badsum+ttl8",
    },
    # ── split2 без fake (чистая фрагментация) ────────────────────────────────
    {
        "id": "C13",
        "args": ["--dpi-desync=split2", "--dpi-desync-fooling=badsum"],
        "desc": "split2+badsum",
    },
    {
        "id": "C14",
        "args": ["--dpi-desync=split2", "--dpi-desync-fooling=md5sig"],
        "desc": "split2+md5sig",
    },
    # ── с уменьшением TCP window (wssize — для некоторых ISP) ────────────────
    {
        "id": "C15",
        "args": [
            "--dpi-desync=fake",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badsum",
            "--wssize=1",
            "--wssize-cutoff=n3",
        ],
        "desc": "fake+badsum+ttl8+wssize1",
    },
    # ── split position 2 (разрезаем после 2-го байта) ────────────────────────
    {
        "id": "C16",
        "args": [
            "--dpi-desync=fake,split2",
            "--dpi-desync-split-pos=2",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=badsum",
        ],
        "desc": "fake+split@2+badsum+ttl8",
    },
    # ── hopbyhop (IPv6 extension header trick, нечасто но работает) ─────────
    {
        "id": "C17",
        "args": [
            "--dpi-desync=fake",
            "--dpi-desync-ttl=8",
            "--dpi-desync-fooling=hopbyhop",
        ],
        "desc": "fake+hopbyhop+ttl8",
    },
]

PRESET_IDS = [p["id"] for p in PRESETS]
N_ARMS = len(PRESETS)


# ---------------------------------------------------------------------------
# Thompson Sampling
# ---------------------------------------------------------------------------
class ThompsonSampling:
    """
    Beta(alpha, beta) posterior для каждого пресета.
    Prior: alpha=1, beta=1 (равномерное — все пресеты равновероятны изначально).
    """

    def __init__(self, n: int) -> None:
        self.alpha = [1.0] * n
        self.beta  = [1.0] * n

    def choose(self) -> int:
        """Выбрать пресет для следующего теста (Thompson Sampling)."""
        samples = [random.betavariate(a, b) for a, b in zip(self.alpha, self.beta)]
        return samples.index(max(samples))

    def update(self, arm: int, success: bool) -> None:
        if success:
            self.alpha[arm] += 1.0
        else:
            self.beta[arm] += 1.0

    def best_arm(self) -> int:
        """Наиболее успешный по текущим данным (argmax mean)."""
        means = [a / (a + b) for a, b in zip(self.alpha, self.beta)]
        return means.index(max(means))

    def confidence(self, arm: int) -> float:
        """Вероятность успеха (mean Beta)."""
        a, b = self.alpha[arm], self.beta[arm]
        return a / (a + b)

    def ranking(self) -> list[tuple[int, float]]:
        """Список (arm_idx, mean) отсортированный по убыванию."""
        means = [(i, self.alpha[i] / (self.alpha[i] + self.beta[i])) for i in range(len(self.alpha))]
        return sorted(means, key=lambda x: -x[1])

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "beta": self.beta}

    def from_dict(self, d: dict) -> None:
        self.alpha = d["alpha"]
        self.beta  = d["beta"]


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------
class ProbeState:
    def __init__(self) -> None:
        self.ts = ThompsonSampling(N_ARMS)
        self.best_preset_id: str = PRESETS[0]["id"]
        self.consecutive_failures: int = 0
        self.last_probe: str = ""
        self.last_full_probe: str = ""
        self.probe_history: list[dict] = []  # последние 100 результатов

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            d = json.loads(STATE_FILE.read_text())
            self.ts.from_dict(d.get("ts", {"alpha": [1.0] * N_ARMS, "beta": [1.0] * N_ARMS}))
            self.best_preset_id      = d.get("best_preset_id", PRESETS[0]["id"])
            self.consecutive_failures = d.get("consecutive_failures", 0)
            self.last_probe           = d.get("last_probe", "")
            self.last_full_probe      = d.get("last_full_probe", "")
            self.probe_history        = d.get("probe_history", [])
        except Exception as e:
            print(f"[probe] Ошибка загрузки состояния: {e}", file=sys.stderr)

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        d = {
            "ts":                   self.ts.to_dict(),
            "best_preset_id":       self.best_preset_id,
            "consecutive_failures": self.consecutive_failures,
            "last_probe":           self.last_probe,
            "last_full_probe":      self.last_full_probe,
            "probe_history":        self.probe_history[-100:],
        }
        STATE_FILE.write_text(json.dumps(d, indent=2))

    def best_preset(self) -> dict:
        for p in PRESETS:
            if p["id"] == self.best_preset_id:
                return p
        return PRESETS[0]

    def record(self, preset_id: str, success: bool, latency_ms: float, urls_ok: int) -> None:
        self.probe_history.append({
            "ts":         datetime.now().isoformat(timespec="seconds"),
            "preset_id":  preset_id,
            "success":    success,
            "latency_ms": latency_ms,
            "urls_ok":    urls_ok,
        })


probe_state = ProbeState()


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


def _nfqws_cmd(preset: dict, extra_args: list | None = None) -> list[str]:
    """Собрать команду запуска nfqws с параметрами пресета."""
    cmd = [
        NFQWS_BIN,
        "--daemon",
        f"--pidfile=/run/nfqws-probe.pid",
        "--user=daemon",
        f"--qnum={NFQUEUE_NUM}",
    ] + preset["args"]
    if extra_args:
        cmd += extra_args
    return cmd


async def _start_nfqws(preset: dict) -> bool:
    """Запустить nfqws с параметрами пресета (daemon mode)."""
    await _stop_nfqws()
    rc, _, err = await run_cmd(_nfqws_cmd(preset), timeout=5)
    if rc != 0:
        print(f"[probe] nfqws start failed: {err.strip()}", file=sys.stderr)
        return False
    await asyncio.sleep(0.5)
    return True


async def _stop_nfqws() -> None:
    """Остановить nfqws (probe instance)."""
    pidfile = Path("/run/nfqws-probe.pid")
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            await run_cmd(["kill", str(pid)], timeout=3)
            pidfile.unlink(missing_ok=True)
        except Exception:
            pass
    await run_cmd(["pkill", "-f", f"nfqws.*--qnum={NFQUEUE_NUM}.*pidfile=/run/nfqws-probe"], timeout=3)


async def _add_nft_probe_rules() -> None:
    """
    Добавить временную nft таблицу для probe-теста.
    Перехватывает TCP 443 (HTTPS) и 80 (HTTP) от WireGuard клиентов.
    """
    nft_script = f"""
table inet zapret_probe {{
    chain forward {{
        type filter hook forward priority filter + 1;
        iifname {{ "wg0", "wg1" }} oifname "{ETH_IFACE}" tcp dport {{ 80, 443 }} queue num {NFQUEUE_NUM} bypass
    }}
}}
"""
    await run_cmd(["nft", "-f", "-"], timeout=5)
    # Используем pipe
    proc = await asyncio.create_subprocess_exec(
        "nft", "-f", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=nft_script.encode())


async def _del_nft_probe_rules() -> None:
    """Удалить временную nft таблицу probe."""
    await run_cmd(["nft", "delete", "table", "inet", "zapret_probe"], timeout=5)


async def _test_preset_connectivity() -> tuple[bool, float, int]:
    """
    Тест подключения к заблокированным сайтам через текущий eth0 маршрут.
    Возвращает (success, avg_latency_ms, urls_ok).
    """
    ok_count = 0
    latencies: list[float] = []

    for url in TEST_URLS:
        t0 = time.time()
        rc, out, _ = await run_cmd(
            [
                "curl", "-s",
                "--interface", ETH_IFACE,
                "--max-time", str(TEST_TIMEOUT),
                "--connect-timeout", "5",
                "-o", "/dev/null",
                "-w", "%{http_code}",
                url,
            ],
            timeout=TEST_TIMEOUT + 3,
        )
        elapsed_ms = (time.time() - t0) * 1000
        code = out.strip()
        if rc == 0 and code in ("200", "301", "302", "303"):
            ok_count += 1
            latencies.append(elapsed_ms)

    avg_lat = sum(latencies) / len(latencies) if latencies else 9999.0
    success = ok_count >= 2  # минимум 2 из 3 URL доступны
    return success, avg_lat, ok_count


# ---------------------------------------------------------------------------
# Основные функции probe
# ---------------------------------------------------------------------------
async def run_full_probe() -> str:
    """
    Полный probe: тестирует все пресеты последовательно.
    Обновляет Thompson Sampling на основе результатов.
    Возвращает ID лучшего пресета.
    """
    probe_state.load()
    print("[probe] Запуск полного probe (все 17 пресетов)...", flush=True)

    await _add_nft_probe_rules()
    try:
        for i, preset in enumerate(PRESETS):
            print(f"[probe] [{i+1}/{N_ARMS}] Тест пресета {preset['id']}: {preset['desc']}", flush=True)

            started = await _start_nfqws(preset)
            if not started:
                probe_state.ts.update(i, False)
                probe_state.record(preset["id"], False, 9999.0, 0)
                continue

            success, latency_ms, urls_ok = await _test_preset_connectivity()
            probe_state.ts.update(i, success)
            probe_state.record(preset["id"], success, latency_ms, urls_ok)

            status = "✓" if success else "✗"
            print(
                f"[probe]   {status} {urls_ok}/3 URL, latency={latency_ms:.0f}ms",
                flush=True,
            )
            await _stop_nfqws()
            await asyncio.sleep(1)

    finally:
        await _stop_nfqws()
        await _del_nft_probe_rules()

    best_idx = probe_state.ts.best_arm()
    best = PRESETS[best_idx]
    probe_state.best_preset_id = best["id"]
    probe_state.consecutive_failures = 0
    probe_state.last_full_probe = datetime.now().isoformat(timespec="seconds")
    probe_state.save()

    ranking = probe_state.ts.ranking()
    print("\n[probe] Топ-5 пресетов:")
    for rank, (idx, mean) in enumerate(ranking[:5], 1):
        p = PRESETS[idx]
        print(f"  {rank}. {p['id']} {p['desc']} — score={mean:.2f}")
    print(f"\n[probe] Лучший пресет: {best['id']} ({best['desc']})")

    return best["id"]


async def run_quick_probe() -> str:
    """
    Быстрый probe: тестирует только топ-3 пресета + 1 случайный (exploration).
    Используется для ночной проверки / после failure.
    """
    probe_state.load()

    ranking = probe_state.ts.ranking()
    top3_idxs = [r[0] for r in ranking[:3]]
    explore_idx = probe_state.ts.choose()  # Thompson Sampling exploration

    candidates_idxs = list(dict.fromkeys(top3_idxs + [explore_idx]))  # дедупликация
    candidates = [PRESETS[i] for i in candidates_idxs]

    print(f"[probe] Быстрый probe ({len(candidates)} пресетов)...", flush=True)
    await _add_nft_probe_rules()
    try:
        for i, preset in enumerate(candidates):
            arm_idx = PRESET_IDS.index(preset["id"])
            print(f"[probe] [{i+1}/{len(candidates)}] Тест {preset['id']}: {preset['desc']}", flush=True)

            started = await _start_nfqws(preset)
            if not started:
                probe_state.ts.update(arm_idx, False)
                continue

            success, latency_ms, urls_ok = await _test_preset_connectivity()
            probe_state.ts.update(arm_idx, success)
            probe_state.record(preset["id"], success, latency_ms, urls_ok)

            status = "✓" if success else "✗"
            print(f"[probe]   {status} {urls_ok}/3 URL, latency={latency_ms:.0f}ms", flush=True)
            await _stop_nfqws()
            await asyncio.sleep(0.5)
    finally:
        await _stop_nfqws()
        await _del_nft_probe_rules()

    best_idx = probe_state.ts.best_arm()
    best = PRESETS[best_idx]
    probe_state.best_preset_id = best["id"]
    probe_state.last_probe = datetime.now().isoformat(timespec="seconds")
    probe_state.save()
    return best["id"]


def get_best_preset() -> dict:
    """Вернуть лучший пресет по текущему состоянию Thompson Sampling."""
    probe_state.load()
    return probe_state.best_preset()


def record_result(success: bool) -> None:
    """Записать результат текущего пресета (вызывается из client.py)."""
    probe_state.load()
    best_id = probe_state.best_preset_id
    arm_idx = PRESET_IDS.index(best_id) if best_id in PRESET_IDS else 0
    probe_state.ts.update(arm_idx, success)
    if success:
        probe_state.consecutive_failures = 0
    else:
        probe_state.consecutive_failures += 1
    probe_state.record(best_id, success, 0.0, 0)
    probe_state.save()


def needs_emergency_probe() -> bool:
    """True если нужен экстренный re-probe (3 подряд неудачи)."""
    probe_state.load()
    return probe_state.consecutive_failures >= 3


def needs_initial_probe() -> bool:
    """True если полный probe ещё не проводился."""
    probe_state.load()
    return not probe_state.last_full_probe


def print_status() -> None:
    """Распечатать текущее состояние Thompson Sampling."""
    probe_state.load()
    print(f"Лучший пресет: {probe_state.best_preset_id}")
    print(f"Подряд неудач: {probe_state.consecutive_failures}")
    print(f"Последний full probe: {probe_state.last_full_probe or 'никогда'}")
    print(f"Последний probe: {probe_state.last_probe or 'никогда'}")
    print("\nТекущий рейтинг (Thompson Sampling):")
    ranking = probe_state.ts.ranking()
    for rank, (idx, mean) in enumerate(ranking, 1):
        p = PRESETS[idx]
        a = probe_state.ts.alpha[idx]
        b = probe_state.ts.beta[idx]
        n = int(a + b - 2)
        print(f"  {rank:2}. {p['id']} score={mean:.3f} ({n} тестов) — {p['desc']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "full":
        best = await run_full_probe()
        print(f"\nРезультат: {best}")
    elif cmd == "quick":
        best = await run_quick_probe()
        print(f"\nРезультат: {best}")
    elif cmd == "best":
        p = get_best_preset()
        print(json.dumps({"id": p["id"], "args": p["args"], "desc": p["desc"]}))
    elif cmd == "status":
        print_status()
    elif cmd == "needs-probe":
        sys.exit(0 if needs_initial_probe() or needs_emergency_probe() else 1)
    else:
        print(f"Команды: full | quick | best | status | needs-probe", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

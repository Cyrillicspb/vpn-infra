#!/usr/bin/env python3
"""
watchdog.py — Центральный агент управления VPN Infrastructure v4.0

Отвечает за:
  - Единый decision loop: адаптивный failover + ротация (взаимоисключающие)
  - Plugin-архитектуру стеков (hysteria2 / reality / reality-grpc / cloudflare-cdn)
  - HTTP API для telegram-bot (FastAPI, rate limiting, bearer token)
  - Комплексный мониторинг: ping, speedtest, WG peers, контейнеры, диск, DNS,
    mTLS сертификаты, DKMS, upload utilization, heartbeat на VPS
  - Надёжную доставку алертов (TelegramQueue, graceful degradation)
  - Hot reload плагинов по SIGHUP
  - Systemd watchdog ping (sd_notify WATCHDOG=1)
  - Conntrack-статистику для самообучения AllowedIPs
"""


import asyncio
import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiohttp
import psutil
import yaml
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
API_TOKEN            = os.getenv("WATCHDOG_API_TOKEN", "")
API_PORT             = int(os.getenv("WATCHDOG_PORT", "8080"))
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID    = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
VPS_IP               = os.getenv("VPS_IP", "")
VPS_TUNNEL_IP        = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")
GRAFANA_URL          = os.getenv("GRAFANA_URL", "http://172.20.0.32:3000")
GRAFANA_TOKEN        = os.getenv("GRAFANA_TOKEN", "")
DDNS_PROVIDER        = os.getenv("DDNS_PROVIDER", "")
DDNS_DOMAIN          = os.getenv("DDNS_DOMAIN", "")
DDNS_TOKEN           = os.getenv("DDNS_TOKEN", "")
CF_API_TOKEN         = os.getenv("CF_API_TOKEN", "")
NET_INTERFACE        = os.getenv("NET_INTERFACE", "eth0")
GATEWAY_IP           = os.getenv("GATEWAY_IP", "")

STATE_FILE   = Path("/opt/vpn/watchdog/state.json")
PLUGINS_DIR  = Path("/opt/vpn/watchdog/plugins")
ROUTES_DIR   = Path("/etc/vpn-routes")
LOG_FILE     = "/var/log/vpn-watchdog.log"

# Порядок по устойчивости (индекс 0 = самый устойчивый)
# zapret исключён: прямой обход DPI без VPS, работает параллельно (direct_mode=true)
STACK_ORDER = ["cloudflare-cdn", "reality-grpc", "reality", "hysteria2"]

# Пороги мониторинга
RTT_DEGRADATION_FACTOR   = 3.0   # RTT > 3× baseline → деградация
RTT_BASELINE_WINDOW      = 7 * 24 * 3600 // 10   # 7 дней при опросе каждые 10 с
THROUGHPUT_DEGRADATION    = 0.5   # throughput < 50% baseline → шейпинг
PEER_STALE_SECONDS        = 180
DISK_WARN_PCT             = 85
DISK_CLEAN_PCT            = 80
DISK_AGGRESSIVE_PCT       = 90
DISK_EMERGENCY_PCT        = 95
UPLOAD_ALERT_PCT          = 80
CERT_WARN_CLIENT_DAYS     = 14
CERT_WARN_CA_DAYS         = 30
ROUTES_CACHE_ALERT_DAYS   = 3
ALL_STACKS_DOWN_MINUTES   = 5

# ---------------------------------------------------------------------------
# DPI bypass (zapret lane)
# ---------------------------------------------------------------------------
DPI_FWMARK       = "0x2"
DPI_TABLE        = 201
DPI_DNSMASQ_CONF = Path("/opt/vpn/dnsmasq/dnsmasq.d/dpi-domains.conf")
DPI_VPS_DNS      = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")

DPI_SERVICE_PRESETS: dict[str, dict] = {
    "youtube": {
        "display": "YouTube",
        "domains": [
            "youtube.com", "googlevideo.com", "ytimg.com",
            "yt3.ggpht.com", "youtu.be",
        ],
    },
    "twitch": {
        "display": "Twitch",
        "domains": [
            "twitch.tv", "twitchsvc.net", "jtvnw.net",
            "static.twitchsvc.net",
        ],
    },
    "discord": {
        "display": "Discord",
        "domains": [
            "discord.com", "discordapp.com", "discordapp.net",
            "discord.gg", "discord.media",
        ],
    },
}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
async def run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Асинхронный запуск команды."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        return 1, "", f"Timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _notify_systemd(msg: bytes) -> None:
    """Отправка уведомления systemd через NOTIFY_SOCKET."""
    notify_socket = os.getenv("NOTIFY_SOCKET", "")
    if not notify_socket:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if notify_socket.startswith("@"):
            notify_socket = "\0" + notify_socket[1:]
        sock.sendto(msg, notify_socket)
        sock.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TelegramQueue — надёжная доставка алертов
# ---------------------------------------------------------------------------
class TelegramQueue:
    """
    Очередь Telegram-алертов с retry при недоступности API.
    Сообщения не теряются: накапливаются и отправляются при восстановлении.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._running = False

    def enqueue(self, text: str, chat_id: str = "") -> None:
        target = chat_id or TELEGRAM_ADMIN_ID
        if target and TELEGRAM_BOT_TOKEN:
            self._queue.put_nowait((text, target))

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                text, chat_id = await asyncio.wait_for(self._queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            await self._deliver(text, chat_id)
            self._queue.task_done()

    async def _deliver(self, text: str, chat_id: str) -> None:
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        return
                    if resp.status == 400:
                        # Markdown parse error (напр. _ в reason string) — plain text
                        resp2 = await session.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                        if resp2.status == 200:
                            return
                        return  # не ретраить
            except Exception as exc:
                logger.debug(f"Telegram недоступен (попытка {attempt + 1}): {exc}")
            await asyncio.sleep(min(30, 5 * (attempt + 1)))

    def stop(self) -> None:
        self._running = False


tg = TelegramQueue()


def alert(text: str, chat_id: str = "") -> None:
    """Добавить алерт в очередь Telegram."""
    logger.info(f"ALERT: {text[:120]}")
    ts = datetime.now().strftime("%d.%m %H:%M")
    tg.enqueue(f"{text}\n\n🕐 {ts}", chat_id)


# ---------------------------------------------------------------------------
# Plugin Manager
# ---------------------------------------------------------------------------
class Plugin:
    """Обёртка над плагином стека."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.name = directory.name
        meta_path = directory / "metadata.yaml"
        with open(meta_path) as f:
            self.meta: dict[str, Any] = yaml.safe_load(f)
        self.resilience: int = self.meta.get("resilience", 0)

    async def _run(self, *args: str, timeout: int = 30) -> tuple[int, str, str]:
        client = self.dir / "client.py"
        return await run_cmd([sys.executable, str(client), *args], timeout=timeout)

    async def start(self, temp_port: str = "") -> bool:
        args = ["start"]
        if temp_port:
            args += [f"--temp-port={temp_port}"]
        rc, out, err = await self._run(*args, timeout=40)
        if rc != 0:
            logger.error(f"[{self.name}] start failed: {err.strip()}")
        return rc == 0

    async def stop(self) -> bool:
        rc, _, err = await self._run("stop", timeout=15)
        if rc != 0:
            logger.warning(f"[{self.name}] stop: {err.strip()}")
        return rc == 0

    async def test(self, timeout: int = 10) -> tuple[bool, float]:
        """Возвращает (работает, throughput_mbps)."""
        rc, out, err = await self._run("test", timeout=timeout + 5)
        if rc == 0:
            try:
                data = json.loads(out)
                return True, float(data.get("throughput_mbps", 5.0))
            except Exception:
                return True, 5.0
        return False, 0.0

    async def rotate(self) -> bool:
        rc, _, err = await self._run("rotate", timeout=40)
        if rc != 0:
            logger.error(f"[{self.name}] rotate: {err.strip()}")
        return rc == 0


class PluginManager:
    """Загрузка и управление плагинами стеков."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def load(self) -> None:
        loaded = []
        if not PLUGINS_DIR.exists():
            logger.warning(f"Директория плагинов не найдена: {PLUGINS_DIR}")
            return
        for d in sorted(PLUGINS_DIR.iterdir()):
            if d.is_dir() and (d / "metadata.yaml").exists() and (d / "client.py").exists():
                try:
                    p = Plugin(d)
                    self._plugins[p.name] = p
                    loaded.append(f"{p.name}(resilience={p.resilience})")
                except Exception as exc:
                    logger.error(f"Ошибка загрузки плагина {d.name}: {exc}")
        logger.info(f"Плагины загружены: {', '.join(loaded) or 'нет'}")

    def reload(self) -> None:
        logger.info("Перезагрузка плагинов (SIGHUP)...")
        self._plugins.clear()
        self.load()

    def get(self, name: str) -> Optional[Plugin]:
        return self._plugins.get(name)

    def all_names(self) -> list[str]:
        """Список имён по убыванию устойчивости (высокий resilience → первый)."""
        return [
            p.name for p in sorted(
                self._plugins.values(), key=lambda p: p.resilience, reverse=True
            )
        ]

    def names_list(self) -> list[dict]:
        return [
            {"name": p.name, "display": p.meta.get("display_name", p.name),
             "resilience": p.resilience}
            for p in sorted(self._plugins.values(), key=lambda p: p.resilience, reverse=True)
        ]


plugins = PluginManager()


# ---------------------------------------------------------------------------
# Watchdog State
# ---------------------------------------------------------------------------
class WatchdogState:
    def __init__(self) -> None:
        self.active_stack: str = "cloudflare-cdn"
        self.primary_stack: str = "cloudflare-cdn"
        # RTT sliding window (10-секундные отсчёты, 7 дней)
        self.rtt_baseline: dict[str, deque] = {s: deque(maxlen=RTT_BASELINE_WINDOW) for s in STACK_ORDER}
        # Throughput baseline (Mbps, последние 50 измерений)
        self.throughput_baseline: dict[str, deque] = {s: deque(maxlen=50) for s in STACK_ORDER}
        # Speedtest baselines (Mbps)
        self.small_speedtest: deque = deque(maxlen=100)   # 100KB каждые 5 мин
        self.large_speedtest: deque = deque(maxlen=20)    # 10MB каждые 6 ч
        self.last_failover: Optional[datetime] = None
        self.last_rotation: Optional[datetime] = None
        self.next_rotation: datetime = datetime.now() + timedelta(minutes=random.randint(30, 60))
        self.failover_in_progress: bool = False
        self.rotation_in_progress: bool = False
        self.all_stacks_down_since: Optional[datetime] = None
        self.vps_list: list[dict] = []          # [{ip, ssh_port, tunnel_ip, active}]
        self.active_vps_idx: int = 0
        self.external_ip: str = ""
        self.started_at: datetime = datetime.now()
        self.degraded_mode: bool = False
        self.is_first_run: bool = True
        self.last_full_assessment: Optional[datetime] = None
        self.peer_lock = asyncio.Lock()         # mutex /peer/add
        # Дедупликация алертов о stale peers: {iface:pubkey → timestamp последнего алерта}
        self.stale_peer_alerted: dict[str, float] = {}
        # ── Метрики для Prometheus /metrics ──────────────────────────────────────
        self.last_rtt: float = 0.0              # последний RTT (ms)
        self.ping_results: deque = deque(maxlen=30)  # 1=ok, 0=fail (для packet_loss_pct)
        self.last_download_mbps: float = 0.0   # последний speedtest (Mbps)
        self.upload_util_pct: float = 0.0      # утилизация upload-канала %
        self.blocked_sites_reachable: int = 1  # 1=OK, 0=недоступны
        self.failover_count: int = 0           # счётчик failover-переключений
        self.dnsmasq_up: int = 1               # 1=работает, 0=нет
        self.docker_health: dict[str, int] = {}  # {container: 1/0}
        self.cached_peers: list[dict] = []      # последний дамп WG пиров
        # ── DPI bypass (zapret lane) ─────────────────────────────────────────
        self.dpi_enabled: bool = False          # глобальный on/off
        self.dpi_services: list[dict] = []      # [{name, display, domains, enabled}]

    @property
    def active_vps(self) -> Optional[dict]:
        if not self.vps_list:
            return None
        return self.vps_list[self.active_vps_idx % len(self.vps_list)]

    def rtt_avg(self, stack: str) -> float:
        window = self.rtt_baseline.get(stack, deque())
        return sum(window) / len(window) if window else 0.0

    def throughput_avg(self, stack: str) -> float:
        window = self.throughput_baseline.get(stack, deque())
        return sum(window) / len(window) if window else 0.0

    def to_dict(self) -> dict:
        return {
            "active_stack": self.active_stack,
            "primary_stack": self.primary_stack,
            "last_failover": self.last_failover.isoformat() if self.last_failover else None,
            "last_rotation": self.last_rotation.isoformat() if self.last_rotation else None,
            "next_rotation": self.next_rotation.isoformat(),
            "external_ip": self.external_ip,
            "started_at": self.started_at.isoformat(),
            "degraded_mode": self.degraded_mode,
            "is_first_run": self.is_first_run,
            "vps_list": self.vps_list,
            "active_vps_idx": self.active_vps_idx,
            "dpi_enabled": self.dpi_enabled,
            "dpi_services": self.dpi_services,
        }

    def save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.to_dict(), indent=2))
        except Exception as exc:
            logger.error(f"Не удалось сохранить состояние: {exc}")

    def load(self) -> None:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                self.active_stack   = data.get("active_stack", "cloudflare-cdn")
                self.primary_stack  = data.get("primary_stack", "cloudflare-cdn")
                self.external_ip    = data.get("external_ip", "")
                self.vps_list       = data.get("vps_list", [])
                self.active_vps_idx = data.get("active_vps_idx", 0)
                self.degraded_mode  = data.get("degraded_mode", False)
                self.is_first_run   = data.get("is_first_run", True)
                self.dpi_enabled    = data.get("dpi_enabled", False)
                self.dpi_services   = data.get("dpi_services", [])
                logger.info(f"Состояние загружено: стек={self.active_stack}, dpi={self.dpi_enabled}")
        except Exception as exc:
            logger.error(f"Не удалось загрузить состояние: {exc}")


state = WatchdogState()


# ---------------------------------------------------------------------------
# Мониторинг: ping VPS через tun
# ---------------------------------------------------------------------------
async def ping_vps(target: str = "") -> tuple[bool, float]:
    """Проверяет связь через активный стек (curl via SOCKS5). Возвращает (success, rtt_ms)."""
    plugin = plugins.get(state.active_stack)

    # direct_mode (zapret): нет SOCKS5 прокси, пинг = прямой curl через eth0
    if plugin and plugin.meta.get("direct_mode"):
        import time as _time
        start = _time.time()
        eth = NET_INTERFACE or "eth0"
        rc, out, _ = await run_cmd(
            ["curl", "-s", "--max-time", "8", "--interface", eth,
             "-o", "/dev/null", "-w", "%{http_code}",
             "http://www.gstatic.com/generate_204"],
            timeout=12,
        )
        elapsed_ms = (_time.time() - start) * 1000
        if rc == 0 and out.strip() in ("200", "204", "301", "302"):
            return True, elapsed_ms
        return False, 0.0

    socks_port = 1080
    if plugin:
        cy_path = plugin.dir / "client.yaml"
        if cy_path.exists():
            try:
                import yaml as _yaml
                cy = _yaml.safe_load(cy_path.read_text())
                # Формат watchdog-плагинов: socks_port: 1081
                # Формат hysteria2 binary config: socks5.listen: 127.0.0.1:1083
                sp = cy.get("socks_port")
                if sp is None:
                    listen = cy.get("socks5", {}).get("listen", "127.0.0.1:1080")
                    sp = listen.split(":")[-1]
                socks_port = int(sp)
            except Exception:
                pass
    import time as _time
    start = _time.time()
    rc, out, _ = await run_cmd(
        ["curl", "-s", "--max-time", "8",
         "--proxy", f"socks5://127.0.0.1:{socks_port}",
         "-o", "/dev/null", "-w", "%{http_code}",
         "http://www.gstatic.com/generate_204"],
        timeout=12,
    )
    elapsed_ms = (_time.time() - start) * 1000
    if rc == 0 and out.strip() in ("200", "204", "301", "302"):
        return True, elapsed_ms
    return False, 0.0


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------
SPEED_URL_SMALL = "https://speed.cloudflare.com/__down?bytes=102400"    # 100 KB (через VPN)
SPEED_URL_LARGE = "https://speed.cloudflare.com/__down?bytes=10485760"  # 10 MB  (через VPN)

# Российские speedtest-серверы для ISP-теста (не заблокированы провайдером).
# Проверены по убыванию приоритета: первый рабочий используется.
DIRECT_TEST_SERVERS = [
    "http://speedtest.corbina.ru/speedtest/random4000x4000.jpg",   # Beeline/Corbina ~4 MB
    "http://speedtest.corbina.ru/speedtest/random1000x1000.jpg",   # Beeline/Corbina ~1 MB
    "https://speedtest.megafon.ru/speedtest/random1000x1000.jpg",  # МегаФон ~1 MB
]


async def _measure_throughput(url: str, proxy: str = "") -> float:
    """Замер throughput (Mbps) через URL, опционально через прокси."""
    cmd = ["curl", "-s", "--max-time", "30", "-o", "/dev/null", "-w", "%{speed_download}", url]
    if proxy:
        cmd = ["curl", "-s", "--max-time", "30", "--proxy", proxy,
               "-o", "/dev/null", "-w", "%{speed_download}", url]
    rc, out, _ = await run_cmd(cmd, timeout=35)
    if rc == 0:
        try:
            bytes_per_sec = float(out.strip())
            return round(bytes_per_sec * 8 / 1_000_000, 2)   # Mbps
        except Exception as exc:
            logger.debug(f"_measure_throughput: не удалось распарсить вывод curl '{out.strip()}': {exc}")
    return 0.0


async def speedtest_small() -> float:
    """100KB тест через активный стек. Возвращает Mbps."""
    mbps = await _measure_throughput(SPEED_URL_SMALL)
    if mbps > 0:
        state.small_speedtest.append(mbps)
        state.last_download_mbps = mbps
    return mbps


async def speedtest_large() -> float:
    """10MB тест через активный стек. Возвращает Mbps."""
    mbps = await _measure_throughput(SPEED_URL_LARGE)
    if mbps > 0:
        state.large_speedtest.append(mbps)
    return mbps


async def speedtest_direct() -> float:
    """ISP-тест напрямую через NET_INTERFACE (без VPN). Перебирает DIRECT_TEST_SERVERS.
    Возвращает Mbps первого успешного сервера, 0.0 если все недоступны."""
    for url in DIRECT_TEST_SERVERS:
        cmd = [
            "curl", "-sL", "--max-time", "12",
            "--interface", NET_INTERFACE,
            "-o", "/dev/null", "-w", "%{speed_download} %{http_code}",
            url,
        ]
        rc, out, _ = await run_cmd(cmd, timeout=17)
        if not out.strip():
            continue
        parts = out.strip().split()
        http_code = parts[1] if len(parts) >= 2 else "0"
        if http_code != "200":
            continue
        try:
            mbps = round(float(parts[0]) * 8 / 1_000_000, 1)
            if mbps >= 1.0:
                logger.debug(f"speedtest_direct: {mbps} Mbps via {url}")
                return mbps
        except Exception:
            continue
    return 0.0


async def speedtest_iperf_vps() -> float:
    """Замер download-скорости от VPS через iperf3 (tier-2 туннель). Возвращает Mbps."""
    cmd = [
        "iperf3", "-c", VPS_TUNNEL_IP,
        "-p", "5201",
        "-t", "10",   # 10 секунд
        "-R",         # reverse: VPS → дом (download, как у стеков)
        "--json",
    ]
    rc, out, _ = await run_cmd(cmd, timeout=25)
    if rc == 0:
        try:
            import json as _json
            data = _json.loads(out)
            bits = data["end"]["sum_received"]["bits_per_second"]
            return round(bits / 1_000_000, 2)
        except Exception as e:
            logger.debug(f"iperf3: ошибка парсинга: {e}")
    return 0.0


def detect_volume_shaping() -> Optional[str]:
    """
    Детекция объёмного шейпинга: расхождение между маленьким и большим speedtest.
    Если маленький тест >> большого — шейпинг по объёму.
    """
    if len(state.small_speedtest) < 3 or len(state.large_speedtest) < 2:
        return None
    small_avg = sum(list(state.small_speedtest)[-3:]) / 3
    large_avg = sum(list(state.large_speedtest)[-2:]) / 2
    if large_avg > 0 and small_avg / large_avg > 2.0:
        return f"Объёмный шейпинг: {small_avg:.1f} Mbps (100KB) vs {large_avg:.1f} Mbps (10MB)"
    return None


# ---------------------------------------------------------------------------
# Хелпер: выбор wg/awg команды по интерфейсу
# ---------------------------------------------------------------------------
def _wg_tool(iface: str) -> str:
    """wg0 использует AmneziaWG → awg/awg-quick, остальные → wg/wg-quick."""
    return "awg" if iface == "wg0" else "wg"


def _wg_quick_tool(iface: str) -> str:
    return "awg-quick" if iface == "wg0" else "wg-quick"


# ---------------------------------------------------------------------------
# Мониторинг: WireGuard peers
# ---------------------------------------------------------------------------
PEER_STALE_REPEAT_INTERVAL = 3600  # повторный алерт о stale peer — не чаще раза в час
BOT_DB_PATH = Path("/opt/vpn/telegram-bot/data/vpn_bot.db")


def _lookup_peer_device(pubkey: str) -> str:
    """
    Найти имя устройства и владельца по публичному ключу в БД бота.
    Возвращает строку вида 'Иван / iPhone' или 'неизвестное устройство'.
    """
    if not BOT_DB_PATH.exists():
        return ""
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=3) as conn:
            row = conn.execute(
                """
                SELECT c.first_name, d.device_name
                FROM devices d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.public_key = ? OR d.peer_id = ?
                LIMIT 1
                """,
                (pubkey, pubkey),
            ).fetchone()
        if row:
            name, device = row
            parts = [p for p in [name, device] if p]
            return " / ".join(parts) if parts else ""
    except Exception:
        pass
    return ""


async def check_wg_peers() -> None:
    """Проверка stale peers (last handshake > 180 сек).

    Дедупликация: первый алерт — сразу, повторные — не чаще раза в час.
    При восстановлении пира — очищаем запись, чтобы следующий stale снова
    дал немедленный алерт.
    """
    # Собираем данные из обоих стеков (awg — wg0, wg — wg1)
    combined_out = ""
    for tool in ("awg", "wg"):
        rc, out, _ = await run_cmd([tool, "show", "all", "latest-handshakes"], timeout=10)
        if rc == 0:
            combined_out += out
    if not combined_out:
        return
    out = combined_out
    now = int(time.time())
    seen_keys: set[str] = set()
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            iface, pubkey, ts_str = parts[0], parts[1], parts[2]
            peer_key = f"{iface}:{pubkey}"
            seen_keys.add(peer_key)
            try:
                ts = int(ts_str)
                age = now - ts
                if ts > 0 and age > PEER_STALE_SECONDS:
                    last_alerted = state.stale_peer_alerted.get(peer_key, 0)
                    if now - last_alerted >= PEER_STALE_REPEAT_INTERVAL:
                        device_info = _lookup_peer_device(pubkey)
                        hours = age // 3600
                        mins  = (age % 3600) // 60
                        age_str = f"{hours}ч {mins}мин" if hours else f"{mins}мин"
                        if device_info:
                            who = f"*{device_info}*"
                        else:
                            who = f"неизвестное устройство (`{pubkey[:20]}…`)"
                        logger.warning(f"Stale peer {pubkey[:16]}… на {iface} ({age}s)")
                        alert(
                            f"⚠️ WireGuard peer не на связи\n"
                            f"Устройство: {who}\n"
                            f"Интерфейс: `{iface}`\n"
                            f"Без handshake: {age_str}"
                        )
                        state.stale_peer_alerted[peer_key] = now
                else:
                    # Пир восстановился — сбросить дедупликацию
                    state.stale_peer_alerted.pop(peer_key, None)
            except Exception as exc:
                logger.debug(f"check_wg_peers: не удалось распарсить строку '{line}': {exc}")
    # Удалить записи о пирах, которых больше нет в wg show
    for key in list(state.stale_peer_alerted):
        if key not in seen_keys:
            state.stale_peer_alerted.pop(key, None)

    # Обновить кэш пиров для /metrics (vpn_peer_count, vpn_peer_last_handshake)
    dump_out = ""
    for tool in ("awg", "wg"):
        rc, out, _ = await run_cmd([tool, "show", "all", "dump"], timeout=10)
        if rc == 0:
            dump_out += out
    peers: list[dict] = []
    for line in dump_out.strip().splitlines():
        p = line.split("\t")
        if len(p) != 9:
            continue
        try:
            peers.append({
                "interface": p[0], "public_key": p[1],
                "last_handshake": int(p[5]),
            })
        except Exception:
            continue
    state.cached_peers = peers


# ---------------------------------------------------------------------------
# Мониторинг: Docker контейнеры
# ---------------------------------------------------------------------------
async def check_containers() -> None:
    """Проверка exited/unhealthy контейнеров. Обновляет state.docker_health."""
    rc, out, _ = await run_cmd(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"], timeout=15
    )
    if rc != 0:
        return
    new_health: dict[str, int] = {}
    for line in out.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            name, status = parts
            healthy = 0 if ("Exited" in status or "unhealthy" in status.lower()) else 1
            new_health[name] = healthy
            if healthy == 0:
                alert(f"🚨 Контейнер *{name}*: `{status}`")
    state.docker_health = new_health


# ---------------------------------------------------------------------------
# Мониторинг: диск
# ---------------------------------------------------------------------------
async def check_disk() -> None:
    disk = psutil.disk_usage("/")
    pct = disk.percent

    if pct >= DISK_EMERGENCY_PCT:
        logger.critical(f"Диск АВАРИЙНО: {pct}%")
        alert(f"🚨 Диск АВАРИЙНО: *{pct}%* — остановка некритичных сервисов")
        for svc in ["homepage", "portainer"]:
            await run_cmd(["docker", "stop", svc], timeout=10)

    elif pct >= DISK_AGGRESSIVE_PCT:
        logger.error(f"Диск критично: {pct}%")
        alert(f"⚠️ Диск: *{pct}%* — агрессивная очистка Docker")
        await run_cmd(["docker", "system", "prune", "-af", "--volumes"], timeout=120)
        # Удаляем бэкапы старше 7 дней
        await run_cmd(["find", "/opt/vpn/backups", "-mtime", "+7", "-delete"], timeout=30)

    elif pct >= DISK_CLEAN_PCT:
        logger.warning(f"Диск: {pct}%")
        alert(f"ℹ️ Диск: *{pct}%* — очистка Docker")
        await run_cmd(["docker", "system", "prune", "-f"], timeout=60)

    if pct >= DISK_WARN_PCT:
        alert(f"⚠️ Диск заполнен на *{pct}%*")


# ---------------------------------------------------------------------------
# Мониторинг: upload utilization
# ---------------------------------------------------------------------------
async def check_upload_utilization() -> None:
    """Алерт если upload > UPLOAD_ALERT_PCT% от канала."""
    bw_limit_mbps = float(os.getenv("BANDWIDTH_LIMIT", "0") or "0")
    if bw_limit_mbps <= 0:
        return
    iface = NET_INTERFACE
    try:
        counters = psutil.net_io_counters(pernic=True)
        if iface not in counters:
            return
        c1 = counters[iface]
        await asyncio.sleep(2)
        c2 = psutil.net_io_counters(pernic=True)[iface]
        upload_mbps = (c2.bytes_sent - c1.bytes_sent) * 8 / 1_000_000 / 2
        pct = upload_mbps / bw_limit_mbps * 100
        state.upload_util_pct = round(pct, 1)
        if pct > UPLOAD_ALERT_PCT:
            alert(f"⚠️ Upload {upload_mbps:.1f} Mbps — *{pct:.0f}%* от канала ({bw_limit_mbps} Mbps)")
    except Exception as exc:
        logger.debug(f"check_upload: {exc}")


# ---------------------------------------------------------------------------
# Мониторинг: dnsmasq
# ---------------------------------------------------------------------------
async def check_dnsmasq() -> None:
    rc, out, _ = await run_cmd(["dig", "@127.0.0.1", "google.com", "+short", "+time=3"], timeout=10)
    if rc != 0 or not out.strip():
        state.dnsmasq_up = 0
        logger.error("dnsmasq не отвечает, перезапуск")
        alert("⚠️ dnsmasq не отвечает — перезапуск")
        await run_cmd(["systemctl", "restart", "dnsmasq"])
    else:
        state.dnsmasq_up = 1


# ---------------------------------------------------------------------------
# Мониторинг: внешний IP + DDNS
# ---------------------------------------------------------------------------
async def check_external_ip() -> None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=10)) as r:
                new_ip = (await r.text()).strip()
    except Exception:
        return

    if not new_ip or new_ip == state.external_ip:
        return

    old_ip = state.external_ip
    state.external_ip = new_ip
    state.save()
    logger.info(f"Внешний IP: {old_ip} → {new_ip}")

    if DDNS_PROVIDER:
        await _update_ddns(new_ip)
        alert(f"ℹ️ Внешний IP изменился: `{old_ip}` → `{new_ip}`\nDDNS обновлён.")
    else:
        alert(
            f"⚠️ Внешний IP изменился: `{old_ip}` → `{new_ip}`\n"
            "DDNS не настроен — нужна рассылка конфигов клиентам!"
        )


async def _update_ddns(ip: str) -> None:
    try:
        if DDNS_PROVIDER == "duckdns":
            url = f"https://www.duckdns.org/update?domains={DDNS_DOMAIN}&token={DDNS_TOKEN}&ip={ip}"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logger.info(f"DuckDNS: {await r.text()}")

        elif DDNS_PROVIDER == "cloudflare":
            # Получаем zone_id и record_id из Cloudflare API
            headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(
                    f"https://api.cloudflare.com/client/v4/zones?name={DDNS_DOMAIN}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json()
                zones = data.get("result", [])
                if not zones:
                    return
                zone_id = zones[0]["id"]
                async with s.get(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
                    f"?type=A&name={DDNS_DOMAIN}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    records = (await r.json()).get("result", [])
                if records:
                    rec_id = records[0]["id"]
                    await s.put(
                        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec_id}",
                        json={"type": "A", "name": DDNS_DOMAIN, "content": ip, "ttl": 60},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    logger.info(f"Cloudflare DDNS обновлён: {DDNS_DOMAIN} → {ip}")

        elif DDNS_PROVIDER == "noip":
            async with aiohttp.ClientSession() as s:
                await s.get(
                    f"https://dynupdate.no-ip.com/nic/update?hostname={DDNS_DOMAIN}&myip={ip}",
                    timeout=aiohttp.ClientTimeout(total=10),
                )
    except Exception as exc:
        logger.error(f"DDNS update error: {exc}")


# ---------------------------------------------------------------------------
# Мониторинг: heartbeat → VPS
# ---------------------------------------------------------------------------
async def send_heartbeat() -> None:
    """Отправка heartbeat на VPS каждые 60 сек."""
    vps = state.active_vps
    if not vps:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"http://{vps['ip']}:8081/heartbeat",
                json={"ts": datetime.now().isoformat()},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        pass   # Не алертим здесь — это делает VPS-side скрипт


# ---------------------------------------------------------------------------
# Мониторинг: заблокированные сайты через tun
# ---------------------------------------------------------------------------
BLOCKED_CHECK_URLS = ["https://youtube.com", "https://t.me"]


async def check_blocked_sites() -> None:
    plugin = plugins.get(state.active_stack)
    tun = plugin.meta.get("tun_name", f"tun-{state.active_stack}") if plugin else f"tun-{state.active_stack}"
    for url in BLOCKED_CHECK_URLS:
        rc, out, _ = await run_cmd(
            ["curl", "-s", "--max-time", "15", "--interface", tun, "-o", "/dev/null", "-w", "%{http_code}", url],
            timeout=20,
        )
        if rc != 0 or out.strip() not in ("200", "301", "302", "303"):
            state.blocked_sites_reachable = 0
            alert(f"⚠️ Заблокированный сайт *{url}* недоступен через туннель (код: {out.strip() or 'нет ответа'})")
            return
    state.blocked_sites_reachable = 1


# ---------------------------------------------------------------------------
# Мониторинг: DKMS
# ---------------------------------------------------------------------------
async def check_dkms() -> None:
    rc, out, _ = await run_cmd(["dkms", "status"], timeout=15)
    if rc == 0:
        for line in out.splitlines():
            if "installed" not in line.lower() and line.strip():
                alert(f"⚠️ DKMS модуль не собран: `{line.strip()}`")


# ---------------------------------------------------------------------------
# Мониторинг: mTLS сертификаты
# ---------------------------------------------------------------------------
async def check_certs() -> None:
    cert_paths = [
        ("/opt/vpn/nginx/mtls/client.crt", "client", CERT_WARN_CLIENT_DAYS),
        ("/opt/vpn/nginx/mtls/ca.crt",     "CA",     CERT_WARN_CA_DAYS),
    ]
    for path, label, warn_days in cert_paths:
        if not Path(path).exists():
            continue
        rc, out, _ = await run_cmd(
            ["openssl", "x509", "-enddate", "-noout", "-in", path], timeout=10
        )
        if rc == 0 and "notAfter=" in out:
            try:
                date_str = out.split("=", 1)[1].strip()
                # openssl может вернуть "Jan  1 00:00:00 2026 GMT" или "Jan 1 00:00:00 2026 GMT"
                for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
                    try:
                        expiry = datetime.strptime(date_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    logger.warning(f"check_certs: не удалось распарсить дату '{date_str}' для {label}")
                    continue
                days_left = (expiry - datetime.utcnow()).days
                if days_left <= warn_days:
                    alert(
                        f"⚠️ Сертификат *{label}* истекает через *{days_left} дн.*\n"
                        f"Путь: `{path}`\nИспользуйте /renew-cert или /renew-ca"
                    )
            except Exception as exc:
                logger.warning(f"check_certs: ошибка проверки {label} ({path}): {exc}")


# ---------------------------------------------------------------------------
# Мониторинг: кэш маршрутов
# ---------------------------------------------------------------------------
async def check_nftables_integrity() -> None:
    """
    Проверяет что правила nftables соответствуют ожидаемым (раз в час).

    Контролирует:
      - таблица inet vpn существует
      - input chain: policy drop
      - forward chain: policy drop
      - ключевые правила (SSH 22, AWG 51820, WG 51821, blocked sets)
      - sets blocked_static и blocked_dynamic существуют

    При расхождении — восстанавливает из /etc/nftables.conf и алертит.
    """
    issues: list[str] = []

    # 1. Таблица inet vpn существует?
    rc, out, _ = await run_cmd(["nft", "list", "tables"], timeout=5)
    if rc != 0 or "inet vpn" not in out:
        issues.append("таблица inet vpn отсутствует")
    else:
        # 2. input: policy drop
        rc_in, out_in, _ = await run_cmd(
            ["nft", "list", "chain", "inet", "vpn", "input"], timeout=5
        )
        if rc_in != 0 or "policy drop" not in out_in:
            issues.append("input chain: policy не drop")
        else:
            if "51820" not in out_in:
                issues.append("AWG порт 51820 отсутствует в input")
            if "51821" not in out_in:
                issues.append("WG порт 51821 отсутствует в input")
            if "dport 22" not in out_in:
                issues.append("SSH порт 22 отсутствует в input")

        # 3. forward: policy drop
        rc_fw, out_fw, _ = await run_cmd(
            ["nft", "list", "chain", "inet", "vpn", "forward"], timeout=5
        )
        if rc_fw != 0 or "policy drop" not in out_fw:
            issues.append("forward chain: policy не drop")

        # 4. Sets существуют
        rc_s, out_s, _ = await run_cmd(["nft", "list", "sets", "inet"], timeout=5)
        if rc_s == 0:
            if "blocked_static" not in out_s:
                issues.append("set blocked_static отсутствует")
            if "blocked_dynamic" not in out_s:
                issues.append("set blocked_dynamic отсутствует")

    if not issues:
        logger.debug("check_nftables_integrity: OK")
        return

    details = "; ".join(issues)
    logger.warning(f"check_nftables_integrity: расхождения: {details}")

    # Восстановить правила
    rc_r, _, err = await run_cmd(["nft", "-f", "/etc/nftables.conf"], timeout=15)
    if rc_r == 0:
        # Восстановить blocked_static (без blocked_dynamic — он self-healing через dnsmasq)
        await run_cmd(["nft", "-f", "/etc/nftables-blocked-static.conf"], timeout=15)
        alert(
            f"🔥 *nftables: правила изменены или сброшены!*\n\n"
            f"Проблемы: `{details}`\n\n"
            f"✅ Правила восстановлены из `/etc/nftables.conf`"
        )
        logger.info("check_nftables_integrity: правила восстановлены")
    else:
        alert(
            f"🔥 *nftables: правила изменены или сброшены!*\n\n"
            f"Проблемы: `{details}`\n\n"
            f"❌ Восстановление ПРОВАЛИЛОСЬ: `{err.strip()}`\n"
            f"Выполните вручную: `sudo nft -f /etc/nftables.conf`"
        )
        logger.error(f"check_nftables_integrity: восстановление провалилось: {err}")


async def check_routes_cache_age() -> None:
    per_source_dir = ROUTES_DIR
    if not per_source_dir.exists():
        return
    threshold = datetime.now() - timedelta(days=ROUTES_CACHE_ALERT_DAYS)
    for cache_file in per_source_dir.glob("*.cache"):
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        if mtime < threshold:
            age_days = (datetime.now() - mtime).days
            alert(f"⚠️ Кэш маршрутов `{cache_file.name}` устарел на *{age_days} дн.*\nИсточник недоступен?")


# ---------------------------------------------------------------------------
# Мониторинг: conntrack самообучение AllowedIPs
# ---------------------------------------------------------------------------
async def collect_conntrack_stats() -> None:
    """
    Собирает статистику conntrack раз в час.
    Определяет какие AllowedIPs-подсети идут через eth0 (не заблокированы).
    Рекомендует администратору убрать из AllowedIPs если 100% трафика через eth0.
    """
    rc, out, _ = await run_cmd(
        ["conntrack", "-L", "--proto", "tcp", "-o", "extended"], timeout=30
    )
    if rc != 0:
        return

    eth0_dsts: set[str] = set()
    tun_dsts:  set[str] = set()

    for line in out.splitlines():
        if "dst=" not in line:
            continue
        parts = line.split()
        dst = next((p.split("=")[1] for p in parts if p.startswith("dst=")), None)
        if not dst:
            continue
        if "tun" in line:
            tun_dsts.add(dst)
        elif NET_INTERFACE in line or "eth0" in line:
            eth0_dsts.add(dst)

    only_eth0 = eth0_dsts - tun_dsts
    if only_eth0:
        logger.debug(f"conntrack: {len(only_eth0)} IP только через eth0 (не заблокированы)")


# ---------------------------------------------------------------------------
# zapret: ночной full probe параметров (02:30)
# ---------------------------------------------------------------------------
async def _run_zapret_probe() -> None:
    """Запустить full probe zapret в фоне, обновить Thompson Sampling."""
    plugin_dir = PLUGINS_DIR / "zapret"
    probe_script = plugin_dir / "probe.py"
    if not probe_script.exists():
        return
    logger.info("[zapret] Запуск ночного full probe...")
    rc, out, err = await run_cmd(
        [sys.executable, str(probe_script), "full"],
        timeout=600,
    )
    if rc == 0:
        # Извлечь лучший пресет из вывода
        for line in out.splitlines():
            if "Лучший пресет:" in line:
                logger.info(f"[zapret] {line.strip()}")
                break
        logger.info("[zapret] Ночной full probe завершён")
    else:
        logger.warning(f"[zapret] full probe завершился с ошибкой: {err.strip()[:200]}")


# ---------------------------------------------------------------------------
# DPI bypass management (zapret lane)
# ---------------------------------------------------------------------------
async def _regen_dpi_dnsmasq() -> None:
    """Перегенерировать dpi-domains.conf + SIGHUP dnsmasq."""
    enabled = [s for s in state.dpi_services if s.get("enabled")]
    if not enabled:
        DPI_DNSMASQ_CONF.parent.mkdir(parents=True, exist_ok=True)
        DPI_DNSMASQ_CONF.write_text(
            "# dpi-domains.conf — нет активных DPI-bypass сервисов (генерируется watchdog)\n"
        )
    else:
        lines = [
            "# dpi-domains.conf — DPI-bypass сервисы (генерируется watchdog)",
            "# Изменять вручную не нужно — обновляется через /dpi",
            "",
        ]
        for svc in enabled:
            lines.append(f"# {svc.get('display', svc['name'])}")
            for domain in svc.get("domains", []):
                lines.append(f"nftset=/{domain}/4#inet#vpn#dpi_direct")
                lines.append(f"server=/{domain}/{DPI_VPS_DNS}")
            lines.append("")
        DPI_DNSMASQ_CONF.parent.mkdir(parents=True, exist_ok=True)
        DPI_DNSMASQ_CONF.write_text("\n".join(lines))

    rc, _, _ = await run_cmd(["pkill", "-HUP", "dnsmasq"], timeout=5)
    logger.info(f"[DPI] dpi-domains.conf обновлён ({len(enabled)} сервисов), dnsmasq SIGHUP rc={rc}")


async def _dpi_apply_routing() -> None:
    """Применить ip rule fwmark 0x2 → table 201 и маршрут в table 201."""
    rc, out, _ = await run_cmd(["ip", "rule", "show"], timeout=5)
    if f"fwmark {DPI_FWMARK} lookup {DPI_TABLE}" not in out:
        await run_cmd(
            ["ip", "rule", "add", "fwmark", DPI_FWMARK,
             "lookup", str(DPI_TABLE), "priority", "90"],
            timeout=5,
        )
    gw = GATEWAY_IP
    eth = NET_INTERFACE or "eth0"
    if gw:
        await run_cmd(
            ["ip", "route", "replace", "default", "via", gw,
             "dev", eth, "table", str(DPI_TABLE)],
            timeout=5,
        )
    logger.info(f"[DPI] ip rule fwmark {DPI_FWMARK} → table {DPI_TABLE} применён")


async def _dpi_remove_routing() -> None:
    """Убрать ip rule fwmark 0x2 и очистить nft set dpi_direct."""
    await run_cmd(
        ["ip", "rule", "del", "fwmark", DPI_FWMARK, "lookup", str(DPI_TABLE)],
        timeout=5,
    )
    await run_cmd(["nft", "flush", "set", "inet", "vpn", "dpi_direct"], timeout=5)
    logger.info("[DPI] ip rule fwmark 0x2 удалён, dpi_direct очищен")


async def _dpi_enable_impl() -> None:
    """Включить DPI bypass: routing + zapret activate + dnsmasq."""
    state.dpi_enabled = True
    state.save()
    await _dpi_apply_routing()
    zp = plugins.get("zapret")
    if zp:
        if not (await zp.test(timeout=5))[0]:
            await zp.start()
        await zp.activate()
    await _regen_dpi_dnsmasq()
    enabled_names = [s["display"] for s in state.dpi_services if s.get("enabled")]
    alert(
        f"⚡ *DPI bypass включён*\n"
        f"Сервисы: {', '.join(enabled_names) if enabled_names else 'нет (добавьте /dpi add)'}\n"
        f"Трафик к ним идёт напрямую через zapret, минуя VPS."
    )
    logger.info("[DPI] включён")


async def _dpi_disable_impl() -> None:
    """Выключить DPI bypass: routing убрать, zapret deactivate, dnsmasq очистить."""
    state.dpi_enabled = False
    state.save()
    await _dpi_remove_routing()
    zp = plugins.get("zapret")
    if zp:
        await zp.deactivate()
    await _regen_dpi_dnsmasq()
    alert("⚡ *DPI bypass выключен*\nВесь трафик идёт через VPN-туннель.")
    logger.info("[DPI] выключен")


async def _check_dpi_effectiveness() -> None:
    """Проверить что прямой канал не деградировал (каждые 30 мин при dpi_enabled)."""
    if not state.dpi_enabled:
        return
    eth = NET_INTERFACE or "eth0"
    rc, out, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10", "--interface", eth,
         "-o", "/dev/null", "-w", "%{speed_download}",
         "http://speedtest.corbina.ru/speedtest/random1000x1000.jpg"],
        timeout=15,
    )
    if rc != 0 or not out.strip():
        return
    try:
        isp_mbps = float(out.strip()) * 8 / 1_000_000
    except Exception:
        return
    if isp_mbps < 5.0:
        alert(
            f"⚠️ *zapret DPI bypass*: прямой интернет очень медленный ({isp_mbps:.1f} Mbps).\n"
            "Возможно блокировка стала IP-level или ISP деградировал.\n"
            "Проверьте: /dpi"
        )


# ---------------------------------------------------------------------------
# Мониторинг: проверка standby туннелей (04:30)
# ---------------------------------------------------------------------------
async def test_standby_tunnels() -> None:
    """Ежесуточная проверка standby стеков (test mode, без side effects)."""
    logger.info("Проверка standby туннелей...")
    current = state.active_stack
    failed = []
    for name in plugins.all_names():
        if name == current:
            continue
        plugin = plugins.get(name)
        if not plugin or plugin.meta.get("direct_mode"):
            continue
        ok, mbps = await plugin.test(timeout=15)
        if not ok:
            failed.append(name)
            alert(f"⚠️ Standby стек *{name}* не прошёл проверку")
        else:
            logger.info(f"Standby {name}: OK ({mbps:.1f} Mbps)")

    if not failed:
        logger.info("Все standby туннели в норме")


# ---------------------------------------------------------------------------
# Decision Engine — единый цикл failover + ротация
# ---------------------------------------------------------------------------
_LOCK = asyncio.Lock()   # глобальный mutex decision engine


async def _do_switch(new_stack: str, reason: str) -> bool:
    """
    Make-before-break переключение стека.
    Поднимает новый → переключает маршруты → закрывает старый.
    """
    old_stack = state.active_stack
    if old_stack == new_stack:
        return True

    plugin = plugins.get(new_stack)
    if not plugin:
        logger.error(f"Плагин {new_stack} не найден")
        return False

    logger.info(f"Переключение стека: {old_stack} → {new_stack} (причина: {reason})")

    # 1. Поднимаем новый стек (без temp_port — failover не требует make-before-break)
    ok = await plugin.start()
    if not ok:
        logger.error(f"Не удалось запустить {new_stack}")
        alert(f"⚠️ Failover на *{new_stack}* не удался")
        return False

    # 2. Атомарно переключаем маршрут table marked
    if plugin.meta.get("direct_mode"):
        # direct_mode (zapret): трафик через eth0 с nfqueue, без tun
        gw = GATEWAY_IP
        eth = NET_INTERFACE
        if gw:
            await run_cmd(
                ["ip", "route", "replace", "default", "via", gw, "dev", eth, "table", "marked"],
                timeout=5,
            )
        else:
            await run_cmd(
                ["ip", "route", "replace", "default", "dev", eth, "table", "marked"],
                timeout=5,
            )
    else:
        # Обычный режим: маршрут через tun интерфейс
        tun_name = plugin.meta.get("tun_name", f"tun-{new_stack}")
        await run_cmd(
            ["ip", "route", "replace", "default", "dev", tun_name, "table", "marked"],
            timeout=5,
        )

    # 3. Останавливаем старый стек
    old_plugin = plugins.get(old_stack)
    if old_plugin:
        await old_plugin.stop()

    state.active_stack = new_stack
    state.last_failover = datetime.now()
    state.all_stacks_down_since = None
    state.failover_count += 1
    state.save()

    logger.info(f"Стек переключён: {old_stack} → {new_stack}")
    alert(f"🔄 VPN стек переключён: *{old_stack}* → *{new_stack}*\nПричина: {reason}")
    return True


async def _failover_impl(reason: str) -> None:
    """Внутренняя логика failover. Вызывается под _LOCK (не захватывает его сама)."""
    state.failover_in_progress = True
    try:
        current = state.active_stack
        ordered = plugins.all_names()
        try:
            cur_pos = ordered.index(current)
        except ValueError:
            cur_pos = len(ordered) - 1
        candidates = ordered[:cur_pos] + ordered[cur_pos + 1:]
        for candidate in candidates:
            plugin = plugins.get(candidate)
            if not plugin or plugin.meta.get("direct_mode"):
                continue
            logger.info(f"Тест кандидата: {candidate}")
            ok, mbps = await plugin.test(timeout=10)
            if ok:
                await _do_switch(candidate, reason)
                return
        now = datetime.now()
        if state.all_stacks_down_since is None:
            state.all_stacks_down_since = now
        down_min = (now - state.all_stacks_down_since).total_seconds() / 60
        if down_min >= ALL_STACKS_DOWN_MINUTES:
            alert("🚨 *ВСЕ VPN СТЕКИ НЕДОСТУПНЫ* более 5 мин!\nПроверьте VPS вручную.")
    finally:
        state.failover_in_progress = False


async def _do_failover(reason: str) -> None:
    """Failover с захватом _LOCK. Для вызова изнутри _LOCK используй _failover_impl()."""
    async with _LOCK:
        await _failover_impl(reason)


async def _do_rotation() -> None:
    """
    Make-before-break ротация текущего стека (анти-DPI).
    """
    async with _LOCK:
        state.rotation_in_progress = True
        try:
            current = state.active_stack
            plugin = plugins.get(current)
            if not plugin:
                return

            logger.info(f"Плановая ротация: {current}")

            # Тест нового подключения перед ротацией
            ok, _ = await plugin.test(timeout=10)
            if not ok:
                logger.warning("Ротация: стек не отвечает, выполняем failover вместо ротации")
                await _failover_impl("rotation_check_failed")  # уже под _LOCK, не захватывать повторно
                return

            ok = await plugin.rotate()
            if ok:
                # Ротация пересоздаёт tun-интерфейс: ядро автоматически удаляет
                # маршрут из table marked когда старый tun исчезает.
                # Восстанавливаем маршрут после успешной ротации.
                tun_name = plugin.meta.get("tun_name", f"tun-{current}")
                await run_cmd(
                    ["ip", "route", "replace", "default", "dev", tun_name, "table", "marked"],
                    timeout=5,
                )
                state.last_rotation = datetime.now()
                logger.info(f"Ротация {current} выполнена, маршрут table marked → {tun_name} восстановлен")
            else:
                logger.warning(f"Ротация {current} не удалась")
        finally:
            state.rotation_in_progress = False
        # Планируем следующую ротацию
        state.next_rotation = datetime.now() + timedelta(minutes=random.randint(15, 75))


async def _first_run_assessment() -> None:
    """
    Первый запуск: начать с CDN, протестировать все стеки,
    промотировать самый быстрый работающий.
    """
    logger.info("Первый запуск: оценка всех стеков...")
    alert("🔍 Первый запуск: тестирование стеков...")

    # Стартуем с CDN (гарантированно работает)
    cdn = plugins.get("cloudflare-cdn")
    if cdn:
        await cdn.start()
        await run_cmd(["ip", "route", "replace", "default", "dev", "tun-cloudflare-cdn", "table", "200"])

    best_stack: Optional[str] = None
    best_mbps = 0.0

    for name in plugins.all_names():
        plugin = plugins.get(name)
        if not plugin or plugin.meta.get("direct_mode"):
            continue
        ok, mbps = await plugin.test(timeout=10)
        logger.info(f"Оценка {name}: {'OK' if ok else 'FAIL'} {mbps:.1f} Mbps")
        if ok and mbps > best_mbps:
            best_mbps = mbps
            best_stack = name

    if best_stack and best_stack != "cloudflare-cdn":
        await _do_switch(best_stack, "first_run_best")

    state.is_first_run = False
    state.last_full_assessment = datetime.now()
    state.save()
    alert(f"✅ Оценка завершена. Активный стек: *{state.active_stack}* ({best_mbps:.1f} Mbps)")


async def _full_reassessment() -> None:
    """
    Фоновая полная переоценка раз в час.
    Если более быстрый стек доступен — промотируем.
    """
    async with _LOCK:
        logger.info("Фоновая переоценка стеков...")
        best_stack: Optional[str] = None
        best_mbps = 0.0

        for name in plugins.all_names():
            plugin = plugins.get(name)
            if not plugin or plugin.meta.get("direct_mode"):
                continue
            ok, mbps = await plugin.test(timeout=10)
            logger.info(f"Переоценка {name}: {'OK' if ok else 'FAIL'} {mbps:.1f} Mbps")
            if ok and mbps > best_mbps:
                best_mbps = mbps
                best_stack = name

        if best_stack and best_stack != state.active_stack:
            logger.info(f"Переоценка: переключаемся на {best_stack} ({best_mbps:.1f} Mbps)")
            await _do_switch(best_stack, "hourly_reassessment")
        elif best_stack:
            logger.info(f"Переоценка: текущий стек {state.active_stack} оптимален")
        else:
            logger.warning("Переоценка: ни один стек не доступен")

    state.last_full_assessment = datetime.now()


async def decision_loop() -> None:
    """
    Единый цикл принятия решений: failover + ротация взаимоисключающие.
    Tick 10 сек.
    """
    ping_fails = 0
    logger.info("decision_loop запущен")

    if state.is_first_run:
        await _first_run_assessment()
    else:
        # Не первый запуск (рестарт): через 30 сек запустить переоценку стеков,
        # чтобы переключиться на быстрейший доступный (не ждать 1 час)
        async def _delayed_reassessment():
            await asyncio.sleep(30)
            logger.info("Запуск переоценки стеков после рестарта (30 сек задержка)...")
            await _full_reassessment()
        asyncio.create_task(_delayed_reassessment(), name="startup-reassessment")

    while True:
        try:
            ok, rtt = await ping_vps()

            state.last_rtt = rtt if ok else 0.0
            state.ping_results.append(1 if ok else 0)

            if ok:
                ping_fails = 0
                state.all_stacks_down_since = None
                state.degraded_mode = False

                # Обновляем RTT baseline
                state.rtt_baseline[state.active_stack].append(rtt)

                # Проверяем RTT деградацию
                avg = state.rtt_avg(state.active_stack)
                if avg > 0 and rtt > avg * RTT_DEGRADATION_FACTOR:
                    logger.warning(f"RTT деградация: {rtt:.0f}ms (avg {avg:.0f}ms)")
                    await _do_failover("rtt_degradation")

            else:
                ping_fails += 1
                logger.warning(f"Ping fail #{ping_fails}")
                if ping_fails >= 3:
                    ping_fails = 0
                    await _do_failover("ping_timeout")

            # Ротация (взаимоисключает с failover)
            if (
                not state.failover_in_progress
                and not state.rotation_in_progress
                and datetime.now() >= state.next_rotation
            ):
                asyncio.create_task(_do_rotation())

        except Exception as exc:
            logger.error(f"decision_loop ошибка: {exc}")

        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Мониторинг Loop
# ---------------------------------------------------------------------------
async def _watchdog_ping_loop() -> None:
    """Независимый ping systemd watchdog каждые 10 сек.
    Отдельный task — не блокируется долгими операциями monitoring_loop."""
    while True:
        _notify_systemd(b"WATCHDOG=1")
        await asyncio.sleep(10)


async def monitoring_loop() -> None:
    """Периодические проверки всех компонентов системы."""
    tick = 0
    _now = time.time()
    last_large_speedtest = _now  # не запускать при старте, только через 6ч
    last_full_assessment = _now  # не запускать при старте, только через 1ч
    last_heartbeat = 0.0
    last_conntrack = 0.0
    standby_checked_today = False
    zapret_probe_done_today = False
    last_standby_check_date = datetime.now().date()
    logger.info("monitoring_loop запущен")

    while True:
        try:
            now = time.time()
            tick += 1

            # Каждые 30 сек: dnsmasq
            if tick % 3 == 0:
                await check_dnsmasq()

            # Каждые 60 сек: heartbeat → VPS
            if now - last_heartbeat >= 60:
                await send_heartbeat()
                last_heartbeat = now

            # Каждые 5 мин: внешний IP, диск, small speedtest, блок. сайты, upload
            if tick % 30 == 0:
                await check_external_ip()
                await check_disk()
                mbps = await speedtest_small()
                logger.debug(f"Speedtest (100KB): {mbps:.1f} Mbps")
                vol_shaping = detect_volume_shaping()
                if vol_shaping:
                    alert(f"⚠️ {vol_shaping}")
                await check_blocked_sites()
                await check_upload_utilization()

            # Каждые 10 мин: WG peers, контейнеры
            if tick % 60 == 0:
                await check_wg_peers()
                await check_containers()

            # Каждые 30 мин: проверка эффективности DPI bypass
            if tick % 180 == 0:
                asyncio.create_task(_check_dpi_effectiveness())

            # Каждые 6 ч: large speedtest, кэш маршрутов, сертификаты, DKMS
            if now - last_large_speedtest >= 6 * 3600:
                mbps = await speedtest_large()
                logger.info(f"Speedtest (10MB): {mbps:.1f} Mbps")
                last_large_speedtest = now
                await check_routes_cache_age()
                await check_certs()
                await check_dkms()

            # Каждый час: полная переоценка стеков, conntrack, целостность firewall
            if now - last_full_assessment >= 3600:
                asyncio.create_task(_full_reassessment())
                last_full_assessment = now
                await collect_conntrack_stats()
                await check_nftables_integrity()

            # В 04:30 каждый день: проверка standby туннелей
            now_dt = datetime.now()
            if now_dt.date() != last_standby_check_date:
                standby_checked_today = False
                zapret_probe_done_today = False
                last_standby_check_date = now_dt.date()
            if not standby_checked_today and now_dt.hour == 4 and now_dt.minute >= 30:
                await test_standby_tunnels()
                standby_checked_today = True

            # В 02:30 каждый день: full probe zapret параметров (Thompson Sampling re-train)
            if not zapret_probe_done_today and now_dt.hour == 2 and now_dt.minute >= 30:
                zapret_plugin = plugins.get("zapret")
                if zapret_plugin:
                    logger.info("Ночной full probe zapret параметров...")
                    asyncio.create_task(_run_zapret_probe())
                zapret_probe_done_today = True

            pass  # watchdog ping отправляется из _watchdog_ping_loop()

        except Exception as exc:
            logger.error(f"monitoring_loop ошибка: {exc}")

        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# FastAPI + Rate Limiting
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="VPN Watchdog API", version="4.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

security = HTTPBearer(auto_error=False)


def _auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> bool:
    if not API_TOKEN:
        return True
    if credentials is None or credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ---------------------------------------------------------------------------
# Pydantic модели
# ---------------------------------------------------------------------------
class SwitchRequest(BaseModel):
    stack: str

class PeerAddRequest(BaseModel):
    name: str
    protocol: str       # "awg" | "wg"
    public_key: Optional[str] = None

class PeerRemoveRequest(BaseModel):
    peer_id: str
    interface: Optional[str] = None

class RouteRequest(BaseModel):
    action: str         # "add" | "remove"
    domain: str
    direction: str      # "vpn" | "direct"

class DeployRequest(BaseModel):
    version: Optional[str] = None
    force: bool = False

class ServiceRestartRequest(BaseModel):
    service: str

class ServiceUpdateRequest(BaseModel):
    service: str        # "all" | конкретный контейнер

class NotifyClientsRequest(BaseModel):
    message: str

class GraphRequest(BaseModel):
    panel: str = "tunnel"   # tunnel | speed | clients | system
    period: str = "1h"      # 1h | 6h | 24h | 7d

class VpsRequest(BaseModel):
    ip: str

class DpiServiceRequest(BaseModel):
    name: str = ""
    display: Optional[str] = None
    domains: Optional[list[str]] = None
    preset: Optional[str] = None   # "youtube" | "twitch" | "discord"

class DpiToggleRequest(BaseModel):
    name: str
    enabled: bool
    ssh_port: int = 443
    tunnel_ip: str = ""


# ---------------------------------------------------------------------------
# API Endpoints — GET
# ---------------------------------------------------------------------------
@app.get("/status")
async def get_status(_: bool = Depends(_auth)):
    disk = psutil.disk_usage("/")
    ram  = psutil.virtual_memory()
    return {
        "status": "degraded" if state.degraded_mode else "ok",
        "active_stack": state.active_stack,
        "primary_stack": state.primary_stack,
        "external_ip": state.external_ip,
        "uptime_seconds": int((datetime.now() - state.started_at).total_seconds()),
        "last_failover": state.last_failover.isoformat() if state.last_failover else None,
        "last_rotation": state.last_rotation.isoformat() if state.last_rotation else None,
        "next_rotation": state.next_rotation.isoformat(),
        "degraded_mode": state.degraded_mode,
        "failover_in_progress": state.failover_in_progress,
        "plugins": plugins.names_list(),
        "vps_list": state.vps_list,
        "system": {
            "disk_percent": disk.percent,
            "ram_percent": ram.percent,
            "cpu_percent": psutil.cpu_percent(interval=0.5),
        },
    }


@app.get("/metrics")
async def get_metrics(_: bool = Depends(_auth)):
    """Метрики для Prometheus (text/plain)."""
    net  = psutil.net_io_counters()
    disk = psutil.disk_usage("/")
    ram  = psutil.virtual_memory()

    stack_idx = STACK_ORDER.index(state.active_stack) if state.active_stack in STACK_ORDER else -1
    tunnel_up = 1 if state.last_rtt > 0 else 0
    rtt_baseline = round(state.rtt_avg(state.active_stack), 1)

    # Packet loss из скользящего окна последних 30 пингов
    if state.ping_results:
        loss_pct = round(state.ping_results.count(0) / len(state.ping_results) * 100, 1)
    else:
        loss_pct = 0.0

    lines = [
        # Туннель
        f'vpn_tunnel_up {tunnel_up}',
        f'vpn_tunnel_rtt_ms {round(state.last_rtt, 1)}',
        f'vpn_tunnel_rtt_baseline_ms {rtt_baseline}',
        f'vpn_tunnel_packet_loss_pct {loss_pct}',
        f'vpn_tunnel_download_mbps {state.last_download_mbps}',
        f'vpn_tunnel_upload_mbps 0',          # upload тест не реализован
        f'vpn_tunnel_upload_util_pct {state.upload_util_pct}',
        f'vpn_blocked_sites_reachable {max(0, state.blocked_sites_reachable)}',
        f'vpn_failover_total {state.failover_count}',
        # Стек
        f'vpn_active_stack{{stack="{state.active_stack}"}} {stack_idx}',
        f'vpn_degraded_mode {int(state.degraded_mode)}',
        # dnsmasq
        f'vpn_dnsmasq_up {state.dnsmasq_up}',
        # Система
        f'vpn_bytes_sent_total {net.bytes_sent}',
        f'vpn_bytes_recv_total {net.bytes_recv}',
        f'vpn_disk_used_percent {disk.percent}',
        f'vpn_ram_used_percent {ram.percent}',
        f'vpn_cpu_percent {psutil.cpu_percent(interval=0.1)}',
        f'vpn_uptime_seconds {int((datetime.now() - state.started_at).total_seconds())}',
    ]

    # Docker health per-container
    for container, healthy in state.docker_health.items():
        lines.append(f'vpn_docker_healthy{{container="{container}"}} {healthy}')

    # WG peer metrics
    from collections import Counter as _Counter
    iface_counts = _Counter(p.get("interface") for p in state.cached_peers)
    for iface, count in iface_counts.items():
        lines.append(f'vpn_peer_count{{interface="{iface}"}} {count}')
    # Per-peer last handshake
    for peer in state.cached_peers:
        iface  = peer.get("interface", "wg0")
        pubkey = peer.get("public_key", "")
        hs     = peer.get("last_handshake", 0)
        lines.append(f'vpn_peer_last_handshake{{interface="{iface}",pubkey="{pubkey[:24]}"}} {hs}')

    return Response(content="\n".join(lines), media_type="text/plain")


@app.get("/peer/list")
async def get_peer_list(_: bool = Depends(_auth)):
    combined_out = ""
    for tool in ("awg", "wg"):
        rc, out, _ = await run_cmd([tool, "show", "all", "dump"], timeout=10)
        if rc == 0:
            combined_out += out
    # awg/wg show all dump формат строки пира (9 полей):
    # iface  pubkey  psk  endpoint  allowed_ips  last_handshake  rx_bytes  tx_bytes  keepalive
    # Интерфейсные строки имеют другое число полей (5 для wg, 21+ для awg) — пропускаем их.
    peers = []
    for line in combined_out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 9:
            continue  # интерфейсная строка или нестандартный вывод
        iface, pubkey, _psk, endpoint, allowed_ips, handshake_ts, rx, tx, _ka = parts
        try:
            hs_int = int(handshake_ts)
        except ValueError:
            continue
        peers.append({
            "interface":      iface,
            "public_key":     pubkey,
            "endpoint":       endpoint if endpoint != "(none)" else None,
            "allowed_ips":    allowed_ips,
            "last_handshake": hs_int,
            "rx_bytes":       int(rx) if rx.isdigit() else 0,
            "tx_bytes":       int(tx) if tx.isdigit() else 0,
        })
    return {"peers": peers, "count": len(peers)}


@app.get("/vps/list")
async def get_vps_list(_: bool = Depends(_auth)):
    vps_list = list(state.vps_list)
    # Всегда включаем первичный VPS из конфига если он не в списке
    if VPS_IP and not any(v["ip"] == VPS_IP for v in vps_list):
        vps_list.insert(0, {
            "ip": VPS_IP,
            "ssh_port": int(os.getenv("VPS_SSH_PORT", "443")),
            "tunnel_ip": VPS_TUNNEL_IP,
        })
    return {"vps_list": vps_list, "active_idx": state.active_vps_idx}


# ---------------------------------------------------------------------------
# API Endpoints — POST (rate limit: 10/sec)
# ---------------------------------------------------------------------------
@app.post("/switch")
@limiter.limit("10/second")
async def post_switch(request: Request, req: SwitchRequest, _: bool = Depends(_auth)):
    available = plugins.all_names()
    if req.stack not in available:
        raise HTTPException(status_code=400, detail=f"Неизвестный стек: {req.stack}. Доступны: {available}")
    asyncio.create_task(_do_switch(req.stack, "manual"))
    return {"status": "switching", "target_stack": req.stack}


@app.post("/peer/add")
@limiter.limit("10/second")
async def post_peer_add(request: Request, req: PeerAddRequest,
                        bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(_peer_add_task, req)
    return {"status": "queued", "name": req.name}


async def _peer_add_task(req: PeerAddRequest) -> None:
    async with state.peer_lock:      # mutex — защита от race condition
        iface = "wg0" if req.protocol.lower() == "awg" else "wg1"

        if req.public_key:
            pubkey = req.public_key
        else:
            # Генерируем пару ключей
            rc, privkey, _ = await run_cmd(["wg", "genkey"])
            privkey = privkey.strip()
            proc = await asyncio.create_subprocess_exec(
                "wg", "pubkey",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate(privkey.encode())
            pubkey = out.decode().strip()

        # Выбираем свободный IP
        wg = _wg_tool(iface)
        rc, used_out, _ = await run_cmd([wg, "show", iface, "allowed-ips"], timeout=10)
        used_ips = set()
        for line in used_out.splitlines():
            for part in line.split():
                if "/" in part:
                    used_ips.add(part.split("/")[0])

        subnet = "10.177.1" if iface == "wg0" else "10.177.3"
        peer_ip = None
        for i in range(2, 254):
            candidate = f"{subnet}.{i}"
            if candidate not in used_ips:
                peer_ip = candidate
                break

        if not peer_ip:
            logger.error(f"IP pool исчерпан для {iface}")
            return

        rc, _, err = await run_cmd(
            [wg, "set", iface, "peer", pubkey, "allowed-ips", f"{peer_ip}/32"],
            timeout=10,
        )
        if rc == 0:
            await run_cmd([_wg_quick_tool(iface), "save", iface], timeout=10)
            logger.info(f"Peer добавлен: {req.name} → {peer_ip} на {iface}")
        else:
            logger.error(f"Ошибка добавления peer {req.name}: {err}")


@app.post("/peer/remove")
@limiter.limit("10/second")
async def post_peer_remove(request: Request, req: PeerRemoveRequest, _: bool = Depends(_auth)):
    for iface in ([req.interface] if req.interface else ["wg0", "wg1"]):
        wg = _wg_tool(iface)
        rc, _, _ = await run_cmd(
            [wg, "set", iface, "peer", req.peer_id, "remove"], timeout=10
        )
        if rc == 0:
            await run_cmd([_wg_quick_tool(iface), "save", iface], timeout=10)
            return {"status": "removed", "peer_id": req.peer_id, "interface": iface}
    raise HTTPException(status_code=404, detail="Peer не найден")


@app.post("/routes/update")
@limiter.limit("10/second")
async def post_routes_update(request: Request, bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(_routes_update_task)
    return {"status": "accepted", "message": "Обновление маршрутов запущено"}


async def _routes_update_task() -> None:
    logger.info("Обновление маршрутов...")
    rc, _, err = await run_cmd(
        [sys.executable, "/opt/vpn/scripts/update-routes.py"], timeout=300
    )
    if rc == 0:
        alert("✅ Маршруты обновлены")
    else:
        alert(f"⚠️ Ошибка обновления маршрутов:\n`{err[:300]}`")


@app.post("/service/restart")
@limiter.limit("10/second")
async def post_service_restart(request: Request, req: ServiceRestartRequest, _: bool = Depends(_auth)):
    allowed = {"dnsmasq", "watchdog", "hysteria2", "nftables", "docker",
               "wg-quick@wg0", "wg-quick@wg1"}
    if req.service not in allowed:
        raise HTTPException(status_code=400, detail=f"Сервис '{req.service}' не разрешён")
    rc, _, err = await run_cmd(["systemctl", "restart", req.service], timeout=30)
    return {"status": "ok" if rc == 0 else "error", "service": req.service,
            "error": err.strip() if rc != 0 else None}


@app.post("/service/update")
@limiter.limit("10/second")
async def post_service_update(request: Request, req: ServiceUpdateRequest,
                               bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(_service_update_task, req.service)
    return {"status": "accepted", "service": req.service}


async def _service_update_task(service: str) -> None:
    if service == "all":
        rc, _, err = await run_cmd(
            ["docker", "compose", "-f", "/opt/vpn/docker-compose.yml", "pull"], timeout=300
        )
        if rc == 0:
            await run_cmd(
                ["docker", "compose", "-f", "/opt/vpn/docker-compose.yml", "up", "-d"], timeout=120
            )
            alert("✅ Docker образы обновлены")
        else:
            alert(f"⚠️ Ошибка обновления образов:\n`{err[:300]}`")
    else:
        compose = ["-f", "/opt/vpn/docker-compose.yml"]
        rc, _, err = await run_cmd(
            ["docker", "compose", *compose, "pull", service], timeout=120
        )
        if rc == 0:
            rc, _, err = await run_cmd(
                ["docker", "compose", *compose, "up", "-d", service], timeout=60
            )
        alert(f"{'✅' if rc == 0 else '⚠️'} Обновление *{service}*: {'OK' if rc == 0 else err[:200]}")


@app.post("/deploy")
@limiter.limit("10/second")
async def post_deploy(request: Request, req: DeployRequest,
                      bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(_deploy_task, req)
    return {"status": "accepted"}


async def _deploy_task(req: DeployRequest) -> None:
    cmd = ["bash", "/opt/vpn/deploy.sh"]
    if req.force:
        cmd.append("--force")
    if req.version:
        cmd += ["--version", req.version]
    rc, _, err = await run_cmd(cmd, timeout=600)
    if rc != 0:
        alert(f"❌ Deploy завершился с ошибкой:\n`{err[:300]}`")


@app.post("/rollback")
@limiter.limit("10/second")
async def post_rollback(request: Request, bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(run_cmd, ["bash", "/opt/vpn/deploy.sh", "--rollback"], 300)
    return {"status": "rolling_back"}


@app.post("/reload-plugins")
@limiter.limit("10/second")
async def post_reload_plugins(request: Request, _: bool = Depends(_auth)):
    plugins.reload()
    return {"status": "reloaded", "plugins": plugins.names_list()}


@app.post("/notify-clients")
@limiter.limit("10/second")
async def post_notify_clients(request: Request, req: NotifyClientsRequest, _: bool = Depends(_auth)):
    # telegram-bot обрабатывает рассылку сам — здесь только сохраняем сигнал
    alert(f"📢 Рассылка клиентам:\n{req.message}")
    return {"status": "queued", "message": req.message}




async def _manual_reassessment() -> None:
    """Тест всех стеков по запросу пользователя — отправляет отчёт в Telegram."""
    alert("🔍 Запуск теста скорости всех стеков...")

    # Базовые линии последовательно — параллельный запуск насыщает канал
    # и мешает точному измерению каждого теста
    vps_mbps = await speedtest_iperf_vps()
    direct_mbps = await speedtest_direct()

    results: list[tuple[str, bool, float]] = []
    async with _LOCK:
        for name in plugins.all_names():
            plugin = plugins.get(name)
            if not plugin:
                continue
            ok, mbps = await plugin.test(timeout=10)
            results.append((name, ok, mbps))

    best_stack: Optional[str] = None
    best_mbps = 0.0
    lines = []

    # Базовые линии
    if direct_mbps > 0:
        lines.append(f"🌐 ISP ({NET_INTERFACE}): {direct_mbps:.1f} Mbps")
    else:
        lines.append(f"🌐 ISP ({NET_INTERFACE}): недоступно")
    if vps_mbps > 0:
        # Процент ISP показываем только если ISP тест дал осмысленное значение
        pct_vps = f"  ({round(vps_mbps / direct_mbps * 100)}% ISP)" if direct_mbps >= 1.0 else ""
        lines.append(f"🔒 VPS tier-2 (iperf3): {vps_mbps:.1f} Mbps{pct_vps}")
    else:
        lines.append("🔒 VPS tier-2 (iperf3): недоступно")
    lines.append("─" * 28)

    # Базовая линия для процентов стеков — канал до VPS (он реалистичнее ISP)
    base_mbps = vps_mbps if vps_mbps > 0 else direct_mbps

    for name, ok, mbps in results:
        icon = "✅" if ok else "❌"
        marker = " ← активный" if name == state.active_stack else ""
        if ok:
            pct = f"  ({round(mbps / base_mbps * 100)}%)" if base_mbps > 0 else ""
            speed = f"{mbps:.1f} Mbps{pct}"
        else:
            speed = "недоступен"
        lines.append(f"{icon} {name}: {speed}{marker}")
        if ok and mbps > best_mbps:
            best_mbps, best_stack = mbps, name

    report = "\n".join(lines)
    if best_stack and best_stack != state.active_stack:
        async with _LOCK:
            await _do_switch(best_stack, "manual_reassessment")
        alert(
            f"📊 Тест завершён:\n\n{report}\n\n"
            f"🔄 Переключено на <b>{best_stack}</b> ({best_mbps:.1f} Mbps)"
        )
    elif best_stack:
        alert(
            f"📊 Тест завершён:\n\n{report}\n\n"
            f"✅ Текущий стек <b>{state.active_stack}</b> уже оптимален"
        )
    else:
        alert(f"📊 Тест завершён:\n\n{report}\n\n⚠️ Все стеки недоступны")

    state.last_full_assessment = datetime.now()


@app.post("/assess")
@limiter.limit("10/second")
async def post_assess(request: Request, _: bool = Depends(_auth)):
    if state.failover_in_progress or state.rotation_in_progress:
        raise HTTPException(status_code=409, detail="Уже выполняется переключение")
    stacks = plugins.all_names()
    asyncio.create_task(_manual_reassessment(), name="manual-reassessment")
    return {"status": "started", "stacks": stacks, "eta_seconds": len(stacks) * 10 + 15}


# ---------------------------------------------------------------------------
# DPI bypass API
# ---------------------------------------------------------------------------
@app.get("/dpi/status")
async def get_dpi_status(_: bool = Depends(_auth)):
    import re as _re
    rc, out, _ = await run_cmd(
        ["nft", "list", "set", "inet", "vpn", "dpi_direct"], timeout=5
    )
    ip_count = len(_re.findall(r'\d+\.\d+\.\d+\.\d+', out)) if rc == 0 else 0
    zp = plugins.get("zapret")
    zapret_ok = False
    if zp:
        try:
            zapret_ok, _ = await zp.test(timeout=5)
        except Exception:
            pass
    return {
        "enabled": state.dpi_enabled,
        "zapret_running": zapret_ok,
        "services": state.dpi_services,
        "presets": list(DPI_SERVICE_PRESETS.keys()),
        "dpi_direct_ip_count": ip_count,
    }


@app.post("/dpi/enable")
@limiter.limit("10/second")
async def post_dpi_enable(request: Request, _: bool = Depends(_auth)):
    asyncio.create_task(_dpi_enable_impl())
    return {"status": "enabling"}


@app.post("/dpi/disable")
@limiter.limit("10/second")
async def post_dpi_disable(request: Request, _: bool = Depends(_auth)):
    asyncio.create_task(_dpi_disable_impl())
    return {"status": "disabling"}


@app.post("/dpi/service/add")
@limiter.limit("10/second")
async def post_dpi_service_add(request: Request, req: DpiServiceRequest,
                               _: bool = Depends(_auth)):
    if req.preset:
        if req.preset not in DPI_SERVICE_PRESETS:
            raise HTTPException(400, f"Неизвестный пресет: {req.preset}. "
                                f"Доступны: {list(DPI_SERVICE_PRESETS)}")
        preset = DPI_SERVICE_PRESETS[req.preset]
        name, display, domains = req.preset, preset["display"], preset["domains"]
    else:
        if not req.name or not req.domains:
            raise HTTPException(400, "Требуется name + domains, или preset")
        name = req.name
        display = req.display or req.name
        domains = req.domains

    if any(s["name"] == name for s in state.dpi_services):
        raise HTTPException(409, f"Сервис '{name}' уже добавлен")

    state.dpi_services.append({
        "name": name, "display": display,
        "domains": domains, "enabled": True,
    })
    state.save()
    if state.dpi_enabled:
        asyncio.create_task(_regen_dpi_dnsmasq())
    return {"status": "added", "name": name, "domains": domains}


@app.post("/dpi/service/remove")
@limiter.limit("10/second")
async def post_dpi_service_remove(request: Request, req: DpiServiceRequest,
                                  _: bool = Depends(_auth)):
    before = len(state.dpi_services)
    state.dpi_services = [s for s in state.dpi_services if s["name"] != req.name]
    if len(state.dpi_services) == before:
        raise HTTPException(404, f"Сервис '{req.name}' не найден")
    state.save()
    if state.dpi_enabled:
        asyncio.create_task(_regen_dpi_dnsmasq())
    return {"status": "removed", "name": req.name}


@app.post("/dpi/service/toggle")
@limiter.limit("10/second")
async def post_dpi_service_toggle(request: Request, req: DpiToggleRequest,
                                  _: bool = Depends(_auth)):
    for svc in state.dpi_services:
        if svc["name"] == req.name:
            svc["enabled"] = req.enabled
            state.save()
            if state.dpi_enabled:
                asyncio.create_task(_regen_dpi_dnsmasq())
            return {"status": "toggled", "name": req.name, "enabled": req.enabled}
    raise HTTPException(404, f"Сервис '{req.name}' не найден")


@app.post("/graph")
@limiter.limit("10/second")
async def post_graph(request: Request, req: GraphRequest, _: bool = Depends(_auth)):
    """Получить PNG-график из Grafana Render API."""
    # (dashboard_uid, panel_id) для каждого типа графика
    panel_map = {
        "tunnel":  ("vpn-tunnel",  1),   # RTT туннеля vs Baseline
        "speed":   ("vpn-tunnel",  2),   # Скорость туннеля (speedtest)
        "clients": ("vpn-clients", 10),  # Количество пиров (история)
        "system":  ("vpn-system",  10),  # CPU история
    }
    period_map = {"1h": "1h", "6h": "6h", "24h": "24h", "7d": "7d"}

    dash_uid, panel_id = panel_map.get(req.panel, ("vpn-tunnel", 1))
    period = period_map.get(req.period, "1h")

    url = (
        f"{GRAFANA_URL}/render/d-solo/{dash_uid}/{dash_uid}"
        f"?panelId={panel_id}&width=800&height=400&from=now-{period}&to=now"
    )
    headers = {}
    if GRAFANA_TOKEN:
        headers["Authorization"] = f"Bearer {GRAFANA_TOKEN}"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    png = await r.read()
                    return Response(content=png, media_type="image/png")
                raise HTTPException(status_code=502, detail=f"Grafana вернул {r.status}")
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana недоступна: {exc}")


@app.post("/diagnose/{device}")
@limiter.limit("10/second")
async def post_diagnose(request: Request, device: str, _: bool = Depends(_auth)):
    results: dict[str, Any] = {"device": device, "ts": datetime.now().isoformat()}

    # WG peer (проверяем оба стека)
    wg_dump = ""
    for tool in ("awg", "wg"):
        rc, out, _ = await run_cmd([tool, "show", "all", "dump"], timeout=10)
        if rc == 0:
            wg_dump += out
    results["wg_peer_found"] = device in wg_dump

    # DNS
    rc, out, _ = await run_cmd(["dig", "@127.0.0.1", "youtube.com", "+short", "+time=3"], timeout=10)
    results["dns_ok"] = rc == 0 and bool(out.strip())

    # Туннель
    ok, rtt = await ping_vps()
    results["tunnel_ok"] = ok
    results["tunnel_rtt_ms"] = rtt

    # Заблокированные сайты
    _plugin = plugins.get(state.active_stack)
    _tun = _plugin.meta.get("tun_name", f"tun-{state.active_stack}") if _plugin else f"tun-{state.active_stack}"
    rc, out, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10", "--interface", _tun, "-o", "/dev/null", "-w", "%{http_code}", "https://youtube.com"],
        timeout=15,
    )
    results["blocked_sites_ok"] = rc == 0 and out.strip() in ("200", "301", "302")

    return results


@app.post("/vps/add")
@limiter.limit("10/second")
async def post_vps_add(request: Request, req: VpsRequest, _: bool = Depends(_auth)):
    for v in state.vps_list:
        if v["ip"] == req.ip:
            raise HTTPException(status_code=409, detail="VPS уже добавлен")
    state.vps_list.append({
        "ip": req.ip, "ssh_port": req.ssh_port,
        "tunnel_ip": req.tunnel_ip or f"10.177.2.{len(state.vps_list) * 4 + 2}",
        "active": True,
    })
    state.save()
    return {"status": "added", "ip": req.ip}


@app.post("/vps/remove")
@limiter.limit("10/second")
async def post_vps_remove(request: Request, req: VpsRequest, _: bool = Depends(_auth)):
    before = len(state.vps_list)
    state.vps_list = [v for v in state.vps_list if v["ip"] != req.ip]
    if len(state.vps_list) == before:
        raise HTTPException(status_code=404, detail="VPS не найден")
    state.active_vps_idx = 0
    state.save()
    return {"status": "removed", "ip": req.ip}


# ---------------------------------------------------------------------------
# Startup / Shutdown / Signals
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup() -> None:
    logger.info("=" * 60)
    logger.info("Watchdog v4.0 запускается...")

    # Загружаем плагины
    plugins.load()

    # Загружаем состояние
    state.load()

    # Поднимаем активный стек при старте
    active_plugin = plugins.get(state.active_stack)
    if active_plugin and active_plugin.meta.get("direct_mode"):
        # direct_mode (zapret): не создаёт tun, запускаем через свой метод
        logger.info(f"Поднимаем direct_mode стек {state.active_stack} при старте...")
        await active_plugin.start()
        active_plugin = None  # не устанавливать маршрут table marked через tun
    elif active_plugin:
        tun_name = active_plugin.meta.get("tun_name", f"tun-{state.active_stack}")
        rc, _, _ = await run_cmd(["ip", "link", "show", tun_name], timeout=5)
        if rc != 0:
            logger.info(f"Поднимаем стек {state.active_stack} при старте...")
            ok = await active_plugin.start()
            if not ok:
                logger.warning(f"Не удалось поднять стек {state.active_stack} при старте")
                active_plugin = None
        else:
            logger.info(f"Стек {state.active_stack} уже запущен (tun={tun_name})")
        # Всегда обновляем маршрут table marked → tun.
        # vpn-routes.service ставит table marked=unreachable при загрузке
        # (tun ещё нет), поэтому необходимо восстановить маршрут независимо
        # от того, поднимали ли мы tun сами или он уже существовал.
        if active_plugin:
            await run_cmd(
                ["ip", "route", "replace", "default", "dev", tun_name, "table", "marked"],
                timeout=5,
            )
            logger.info(f"Маршрут table marked → {tun_name} установлен")

    # Всегда запускать zapret (DPI bypass, независимо от активного VPN-стека)
    # Activate только если dpi_enabled — иначе просто крутится в режиме standby
    _zapret_already_started = (
        state.active_stack == "zapret" and active_plugin is None
    )
    if not _zapret_already_started:
        zp = plugins.get("zapret")
        if zp:
            logger.info("Запуск zapret (DPI bypass, standby)...")
            await zp.start()
    if state.dpi_enabled and state.dpi_services:
        zp = plugins.get("zapret")
        if zp:
            await zp.activate()
        await _dpi_apply_routing()
        await _regen_dpi_dnsmasq()
        logger.info("[DPI] DPI bypass восстановлен при старте")

    # Consistency recovery
    ok, _ = await ping_vps()
    if not ok:
        logger.warning("VPS недоступен при старте — degraded mode")
        state.degraded_mode = True
    else:
        state.degraded_mode = False

    # Запускаем фоновые задачи
    asyncio.create_task(tg.run(),              name="tg-queue")
    asyncio.create_task(_watchdog_ping_loop(), name="watchdog-ping")
    asyncio.create_task(decision_loop(),       name="decision-loop")
    asyncio.create_task(monitoring_loop(),     name="monitoring-loop")

    _notify_systemd(b"READY=1")
    logger.info(f"Watchdog готов. Стек: {state.active_stack}, degraded={state.degraded_mode}")
    alert(f"✅ *Watchdog v4.0 запущен*\nСтек: {state.active_stack}\nVPS: {VPS_IP or 'не задан'}")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("Watchdog завершается...")
    tg.stop()
    state.save()
    alert("⚠️ *Watchdog завершается* (сервер выключается или перезапуск)")
    _notify_systemd(b"STOPPING=1")


def _handle_sighup(signum: int, frame: Any) -> None:
    """SIGHUP — hot reload плагинов."""
    logger.info("SIGHUP получен, перезагрузка плагинов...")
    plugins.reload()


def _handle_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM — корректное завершение."""
    logger.info("SIGTERM получен")
    state.save()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP,  _handle_sighup)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="warning",   # Своё логирование выше
        access_log=False,
    )

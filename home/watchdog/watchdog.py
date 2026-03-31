#!/usr/bin/env python3
"""
watchdog.py — Центральный агент управления VPN Infrastructure v4.0

Отвечает за:
  - Единый decision loop: адаптивный failover + ротация (взаимоисключающие)
  - Plugin-архитектуру стеков (hysteria2 / reality-xhttp / cloudflare-cdn)
  - HTTP API для telegram-bot (FastAPI, rate limiting, bearer token)
  - Комплексный мониторинг: ping, speedtest, WG peers, контейнеры, диск, DNS,
    mTLS сертификаты, DKMS, upload utilization, heartbeat на VPS
  - Надёжную доставку алертов (TelegramQueue, graceful degradation)
  - Hot reload плагинов по SIGHUP
  - Systemd watchdog ping (sd_notify WATCHDOG=1)
  - Conntrack-статистику для самообучения AllowedIPs
"""


import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from secrets import compare_digest
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
BOT_NOTIFY_URL       = os.getenv("BOT_NOTIFY_URL", "http://172.20.0.11:8090/notify")
VPS_IP               = os.getenv("VPS_IP", "")
VPS_TUNNEL_IP        = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")
GRAFANA_URL          = os.getenv("GRAFANA_URL", "http://172.20.0.32:3000")
GRAFANA_TOKEN        = os.getenv("GRAFANA_TOKEN", "")
GRAFANA_PASSWORD     = os.getenv("GRAFANA_PASSWORD", "")
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
def _cloudflare_cdn_enabled() -> bool:
    return os.getenv("USE_CLOUDFLARE", "n").lower() == "y" and bool(os.getenv("CF_CDN_HOSTNAME", "").strip())


STACK_ORDER = ["hysteria2", "vless-reality-vision", "reality-xhttp"]
if _cloudflare_cdn_enabled():
    STACK_ORDER.insert(0, "cloudflare-cdn")

DEFAULT_STACK = STACK_ORDER[0]

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
TIER2_PROXY_PORT          = 1089  # Stable SOCKS5 port для tier-2 SSH туннеля
HEALTH_SCORE_THRESHOLD    = int(os.getenv("HEALTH_SCORE_THRESHOLD", "70"))
BACKUP_MAX_AGE_DAYS       = 3     # deep check: бэкап не старше N дней

# ---------------------------------------------------------------------------
# DPI bypass (zapret lane)
# ---------------------------------------------------------------------------
DPI_FWMARK       = "0x2"
DPI_TABLE        = 201
DPI_DNSMASQ_CONF = Path("/etc/dnsmasq.d/aaa-dpi.conf")  # aaa < vpn = загружается первым, nftset dpi_direct выигрывает
DPI_VPS_DNS      = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")

DPI_SERVICE_PRESETS: dict[str, dict] = {
    "youtube": {
        "display": "YouTube",
        "domains": [
            "youtube.com", "googlevideo.com", "ytimg.com",
            "yt3.ggpht.com", "youtu.be",
        ],
    },
    "instagram": {
        "display": "Instagram",
        "domains": [
            "instagram.com", "cdninstagram.com", "fbcdn.net",
        ],
    },
    "twitter": {
        "display": "Twitter/X",
        "domains": [
            "twitter.com", "x.com", "t.co", "twimg.com",
        ],
    },
    "spotify": {
        "display": "Spotify",
        "domains": [
            "spotify.com", "scdn.co", "spotilocal.com",
            "audio-ak.spotify.com", "audio4.spotify.com",
        ],
    },
    "steam": {
        "display": "Steam",
        "domains": [
            "store.steampowered.com", "steamcommunity.com",
            "steampowered.com", "steamstatic.com",
            "steamusercontent.com", "steam-chat.com",
        ],
    },
    "tiktok": {
        "display": "TikTok",
        "domains": [
            "tiktok.com", "tiktokcdn.com", "tiktokv.com",
            "musical.ly", "byteoversea.com",
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

SYSTEMD_NOTIFY_ENV_KEYS = ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")


def _child_env() -> dict[str, str]:
    """Среда без sd_notify-переменных для дочерних процессов."""
    env = os.environ.copy()
    for key in SYSTEMD_NOTIFY_ENV_KEYS:
        env.pop(key, None)
    return env


def installed_version_label() -> str:
    try:
        version = Path("/opt/vpn/version").read_text(encoding="utf-8").strip()
        if version and all(ch.isdigit() or ch == "." for ch in version):
            return f"v{version}"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
async def run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Асинхронный запуск команды."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_child_env(),
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

    @staticmethod
    def _md_to_html(text: str) -> str:
        """Конвертировать Markdown-разметку алертов в HTML для Telegram.
        Поддерживает: *bold*, `code`, ```preformatted```.
        """
        import re
        # ```...``` → <pre>
        text = re.sub(r"```(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", text, flags=re.DOTALL)
        # `code` → <code>
        text = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
        # *bold* → <b> (не затрагивать уже обработанные теги)
        text = re.sub(r"\*([^*\n]+)\*", lambda m: f"<b>{m.group(1)}</b>", text)
        return text

    async def _deliver(self, text: str, chat_id: str) -> None:
        # Preferred path: relay through the bot container, which already has
        # working Telegram connectivity even when the host cannot reach Telegram.
        if BOT_NOTIFY_URL and API_TOKEN:
            for attempt in range(5):
                try:
                    async with aiohttp.ClientSession() as session:
                        resp = await session.post(
                            BOT_NOTIFY_URL,
                            json={"message": text, "target": str(chat_id)},
                            headers={"Authorization": f"Bearer {API_TOKEN}"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                        if resp.status == 200:
                            return
                except Exception as exc:
                    logger.debug(f"Bot notify relay недоступен (попытка {attempt + 1}): {exc}")
                await asyncio.sleep(min(30, 5 * (attempt + 1)))

        html_text = self._md_to_html(text)
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status == 200:
                        return
                    if resp.status == 400:
                        # HTML parse error — отправить plain text
                        resp2 = await session.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                        if resp2.status == 200:
                            return
                        return
            except Exception as exc:
                logger.debug(f"Telegram недоступен (попытка {attempt + 1}): {exc}")
            await asyncio.sleep(min(30, 5 * (attempt + 1)))
        logger.warning("Не удалось доставить alert chat_id=%s ни через bot relay, ни напрямую в Telegram", chat_id)

    def stop(self) -> None:
        self._running = False


tg = TelegramQueue()

_admin_chat_ids_cache: list[str] = []
_admin_chat_ids_ts: float = 0.0
_ADMIN_CACHE_TTL = 300.0  # 5 минут


def _get_admin_chat_ids() -> list[str]:
    global _admin_chat_ids_cache, _admin_chat_ids_ts
    import time as _time
    now = _time.monotonic()
    if now - _admin_chat_ids_ts < _ADMIN_CACHE_TTL and _admin_chat_ids_cache:
        return _admin_chat_ids_cache
    ids: list[str] = []
    if TELEGRAM_ADMIN_ID:
        ids.append(str(TELEGRAM_ADMIN_ID))
    if BOT_DB_PATH.exists():
        try:
            import sqlite3 as _sqlite3
            with _sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=3) as conn:
                rows = conn.execute(
                    "SELECT chat_id FROM clients WHERE is_admin = 1"
                ).fetchall()
            for row in rows:
                cid = str(row[0])
                if cid not in ids:
                    ids.append(cid)
        except Exception as exc:
            logger.warning(f"_get_admin_chat_ids: не удалось прочитать БД бота: {exc}")
    _admin_chat_ids_cache = ids if ids else ([str(TELEGRAM_ADMIN_ID)] if TELEGRAM_ADMIN_ID else [])
    _admin_chat_ids_ts = now
    return _admin_chat_ids_cache


def _reload_admin_cache() -> int:
    global _admin_chat_ids_ts
    _admin_chat_ids_ts = 0.0
    return len(_get_admin_chat_ids())


def alert(text: str, chat_id: str = "") -> None:
    """Добавить алерт в очередь Telegram."""
    logger.info(f"ALERT: {text[:120]}")
    ts = datetime.now().strftime("%d.%m %H:%M")
    msg = f"{text}\n\n🕐 {ts}"
    if chat_id:
        tg.enqueue(msg, chat_id)
    else:
        for cid in _get_admin_chat_ids():
            tg.enqueue(msg, cid)


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
        self.experimental: bool = bool(self.meta.get("experimental", False))
        self.auto_enabled: bool = bool(self.meta.get("auto_enabled", not self.experimental))

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

    async def activate(self) -> bool:
        rc, _, err = await self._run("activate", timeout=15)
        if rc != 0:
            logger.error(f"[{self.name}] activate: {err.strip()}")
        return rc == 0

    async def deactivate(self) -> bool:
        rc, _, err = await self._run("deactivate", timeout=15)
        if rc != 0:
            logger.warning(f"[{self.name}] deactivate: {err.strip()}")
        return rc == 0

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

    def auto_names(self) -> list[str]:
        """Стеки для автоматических reassessment/failover/standby-проверок."""
        return [
            p.name for p in sorted(
                self._plugins.values(), key=lambda p: p.resilience, reverse=True
            )
            if not p.meta.get("direct_mode") and p.auto_enabled
        ]

    def names_list(self) -> list[dict]:
        return [
            {"name": p.name, "display": p.meta.get("display_name", p.name),
             "resilience": p.resilience,
             "experimental": p.experimental,
             "auto_enabled": p.auto_enabled}
            for p in sorted(self._plugins.values(), key=lambda p: p.resilience, reverse=True)
        ]


async def _test_stack_runtime(plugin: Plugin, name: str, timeout: int = 10) -> tuple[bool, float]:
    """Проверяет стек, поднимая standby-плагины временно для честного smoke-test."""
    transient_start = name != state.active_stack
    if transient_start and not await plugin.start():
        return False, 0.0
    try:
        return await plugin.test(timeout=timeout)
    finally:
        if transient_start:
            await plugin.stop()


plugins = PluginManager()


# ---------------------------------------------------------------------------
# Watchdog State
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Health Check — data model
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name:   str
    status: str          # "ok" | "warn" | "fail"
    detail: str  = ""
    weight: int  = 3     # 1=low, 3=medium, 5=high, 10=critical
    tier:   str  = "quick"


class WatchdogState:
    def __init__(self) -> None:
        self.active_stack: str = DEFAULT_STACK
        self.primary_stack: str = DEFAULT_STACK
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
        self.last_download_mbps: float = 0.0   # последний speedtest download (Mbps)
        self.last_upload_mbps: float = 0.0     # последний speedtest upload (Mbps)
        self.upload_util_pct: float = 0.0      # утилизация upload-канала %
        self.blocked_sites_reachable: int = 1  # 1=OK, 0=недоступны
        self.failover_count: int = 0           # счётчик failover-переключений
        self.rotation_log: list[dict] = []     # история переключений стека (max 20)
        self.dnsmasq_up: int = 1               # 1=работает, 0=нет
        self.docker_health: dict[str, int] = {}  # {container: 1/0}
        self.cached_peers: list[dict] = []      # последний дамп WG пиров
        # ── DPI bypass (zapret lane) ─────────────────────────────────────────
        self.dpi_enabled: bool = False          # глобальный on/off
        self.dpi_services: list[dict] = []      # [{name, display, domains, enabled}]
        # ── Health Check ─────────────────────────────────────────────────────
        self.last_monitoring_tick: float = 0.0  # timestamp последнего тика мониторинга
        self.wg0_up: bool = False               # wg0 интерфейс существует
        self.wg1_up: bool = False               # wg1 интерфейс существует
        self.cert_days: dict[str, int] = {}     # {label: days_remaining}
        self.nftset_counts: dict[str, int] = {} # {set_name: element_count}
        self.nftables_ok: bool = False          # True если check_nftables_integrity прошла
        self.nftables_checked: bool = False     # True после первой проверки
        self.nfqws_ok: Optional[bool] = None    # None=не проверялось, True/False по факту
        self.last_heartbeat_ts: float = 0.0     # timestamp последней успешной проверки доступности VPS
        self.last_wg_check_ts: float = 0.0      # timestamp последней проверки wg0/wg1
        self.stacks_ok_count: int = 0           # количество рабочих стеков
        self.stacks_checked: bool = False       # True после первой проверки стеков
        self.health_score: float = 0.0          # последний расчитанный score
        self.health_report: dict = {}           # полный last report
        self.post_deploy_until: float = 0.0     # timestamp конца post-deploy watch
        self.bot_runtime_drift: bool = False
        self.bot_runtime_drift_detail: str = ""
        self.bot_runtime_drift_since: float = 0.0
        self.bot_selfheal_last_ts: float = 0.0
        self.dnsmasq_config_hash: str = ""
        self.compose_runtime_drift: bool = False
        self.compose_runtime_drift_detail: str = ""
        self.compose_runtime_drift_since: float = 0.0
        self.compose_selfheal_last_ts: float = 0.0
        self.watchdog_runtime_drift: bool = False
        self.watchdog_runtime_drift_detail: str = ""
        self.watchdog_runtime_drift_since: float = 0.0
        self.watchdog_selfheal_last_ts: float = 0.0
        self.peer_reconcile_last_ts: float = 0.0

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
            "rotation_log": self.rotation_log[-20:],
            "bot_runtime_drift": self.bot_runtime_drift,
            "bot_runtime_drift_detail": self.bot_runtime_drift_detail,
            "bot_runtime_drift_since": self.bot_runtime_drift_since,
            "bot_selfheal_last_ts": self.bot_selfheal_last_ts,
            "dnsmasq_config_hash": self.dnsmasq_config_hash,
            "compose_runtime_drift": self.compose_runtime_drift,
            "compose_runtime_drift_detail": self.compose_runtime_drift_detail,
            "compose_runtime_drift_since": self.compose_runtime_drift_since,
            "compose_selfheal_last_ts": self.compose_selfheal_last_ts,
            "watchdog_runtime_drift": self.watchdog_runtime_drift,
            "watchdog_runtime_drift_detail": self.watchdog_runtime_drift_detail,
            "watchdog_runtime_drift_since": self.watchdog_runtime_drift_since,
            "watchdog_selfheal_last_ts": self.watchdog_selfheal_last_ts,
            "peer_reconcile_last_ts": self.peer_reconcile_last_ts,
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
                self.active_stack   = data.get("active_stack", DEFAULT_STACK)
                self.primary_stack  = data.get("primary_stack", DEFAULT_STACK)
                self.external_ip    = data.get("external_ip", "")
                self.vps_list       = data.get("vps_list", [])
                self.active_vps_idx = data.get("active_vps_idx", 0)
                self.degraded_mode  = data.get("degraded_mode", False)
                self.is_first_run   = data.get("is_first_run", True)
                self.dpi_enabled    = data.get("dpi_enabled", False)
                self.dpi_services   = data.get("dpi_services", [])
                self.rotation_log   = data.get("rotation_log", [])
                self.bot_runtime_drift = data.get("bot_runtime_drift", False)
                self.bot_runtime_drift_detail = data.get("bot_runtime_drift_detail", "")
                self.bot_runtime_drift_since = float(data.get("bot_runtime_drift_since", 0.0) or 0.0)
                self.bot_selfheal_last_ts = float(data.get("bot_selfheal_last_ts", 0.0) or 0.0)
                self.dnsmasq_config_hash = data.get("dnsmasq_config_hash", "")
                self.compose_runtime_drift = data.get("compose_runtime_drift", False)
                self.compose_runtime_drift_detail = data.get("compose_runtime_drift_detail", "")
                self.compose_runtime_drift_since = float(data.get("compose_runtime_drift_since", 0.0) or 0.0)
                self.compose_selfheal_last_ts = float(data.get("compose_selfheal_last_ts", 0.0) or 0.0)
                self.watchdog_runtime_drift = data.get("watchdog_runtime_drift", False)
                self.watchdog_runtime_drift_detail = data.get("watchdog_runtime_drift_detail", "")
                self.watchdog_runtime_drift_since = float(data.get("watchdog_runtime_drift_since", 0.0) or 0.0)
                self.watchdog_selfheal_last_ts = float(data.get("watchdog_selfheal_last_ts", 0.0) or 0.0)
                self.peer_reconcile_last_ts = float(data.get("peer_reconcile_last_ts", 0.0) or 0.0)
                if self.active_stack not in STACK_ORDER:
                    self.active_stack = DEFAULT_STACK
                    self.is_first_run = True
                if self.primary_stack not in STACK_ORDER:
                    self.primary_stack = DEFAULT_STACK
                    self.is_first_run = True
                if self.active_stack == "cloudflare-cdn" and not _cloudflare_cdn_enabled():
                    self.active_stack = DEFAULT_STACK
                    self.primary_stack = DEFAULT_STACK
                    self.degraded_mode = False
                    self.is_first_run = True
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
            except Exception as exc:
                logger.debug("Не удалось определить socks_port из конфига: %s", exc)
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


async def _measure_throughput(url: str, proxy: str = "", interface: str = "") -> float:
    """Замер throughput (Mbps) через URL, опционально через прокси или интерфейс."""
    cmd = ["curl", "-s", "--max-time", "30", "-o", "/dev/null", "-w", "%{speed_download}"]
    if proxy:
        cmd += ["--proxy", proxy]
    if interface:
        cmd += ["--interface", interface]
    cmd.append(url)
    rc, out, _ = await run_cmd(cmd, timeout=35)
    if rc == 0:
        try:
            bytes_per_sec = float(out.strip())
            return round(bytes_per_sec * 8 / 1_000_000, 2)   # Mbps
        except Exception as exc:
            logger.debug(f"_measure_throughput: не удалось распарсить вывод curl '{out.strip()}': {exc}")
    return 0.0


async def _get_active_tun() -> str:
    """Вернуть имя активного tun-интерфейса, или '' для direct_mode (zapret) и при ошибке."""
    try:
        plugin = plugins.get(state.active_stack)
        if not plugin or plugin.meta.get("direct_mode"):
            return ""   # zapret: нет tun, трафик идёт напрямую
        tun = plugin.meta.get("tun_name", f"tun-{state.active_stack}")
        rc, _, _ = await run_cmd(["ip", "link", "show", tun], timeout=3)
        return tun if rc == 0 else ""
    except Exception:
        return ""


async def speedtest_small() -> float:
    """100KB тест через активный стек. Возвращает Mbps."""
    tun = await _get_active_tun()
    mbps = await _measure_throughput(SPEED_URL_SMALL, interface=tun)
    if mbps > 0:
        state.small_speedtest.append(mbps)
        state.last_download_mbps = mbps
    return mbps


async def speedtest_large() -> float:
    """10MB тест через активный стек. Возвращает Mbps."""
    tun = await _get_active_tun()
    mbps = await _measure_throughput(SPEED_URL_LARGE, interface=tun)
    if mbps > 0:
        state.large_speedtest.append(mbps)
    return mbps


async def speedtest_upload() -> float:
    """100KB upload-тест через активный стек. Возвращает Mbps."""
    tun = await _get_active_tun()
    tmp = "/tmp/wdg_upload_100k.bin"
    try:
        # Генерируем 100KB случайных данных
        rc, _, _ = await run_cmd(
            ["dd", "if=/dev/urandom", f"of={tmp}", "bs=1024", "count=100"],
            timeout=5,
        )
        if rc != 0:
            return 0.0
        cmd = ["curl", "-s", "--max-time", "30",
               "-X", "POST", "--data-binary", f"@{tmp}",
               "-o", "/dev/null", "-w", "%{speed_upload}",
               "https://speed.cloudflare.com/__up"]
        if tun:
            cmd = ["curl", "-s", "--max-time", "30", "--interface", tun,
                   "-X", "POST", "--data-binary", f"@{tmp}",
                   "-o", "/dev/null", "-w", "%{speed_upload}",
                   "https://speed.cloudflare.com/__up"]
        rc, out, _ = await run_cmd(cmd, timeout=35)
        if rc == 0:
            try:
                mbps = round(float(out.strip()) * 8 / 1_000_000, 2)
                if mbps > 0:
                    state.last_upload_mbps = mbps
                return mbps
            except Exception:
                pass
        return 0.0
    finally:
        Path(tmp).unlink(missing_ok=True)


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
BOT_SOURCE_DIR = Path("/opt/vpn/telegram-bot")
BOT_RUNTIME_FILES = (
    "bot.py",
    "config.py",
    "database.py",
    "handlers/admin.py",
    "handlers/client.py",
    "handlers/alerts.py",
    "handlers/keyboards.py",
    "services/config_builder.py",
    "services/watchdog_client.py",
)
BOT_DRIFT_CONFIRM_SECONDS = int(os.getenv("BOT_DRIFT_CONFIRM_SECONDS", "300"))
BOT_SELFHEAL_COOLDOWN_SECONDS = int(os.getenv("BOT_SELFHEAL_COOLDOWN_SECONDS", "1800"))
COMPOSE_SOURCE_FILE = Path("/opt/vpn/home/docker-compose.yml")
COMPOSE_RUNTIME_FILE = Path("/opt/vpn/docker-compose.yml")
COMPOSE_DRIFT_CONFIRM_SECONDS = int(os.getenv("COMPOSE_DRIFT_CONFIRM_SECONDS", "300"))
COMPOSE_SELFHEAL_COOLDOWN_SECONDS = int(os.getenv("COMPOSE_SELFHEAL_COOLDOWN_SECONDS", "1800"))
WATCHDOG_SOURCE_FILE = Path("/opt/vpn/home/watchdog/watchdog.py")
WATCHDOG_RUNTIME_FILE = Path("/opt/vpn/watchdog/watchdog.py")
WATCHDOG_DRIFT_CONFIRM_SECONDS = int(os.getenv("WATCHDOG_DRIFT_CONFIRM_SECONDS", "300"))
WATCHDOG_SELFHEAL_COOLDOWN_SECONDS = int(os.getenv("WATCHDOG_SELFHEAL_COOLDOWN_SECONDS", "1800"))


def _sha256_file(path: Path) -> str:
    import hashlib as _hashlib
    if not path.exists():
        return ""
    return _hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_paths(paths: list[Path]) -> str:
    import hashlib as _hashlib
    digest = _hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p)):
        digest.update(str(path).encode())
        if not path.exists():
            digest.update(b"missing")
            continue
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _dnsmasq_config_paths() -> list[Path]:
    paths = [Path("/etc/dnsmasq.conf")]
    conf_dir = Path("/etc/dnsmasq.d")
    if conf_dir.exists():
        paths.extend(sorted(conf_dir.glob("*.conf")))
    return [p for p in paths if p.exists()]


def _read_expected_device_peers() -> list[dict[str, str]]:
    if not BOT_DB_PATH.exists():
        return []
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT device_name, protocol, public_key, ip_address
                FROM devices
                WHERE public_key IS NOT NULL AND public_key != ''
                  AND ip_address IS NOT NULL AND ip_address != ''
                """
            ).fetchall()
        return [
            {
                "device_name": row[0] or "",
                "protocol": (row[1] or "").lower(),
                "public_key": row[2] or "",
                "ip_address": row[3] or "",
            }
            for row in rows
            if row[2] and row[3]
        ]
    except Exception as exc:
        logger.warning("Не удалось прочитать expected peers из БД бота: %s", exc)
        return []


async def _runtime_peer_dump() -> list[dict[str, Any]]:
    combined_out = ""
    for tool in ("awg", "wg"):
        rc, out, _ = await run_cmd([tool, "show", "all", "dump"], timeout=10)
        if rc == 0:
            combined_out += out
    peers: list[dict[str, Any]] = []
    for line in combined_out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 9:
            continue
        iface, pubkey, _psk, endpoint, allowed_ips, handshake_ts, rx, tx, _ka = parts
        try:
            hs_int = int(handshake_ts)
        except ValueError:
            continue
        peers.append({
            "interface": iface,
            "public_key": pubkey,
            "endpoint": endpoint if endpoint != "(none)" else None,
            "allowed_ips": allowed_ips,
            "last_handshake": hs_int,
            "rx_bytes": int(rx) if rx.isdigit() else 0,
            "tx_bytes": int(tx) if tx.isdigit() else 0,
        })
    return peers


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


def _telegram_bot_host_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    import hashlib as _hashlib
    for rel_path in BOT_RUNTIME_FILES:
        src = BOT_SOURCE_DIR / rel_path
        if not src.exists():
            hashes[rel_path] = "missing"
            continue
        hashes[rel_path] = _hashlib.sha256(src.read_bytes()).hexdigest()
    return hashes


async def _telegram_bot_container_hashes() -> tuple[dict[str, str], str]:
    quoted = " ".join(f'"{rel_path}"' for rel_path in BOT_RUNTIME_FILES)
    rc, out, err = await run_cmd(
        ["docker", "exec", "telegram-bot", "sh", "-lc", f"cd /app && sha256sum {quoted}"],
        timeout=20,
    )
    if rc != 0:
        return {}, err.strip() or out.strip() or f"docker exec rc={rc}"
    hashes: dict[str, str] = {}
    for line in out.strip().splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        hashes[parts[1].lstrip("*")] = parts[0]
    return hashes, ""


async def selfheal_telegram_bot_runtime() -> bool:
    logger.warning("Обнаружен drift telegram-bot runtime → source, запускаю self-heal rebuild")
    alert("⚠️ *telegram-bot runtime drift* — запускаю self-heal rebuild")
    rc, out, err = await run_cmd(
        ["bash", "-lc", "cd /opt/vpn && docker compose up -d --build telegram-bot"],
        timeout=900,
    )
    state.bot_selfheal_last_ts = time.time()
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()[:300]
        state.bot_runtime_drift_detail = f"self-heal failed: {detail}"
        logger.error("telegram-bot self-heal failed: %s", detail)
        alert(f"🚨 *telegram-bot self-heal failed*: `{detail}`")
        return False
    await check_containers()
    container_hashes, detail = await _telegram_bot_container_hashes()
    host_hashes = _telegram_bot_host_hashes()
    if container_hashes and container_hashes == host_hashes:
        state.bot_runtime_drift = False
        state.bot_runtime_drift_since = 0.0
        state.bot_runtime_drift_detail = ""
        logger.info("telegram-bot self-heal completed successfully")
        alert("✅ *telegram-bot self-heal completed* — runtime снова синхронизирован")
        return True
    mismatch = detail or "hash mismatch after rebuild"
    state.bot_runtime_drift = True
    state.bot_runtime_drift_detail = mismatch[:300]
    logger.error("telegram-bot self-heal incomplete: %s", state.bot_runtime_drift_detail)
    alert(f"🚨 *telegram-bot self-heal incomplete*: `{state.bot_runtime_drift_detail}`")
    return False


async def check_telegram_bot_runtime_sync() -> None:
    """Проверить, что live container собран из актуального bot source, и при drift выполнить self-heal."""
    if state.docker_health.get("telegram-bot") != 1:
        state.bot_runtime_drift = False
        state.bot_runtime_drift_since = 0.0
        state.bot_runtime_drift_detail = "telegram-bot container not healthy"
        return

    host_hashes = _telegram_bot_host_hashes()
    container_hashes, detail = await _telegram_bot_container_hashes()
    now = time.time()
    if container_hashes and container_hashes == host_hashes:
        if state.bot_runtime_drift:
            logger.info("telegram-bot runtime снова синхронизирован с /opt/vpn/telegram-bot")
        state.bot_runtime_drift = False
        state.bot_runtime_drift_since = 0.0
        state.bot_runtime_drift_detail = ""
        return

    if not state.bot_runtime_drift:
        state.bot_runtime_drift_since = now
    state.bot_runtime_drift = True
    if container_hashes:
        mismatched = sorted(rel for rel, digest in host_hashes.items() if container_hashes.get(rel) != digest)
        state.bot_runtime_drift_detail = ", ".join(mismatched[:4]) or "hash mismatch"
    else:
        state.bot_runtime_drift_detail = detail[:300] if detail else "container hashes unavailable"

    if now - state.bot_runtime_drift_since < BOT_DRIFT_CONFIRM_SECONDS:
        return
    if now - state.bot_selfheal_last_ts < BOT_SELFHEAL_COOLDOWN_SECONDS:
        return
    await selfheal_telegram_bot_runtime()


async def check_dnsmasq_config_sync() -> None:
    paths = _dnsmasq_config_paths()
    if not paths:
        return
    current_hash = _sha256_paths(paths)
    if not state.dnsmasq_config_hash:
        state.dnsmasq_config_hash = current_hash
        return
    if current_hash == state.dnsmasq_config_hash:
        return

    logger.warning("Обнаружен drift dnsmasq config → runtime, запускаю reload")
    alert("⚠️ *dnsmasq config drift* — запускаю reload")
    rc, out, err = await run_cmd(["dnsmasq", "--test"], timeout=15)
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()[:300]
        logger.error("dnsmasq self-heal aborted, invalid config: %s", detail)
        alert(f"🚨 *dnsmasq self-heal failed*: `{detail}`")
        return

    rc, out, err = await run_cmd(["systemctl", "reload", "dnsmasq"], timeout=20)
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()[:300]
        logger.warning("dnsmasq reload failed, fallback to restart: %s", detail)
        alert(f"⚠️ *dnsmasq reload failed* — fallback to restart: `{detail}`")
        rc, out, err = await run_cmd(["systemctl", "restart", "dnsmasq"], timeout=30)
        if rc != 0:
            detail = (err or out or f"rc={rc}").strip()[:300]
            logger.error("dnsmasq self-heal failed after restart: %s", detail)
            alert(f"🚨 *dnsmasq self-heal failed*: `{detail}`")
            return

    rc, out, _ = await run_cmd(["dig", "@127.0.0.1", "google.com", "+short", "+time=3"], timeout=10)
    if rc == 0 and out.strip():
        state.dnsmasq_config_hash = current_hash
        logger.info("dnsmasq config self-heal completed successfully")
        alert("✅ *dnsmasq self-heal completed* — конфиг перезагружен")
        state.dnsmasq_up = 1
        return

    logger.error("dnsmasq self-heal verification failed after reload")
    alert("🚨 *dnsmasq self-heal incomplete* — после reload DNS не отвечает")


async def selfheal_compose_runtime() -> bool:
    logger.warning("Обнаружен drift docker-compose runtime → source, запускаю controlled recreate")
    alert("⚠️ *docker-compose runtime drift* — запускаю controlled recreate")
    state.compose_selfheal_last_ts = time.time()
    if not COMPOSE_SOURCE_FILE.exists():
        detail = f"source missing: {COMPOSE_SOURCE_FILE}"
        state.compose_runtime_drift_detail = detail
        logger.error("compose self-heal failed: %s", detail)
        alert(f"🚨 *docker-compose self-heal failed*: `{detail}`")
        return False

    rc, out, err = await run_cmd(["cp", str(COMPOSE_SOURCE_FILE), str(COMPOSE_RUNTIME_FILE)], timeout=15)
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()[:300]
        state.compose_runtime_drift_detail = detail
        logger.error("compose self-heal copy failed: %s", detail)
        alert(f"🚨 *docker-compose self-heal failed*: `{detail}`")
        return False

    rc, out, err = await run_cmd(
        ["docker", "compose", "-f", str(COMPOSE_RUNTIME_FILE), "up", "-d"],
        timeout=300,
    )
    if rc != 0:
        detail = (err or out or f"rc={rc}").strip()[:300]
        state.compose_runtime_drift_detail = detail
        logger.error("compose self-heal recreate failed: %s", detail)
        alert(f"🚨 *docker-compose self-heal failed*: `{detail}`")
        return False

    if _sha256_file(COMPOSE_SOURCE_FILE) == _sha256_file(COMPOSE_RUNTIME_FILE):
        state.compose_runtime_drift = False
        state.compose_runtime_drift_since = 0.0
        state.compose_runtime_drift_detail = ""
        logger.info("docker-compose self-heal completed successfully")
        alert("✅ *docker-compose self-heal completed* — runtime снова синхронизирован")
        return True

    state.compose_runtime_drift = True
    state.compose_runtime_drift_detail = "hash mismatch after recreate"
    logger.error("docker-compose self-heal incomplete: %s", state.compose_runtime_drift_detail)
    alert(f"🚨 *docker-compose self-heal incomplete*: `{state.compose_runtime_drift_detail}`")
    return False


async def check_compose_runtime_sync() -> None:
    if not COMPOSE_SOURCE_FILE.exists() or not COMPOSE_RUNTIME_FILE.exists():
        state.compose_runtime_drift = False
        state.compose_runtime_drift_since = 0.0
        state.compose_runtime_drift_detail = "compose file missing"
        return

    src_hash = _sha256_file(COMPOSE_SOURCE_FILE)
    runtime_hash = _sha256_file(COMPOSE_RUNTIME_FILE)
    now = time.time()
    if src_hash == runtime_hash:
        if state.compose_runtime_drift:
            logger.info("docker-compose runtime снова синхронизирован с /opt/vpn/home/docker-compose.yml")
        state.compose_runtime_drift = False
        state.compose_runtime_drift_since = 0.0
        state.compose_runtime_drift_detail = ""
        return

    if not state.compose_runtime_drift:
        state.compose_runtime_drift_since = now
    state.compose_runtime_drift = True
    state.compose_runtime_drift_detail = "docker-compose.yml hash mismatch"
    if now - state.compose_runtime_drift_since < COMPOSE_DRIFT_CONFIRM_SECONDS:
        return
    if now - state.compose_selfheal_last_ts < COMPOSE_SELFHEAL_COOLDOWN_SECONDS:
        return
    await selfheal_compose_runtime()


async def schedule_watchdog_runtime_selfheal() -> bool:
    if not WATCHDOG_SOURCE_FILE.exists():
        detail = f"source missing: {WATCHDOG_SOURCE_FILE}"
        state.watchdog_runtime_drift_detail = detail
        logger.error("watchdog self-heal failed: %s", detail)
        alert(f"🚨 *watchdog self-heal failed*: `{detail}`")
        return False

    logger.warning("Обнаружен drift watchdog runtime → source, планирую self-heal restart")
    alert("⚠️ *watchdog runtime drift* — планирую self-heal restart")
    state.watchdog_selfheal_last_ts = time.time()
    state.save()
    try:
        subprocess.Popen(
            [
                "bash",
                "-lc",
                f"sleep 2 && cp {WATCHDOG_SOURCE_FILE} {WATCHDOG_RUNTIME_FILE} && systemctl restart watchdog.service",
            ],
            env=_child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("watchdog self-heal restart scheduled")
        return True
    except Exception as exc:
        detail = str(exc)[:300]
        state.watchdog_runtime_drift_detail = detail
        logger.error("watchdog self-heal schedule failed: %s", detail)
        alert(f"🚨 *watchdog self-heal failed*: `{detail}`")
        return False


async def check_watchdog_runtime_sync() -> None:
    if not WATCHDOG_SOURCE_FILE.exists() or not WATCHDOG_RUNTIME_FILE.exists():
        state.watchdog_runtime_drift = False
        state.watchdog_runtime_drift_since = 0.0
        state.watchdog_runtime_drift_detail = "watchdog file missing"
        return

    src_hash = _sha256_file(WATCHDOG_SOURCE_FILE)
    runtime_hash = _sha256_file(WATCHDOG_RUNTIME_FILE)
    now = time.time()
    if src_hash == runtime_hash:
        if state.watchdog_runtime_drift:
            logger.info("watchdog runtime снова синхронизирован с /opt/vpn/home/watchdog/watchdog.py")
            alert("✅ *watchdog self-heal completed* — runtime снова синхронизирован")
        state.watchdog_runtime_drift = False
        state.watchdog_runtime_drift_since = 0.0
        state.watchdog_runtime_drift_detail = ""
        return

    if not state.watchdog_runtime_drift:
        state.watchdog_runtime_drift_since = now
    state.watchdog_runtime_drift = True
    state.watchdog_runtime_drift_detail = "watchdog.py hash mismatch"
    if now - state.watchdog_runtime_drift_since < WATCHDOG_DRIFT_CONFIRM_SECONDS:
        return
    if now - state.watchdog_selfheal_last_ts < WATCHDOG_SELFHEAL_COOLDOWN_SECONDS:
        return
    await schedule_watchdog_runtime_selfheal()


async def reconcile_wg_runtime_from_db() -> None:
    devices = _read_expected_device_peers()
    if not devices:
        state.peer_reconcile_last_ts = time.time()
        return
    runtime_peers = {p.get("public_key", ""): p for p in await _runtime_peer_dump() if p.get("public_key")}
    fixes = 0
    for device in devices:
        pubkey = device["public_key"]
        expected_ip = device["ip_address"]
        expected_iface = "wg0" if device["protocol"] == "awg" else "wg1"
        peer = runtime_peers.get(pubkey)
        if not peer or peer.get("interface") != expected_iface:
            continue
        current_allowed = {part.strip() for part in str(peer.get("allowed_ips", "")).split(",") if part.strip()}
        expected_allowed = {f"{expected_ip}/32"}
        if current_allowed == expected_allowed:
            continue
        device_label = device["device_name"] or pubkey[:12]
        logger.warning(
            "Обнаружен peer drift для %s [%s]: %s -> %s",
            device_label,
            expected_iface,
            ",".join(sorted(current_allowed)) or "(empty)",
            f"{expected_ip}/32",
        )
        alert(
            f"⚠️ *peer drift* `{device_label}` [{expected_iface}] — "
            f"`{','.join(sorted(current_allowed)) or '(empty)'}` → `{expected_ip}/32`"
        )
        wg = _wg_tool(expected_iface)
        rc, out, err = await run_cmd(
            [wg, "set", expected_iface, "peer", pubkey, "allowed-ips", f"{expected_ip}/32"],
            timeout=15,
        )
        if rc != 0:
            detail = (err or out or f"rc={rc}").strip()[:300]
            logger.error("peer drift self-heal failed for %s: %s", device_label, detail)
            alert(f"🚨 *peer self-heal failed* `{device_label}`: `{detail}`")
            continue
        await run_cmd([_wg_quick_tool(expected_iface), "save", expected_iface], timeout=15)
        logger.info("peer drift self-heal completed for %s [%s]", device_label, expected_iface)
        alert(f"✅ *peer self-heal completed* `{device_label}` [{expected_iface}]")
        fixes += 1
    if fixes:
        logger.info("peer reconcile completed: %s fixes applied", fixes)
    state.peer_reconcile_last_ts = time.time()


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
    CLIENT_IFACES = {"wg0", "wg1"}
    seen_keys: set[str] = set()
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            iface, pubkey, ts_str = parts[0], parts[1], parts[2]
            if iface not in CLIENT_IFACES:
                continue
            peer_key = f"{iface}:{pubkey}"
            seen_keys.add(peer_key)
            try:
                ts = int(ts_str)
                age = now - ts
                # Алерт о stale peer убран — мобильные устройства нормально
                # уходят в сон, это не признак проблемы. Данные видны в Grafana.
                if age <= PEER_STALE_SECONDS:
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
    # Исключаем транзитные интерфейсы (tier-2 туннель до VPS — не клиентские пиры)
    CLIENT_IFACES = {"wg0", "wg1"}
    peers: list[dict] = []
    for line in dump_out.strip().splitlines():
        p = line.split("\t")
        if len(p) != 9:
            continue
        if p[0] not in CLIENT_IFACES:
            continue
        try:
            peers.append({
                "interface":      p[0],
                "public_key":     p[1],
                "last_handshake": int(p[5]),
                "rx_bytes":       int(p[6]) if p[6].isdigit() else 0,
                "tx_bytes":       int(p[7]) if p[7].isdigit() else 0,
            })
        except Exception:
            continue
    state.cached_peers = peers


# Контейнеры фазы 1 — критичны, алерт если exited/unhealthy
CRITICAL_CONTAINERS = frozenset(
    ["telegram-bot", "socket-proxy", "nginx", "xray-client-xhttp", "xray-client-vision", "xray-client-cdn"]
)
# Контейнеры фазы 2 (мониторинг) — опциональны:
# алерт только если контейнер СУЩЕСТВУЕТ но нездоров; отсутствие — норма
OPTIONAL_CONTAINERS = frozenset(
    ["prometheus", "alertmanager", "grafana", "grafana-renderer", "node-exporter"]
)


# ---------------------------------------------------------------------------
# Мониторинг: Docker контейнеры
# ---------------------------------------------------------------------------
async def check_containers() -> None:
    """Проверка exited/unhealthy контейнеров. Обновляет state.docker_health.

    Критичные (фаза 1): алерт при любом нездоровом состоянии.
    Опциональные (фаза 2, мониторинг): алерт только если контейнер есть но нездоров.
    Отсутствие опциональных — норма (мониторинг устанавливается после поднятия VPN).
    """
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
                if name in CRITICAL_CONTAINERS:
                    alert(f"🚨 Контейнер *{name}*: `{status}`")
                elif name in OPTIONAL_CONTAINERS:
                    # Существует но нездоров — алертим
                    alert(f"⚠️ Мониторинг *{name}*: `{status}`")
                # Неизвестные контейнеры — не алертим
    # Опциональные которых нет вообще: weight=0, не влияют на health score
    for name in OPTIONAL_CONTAINERS:
        if name not in new_health:
            new_health[name] = -1  # -1 = not present (skip, не fail)
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
        rc2, out2, err2 = await run_cmd(["systemctl", "restart", "dnsmasq"], timeout=30)
        if rc2 == 0:
            logger.info("dnsmasq restart completed")
            alert("✅ *dnsmasq self-heal completed* — сервис перезапущен")
        else:
            detail = (err2 or out2 or f"rc={rc2}").strip()[:300]
            logger.error("dnsmasq restart failed: %s", detail)
            alert(f"🚨 *dnsmasq self-heal failed*: `{detail}`")
    else:
        state.dnsmasq_up = 1


# ---------------------------------------------------------------------------
# Мониторинг: внешний IP + DDNS
# ---------------------------------------------------------------------------
async def _detect_public_ip(*, direct: bool = False) -> str:
    """Определить внешний IPv4.

    В gateway mode plain curl может выйти через активный VPS-стек и вернуть
    egress IP VPS. Для клиентского ingress и DDNS нужен реальный WAN IP роутера,
    поэтому прямой режим принудительно идёт через LAN/WAN интерфейс.
    """
    urls = (
        "https://api.ipify.org",
        "https://ifconfig.me",
        "https://ipv4.icanhazip.com",
    )
    for url in urls:
        cmd = ["curl", "-4", "-fsS", "--max-time", "10"]
        if direct and NET_INTERFACE:
            cmd += ["--interface", NET_INTERFACE]
        cmd.append(url)
        rc, out, _ = await run_cmd(cmd, timeout=15)
        ip = out.strip()
        if rc == 0 and ip:
            return ip
    return ""


async def check_external_ip() -> None:
    direct = os.getenv("SERVER_MODE") == "gateway"
    new_ip = await _detect_public_ip(direct=direct)

    if not new_ip or new_ip == state.external_ip:
        return

    old_ip = state.external_ip
    state.external_ip = new_ip
    state.save()
    logger.info(f"Внешний IP: {old_ip} → {new_ip}")

    # Gateway Mode: обновить nft set с IP роутера
    if os.getenv("SERVER_MODE") == "gateway":
        try:
            await run_cmd(
                ["nft", "flush", "set", "inet", "vpn", "router_external_ips"],
                timeout=5,
            )
            await run_cmd(
                ["nft", "add", "element", "inet", "vpn", "router_external_ips",
                 "{", new_ip, "}"],
                timeout=5,
            )
            logger.info(f"Gateway: router_external_ips обновлён → {new_ip}")
        except Exception as e:
            logger.warning(f"Gateway: не удалось обновить router_external_ips: {e}")

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
            # DuckDNS API expects subdomain only, not full domain
            ddns_subdomain = DDNS_DOMAIN.replace(".duckdns.org", "")
            url = f"https://www.duckdns.org/update?domains={ddns_subdomain}&token={DDNS_TOKEN}&ip={ip}"
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
async def probe_vps_reachability() -> None:
    """Проверка доступности VPS через Tier-2 туннель.

    Исторически watchdog слал heartbeat на public `:8081`, но на VPS такого
    сервиса нет. Для health score нам важнее реальная доступность VPS по
    data-plane, поэтому используем node-exporter на `VPS_TUNNEL_IP:9100`.
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://{VPS_TUNNEL_IP}:9100/metrics",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    state.last_heartbeat_ts = time.time()
    except Exception:
        pass


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
                state.cert_days[label] = days_left
                if days_left <= warn_days:
                    alert(
                        f"⚠️ Сертификат *{label}* истекает через *{days_left} дн.*\n"
                        f"Путь: `{path}`\nИспользуйте /renew-cert или /renew-ca"
                    )
            except Exception as exc:
                state.cert_days[label] = -1
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
            if "dpi_direct" not in out_s:
                issues.append("set dpi_direct отсутствует")
        else:
            rc_dp, _, _ = await run_cmd(["nft", "list", "set", "inet", "vpn", "dpi_direct"], timeout=5)
            if rc_dp != 0:
                issues.append("set dpi_direct отсутствует")

    # Gateway Mode: дополнительные проверки
    if os.getenv("SERVER_MODE") == "gateway":
        rc_gw, out_gw, _ = await run_cmd(["nft", "list", "ruleset"], timeout=10)
        if rc_gw == 0:
            for check_item in ["prerouting_nat", "router_external_ips"]:
                if check_item not in out_gw:
                    issues.append(f"Gateway: {check_item} отсутствует в nftables")

    if not issues:
        logger.debug("check_nftables_integrity: OK")
        state.nftables_ok = True
        state.nftables_checked = True
        return

    state.nftables_ok = False
    state.nftables_checked = True
    details = "; ".join(issues)
    logger.warning(f"check_nftables_integrity: расхождения: {details}")

    # Восстановить правила
    rc_r, _, err = await run_cmd(["nft", "-f", "/etc/nftables.conf"], timeout=15)
    if rc_r == 0:
        # Восстановить blocked_static (blocked_dynamic и dpi_direct — self-healing через dnsmasq после warmup)
        await run_cmd(["nft", "-f", "/etc/nftables-blocked-static.conf"], timeout=15)
        # AR3 fix: запустить dns-warmup чтобы dnsmasq заново заполнил blocked_dynamic и dpi_direct
        logger.warning("nftables restored — running DNS warmup to refill dynamic sets")
        await run_cmd(["bash", "/opt/vpn/scripts/dns-warmup.sh"], timeout=60)
        alert(
            f"🔥 *nftables: правила изменены или сброшены!*\n\n"
            f"Проблемы: `{details}`\n\n"
            f"✅ Правила восстановлены из `/etc/nftables.conf`, DNS warmup запущен"
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
# Мониторинг: WireGuard интерфейсы (для quick health check)
# ---------------------------------------------------------------------------
async def check_wg_interfaces() -> None:
    """Проверяет наличие wg0 / wg1 интерфейсов, обновляет state."""
    rc0, _, _ = await run_cmd(["ip", "link", "show", "wg0"], timeout=5)
    state.wg0_up = rc0 == 0
    rc1, _, _ = await run_cmd(["ip", "link", "show", "wg1"], timeout=5)
    state.wg1_up = rc1 == 0
    state.last_wg_check_ts = time.time()


# ---------------------------------------------------------------------------
# Мониторинг: количество элементов в nft sets (для standard health check)
# ---------------------------------------------------------------------------
async def check_nftset_counts() -> None:
    """Считает элементы в blocked_static / blocked_dynamic / dpi_direct."""
    for set_name in ("blocked_static", "blocked_dynamic", "dpi_direct"):
        rc, out, _ = await run_cmd(
            ["nft", "list", "set", "inet", "vpn", set_name], timeout=5
        )
        if rc != 0:
            state.nftset_counts[set_name] = -1
            continue
        # Считаем запятые + 1 внутри { ... elements = { ... } }
        import re as _re
        m = _re.search(r'elements\s*=\s*\{([^}]*)\}', out, _re.DOTALL)
        if m:
            body = m.group(1).strip()
            count = len(body.split(",")) if body else 0
        else:
            count = 0
        state.nftset_counts[set_name] = count


# ---------------------------------------------------------------------------
# Мониторинг: nfqws реально обрабатывает пакеты (standard health check)
# ---------------------------------------------------------------------------
async def check_nfqws_counter() -> None:
    """Проверяет что основная NFQUEUE 200 привязана к userspace consumer.

    В /proc/net/netfilter/nfnetlink_queue поле queue_total — это не "сколько
    пакетов было обработано", а текущая длина очереди. Для исправно работающего
    nfqws оно как раз часто равно 0, поэтому использовать его как health
    indicator нельзя.
    """
    nfq_file = Path("/proc/net/netfilter/nfnetlink_queue")
    if not nfq_file.exists():
        state.nfqws_ok = False
        return
    try:
        content = nfq_file.read_text()
        lines = [l for l in content.splitlines() if l.strip()]
        if not lines:
            state.nfqws_ok = False
            return
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    queue_num = int(parts[0])
                    peer_portid = int(parts[1])
                    if queue_num == 200 and peer_portid > 0:
                        state.nfqws_ok = True
                        return
                except ValueError:
                    pass
        state.nfqws_ok = False
    except Exception as exc:
        logger.debug(f"check_nfqws_counter: {exc}")
        state.nfqws_ok = False


# ---------------------------------------------------------------------------
# Health Check — check helper functions
# ---------------------------------------------------------------------------
async def _health_quick_checks() -> list[CheckResult]:
    """Quick tier: читает state, минимум syscall."""
    results: list[CheckResult] = []
    now = time.time()

    # 1. Event loop alive
    loop_age = now - state.last_monitoring_tick if state.last_monitoring_tick > 0 else -1
    if loop_age < 0:
        warmup_age = int(max(0, now - state.started_at.timestamp()))
        if warmup_age <= 60:
            results.append(CheckResult("watchdog_event_loop", "ok", "startup warmup", weight=10))
        else:
            results.append(CheckResult("watchdog_event_loop", "warn", "ещё не запущен", weight=10))
    elif loop_age < 90:
        results.append(CheckResult("watchdog_event_loop", "ok", f"tick {loop_age:.0f}s назад", weight=10))
    else:
        results.append(CheckResult("watchdog_event_loop", "fail", f"последний tick {loop_age:.0f}s назад", weight=10))

    # 2. Tunnel up
    if not state.degraded_mode and state.last_rtt > 0:
        results.append(CheckResult("tunnel_up", "ok", f"RTT={state.last_rtt:.0f}ms", weight=10))
    elif state.degraded_mode:
        results.append(CheckResult("tunnel_up", "fail", "degraded mode активен", weight=10))
    else:
        results.append(CheckResult("tunnel_up", "warn", "RTT=0 (первый запуск?)", weight=10))

    # 3. DNS
    if state.dnsmasq_up == 1:
        results.append(CheckResult("dns_responding", "ok", "dnsmasq отвечает", weight=5))
    else:
        results.append(CheckResult("dns_responding", "fail", "dnsmasq не отвечает", weight=5))

    # 4. wg0
    if state.last_wg_check_ts <= 0:
        results.append(CheckResult("wg0_active", "warn", "ещё не проверялось", weight=5))
    elif state.wg0_up:
        results.append(CheckResult("wg0_active", "ok", weight=5))
    else:
        results.append(CheckResult("wg0_active", "fail", "wg0 не найден", weight=5))

    # 5. telegram-bot
    bot = state.docker_health.get("telegram-bot")
    if bot == 1:
        results.append(CheckResult("telegram_bot", "ok", weight=5))
    elif bot == 0:
        results.append(CheckResult("telegram_bot", "fail", "контейнер exited/unhealthy", weight=5))
    else:
        results.append(CheckResult("telegram_bot", "warn", "статус ещё не проверялся", weight=5))

    # 6. telegram-bot runtime sync
    if state.bot_runtime_drift:
        age = int(max(0, now - state.bot_runtime_drift_since)) if state.bot_runtime_drift_since > 0 else 0
        status = "fail" if age >= BOT_DRIFT_CONFIRM_SECONDS else "warn"
        detail = state.bot_runtime_drift_detail or "runtime drift"
        if age > 0:
            detail = f"{detail} ({age}s)"
        results.append(CheckResult("telegram_bot_runtime_sync", status, detail, weight=5))
    else:
        results.append(CheckResult("telegram_bot_runtime_sync", "ok", weight=5))

    # 7. docker-compose runtime sync
    if state.compose_runtime_drift:
        age = int(max(0, now - state.compose_runtime_drift_since)) if state.compose_runtime_drift_since > 0 else 0
        status = "fail" if age >= COMPOSE_DRIFT_CONFIRM_SECONDS else "warn"
        detail = state.compose_runtime_drift_detail or "runtime drift"
        if age > 0:
            detail = f"{detail} ({age}s)"
        results.append(CheckResult("compose_runtime_sync", status, detail, weight=3))
    else:
        results.append(CheckResult("compose_runtime_sync", "ok", weight=3))

    # 8. watchdog runtime sync
    if state.watchdog_runtime_drift:
        age = int(max(0, now - state.watchdog_runtime_drift_since)) if state.watchdog_runtime_drift_since > 0 else 0
        status = "fail" if age >= WATCHDOG_DRIFT_CONFIRM_SECONDS else "warn"
        detail = state.watchdog_runtime_drift_detail or "runtime drift"
        if age > 0:
            detail = f"{detail} ({age}s)"
        results.append(CheckResult("watchdog_runtime_sync", status, detail, weight=3))
    else:
        results.append(CheckResult("watchdog_runtime_sync", "ok", weight=3))

    # 9. wg1
    if state.wg1_up:
        results.append(CheckResult("wg1_active", "ok", weight=3))
    else:
        results.append(CheckResult("wg1_active", "warn", "wg1 не найден (нет WG клиентов?)", weight=3))

    # 10. xray-client* (хотя бы один)
    xray = [k for k in state.docker_health if "xray-client" in k]
    xray_ok = sum(1 for c in xray if state.docker_health.get(c) == 1)
    if xray and xray_ok > 0:
        results.append(CheckResult("xray_client", "ok", f"{xray_ok}/{len(xray)} живы", weight=3))
    elif xray:
        results.append(CheckResult("xray_client", "fail", "все xray-client упали", weight=3))
    else:
        results.append(CheckResult("xray_client", "warn", "контейнеры не обнаружены", weight=3))

    return results


async def _health_standard_checks() -> list[CheckResult]:
    """Standard tier: nftables, стеки, сертификаты, диск, heartbeat."""
    results: list[CheckResult] = []
    enabled_dpi_services = [s for s in state.dpi_services if s.get("enabled", True)]
    dpi_lane_active = state.dpi_enabled and bool(enabled_dpi_services)

    # 8–9. nft sets non-empty
    for set_name, w in [("blocked_static", 5), ("blocked_dynamic", 3)]:
        count = state.nftset_counts.get(set_name, -2)
        if count > 0:
            results.append(CheckResult(f"nft_{set_name}_nonempty", "ok", f"{count} элементов", weight=w, tier="standard"))
        elif count == 0:
            results.append(CheckResult(f"nft_{set_name}_nonempty", "warn", "set пустой — dns-warmup?", weight=w, tier="standard"))
        else:
            results.append(CheckResult(f"nft_{set_name}_nonempty", "warn", "ещё не проверялось", weight=w, tier="standard"))

    # 10. dpi_direct — имеет смысл только когда DPI bypass реально включён
    dpi_count = state.nftset_counts.get("dpi_direct", -2)
    if not dpi_lane_active:
        results.append(CheckResult("nft_dpi_direct_nonempty", "ok", "DPI bypass выключен", weight=0, tier="standard"))
    elif dpi_count > 0:
        results.append(CheckResult("nft_dpi_direct_nonempty", "ok", f"{dpi_count} элементов", weight=3, tier="standard"))
    elif dpi_count == 0:
        results.append(CheckResult("nft_dpi_direct_nonempty", "warn", "set пустой — dns-warmup или нет резолвов DPI-доменов", weight=3, tier="standard"))
    else:
        results.append(CheckResult("nft_dpi_direct_nonempty", "warn", "ещё не проверялось", weight=3, tier="standard"))

    # 11. Kill switch
    if state.nftables_ok:
        results.append(CheckResult("kill_switch_rules", "ok", weight=10, tier="standard"))
    elif not state.nftables_checked:
        results.append(CheckResult("kill_switch_rules", "warn", "ещё не проверялось", weight=10, tier="standard"))
    else:
        results.append(CheckResult("kill_switch_rules", "fail", "правила расходятся с эталоном", weight=10, tier="standard"))

    # 12. nfqws / NFQUEUE dataplane
    if not dpi_lane_active:
        results.append(CheckResult("nfqws_processing", "ok", "DPI bypass выключен; zapret standby допустим", weight=0, tier="standard"))
    else:
        rc_zp, _, _ = await run_cmd(["nft", "list", "table", "inet", "zapret_main"], timeout=5)
        if rc_zp != 0:
            results.append(CheckResult("nfqws_processing", "warn", "NFQUEUE dataplane не активирован", weight=3, tier="standard"))
        elif state.nfqws_ok is True:
            results.append(CheckResult("nfqws_processing", "ok", "очередь 200 привязана, NFQUEUE dataplane активен", weight=3, tier="standard"))
        elif state.nfqws_ok is False:
            results.append(CheckResult("nfqws_processing", "warn", "очередь 200 не привязана к nfqws", weight=3, tier="standard"))
        else:
            results.append(CheckResult("nfqws_processing", "warn", "ещё не проверялось", weight=3, tier="standard"))

    # 13. Cert expiry
    for label, days in state.cert_days.items():
        w_days = CERT_WARN_CA_DAYS if label == "CA" else CERT_WARN_CLIENT_DAYS
        if days > w_days:
            results.append(CheckResult(f"cert_{label}_expiry", "ok", f"{days} дней", weight=5, tier="standard"))
        elif days > 0:
            results.append(CheckResult(f"cert_{label}_expiry", "warn", f"истекает через {days} дн.", weight=5, tier="standard"))
        else:
            results.append(CheckResult(f"cert_{label}_expiry", "fail", "просрочен или ошибка чтения", weight=5, tier="standard"))

    # 14. VPS heartbeat
    if state.last_heartbeat_ts <= 0:
        results.append(CheckResult("vps_reachable", "warn", "ещё не проверялось", weight=5, tier="standard"))
        hb_age = None
    else:
        hb_age = time.time() - state.last_heartbeat_ts
    if hb_age is not None and hb_age < 120:
        results.append(CheckResult("vps_reachable", "ok", f"probe {hb_age:.0f}s назад", weight=5, tier="standard"))
    elif hb_age is not None and hb_age < 300:
        results.append(CheckResult("vps_reachable", "warn", f"probe {hb_age:.0f}s назад", weight=5, tier="standard"))
    elif hb_age is not None:
        results.append(CheckResult("vps_reachable", "fail", f"нет probe {hb_age:.0f}s", weight=5, tier="standard"))

    # 15. Disk
    try:
        pct = psutil.disk_usage("/").percent
        if pct < DISK_WARN_PCT:
            results.append(CheckResult("disk_usage", "ok", f"{pct:.0f}%", weight=5, tier="standard"))
        elif pct < DISK_AGGRESSIVE_PCT:
            results.append(CheckResult("disk_usage", "warn", f"{pct:.0f}% (порог {DISK_WARN_PCT}%)", weight=5, tier="standard"))
        else:
            results.append(CheckResult("disk_usage", "fail", f"{pct:.0f}% КРИТИЧНО", weight=5, tier="standard"))
    except Exception as exc:
        results.append(CheckResult("disk_usage", "warn", str(exc)[:80], weight=5, tier="standard"))

    # 16. Stacks testable
    if state.stacks_ok_count > 0:
        results.append(CheckResult("stacks_testable", "ok",
                                   f"{state.stacks_ok_count} из {len(STACK_ORDER)} работают",
                                   weight=5, tier="standard"))
    elif state.stacks_checked:
        results.append(CheckResult("stacks_testable", "fail", "все стеки недоступны", weight=5, tier="standard"))
    else:
        results.append(CheckResult("stacks_testable", "warn", "ещё не проверялось", weight=5, tier="standard"))

    return results


async def _hc_sqlite_integrity() -> CheckResult:
    import sqlite3 as _sqlite3
    if not BOT_DB_PATH.exists():
        return CheckResult("sqlite_integrity", "warn", f"DB не найдена: {BOT_DB_PATH}", weight=3, tier="deep")
    try:
        loop = asyncio.get_event_loop()
        def _check_sync() -> str:
            with _sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=5) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return row[0] if row else "unknown"
        result_str = await loop.run_in_executor(None, _check_sync)
        if result_str == "ok":
            return CheckResult("sqlite_integrity", "ok", weight=3, tier="deep")
        return CheckResult("sqlite_integrity", "fail", result_str[:120], weight=3, tier="deep")
    except Exception as exc:
        return CheckResult("sqlite_integrity", "fail", str(exc)[:120], weight=3, tier="deep")


async def _hc_backup_freshness() -> CheckResult:
    backup_dir = Path("/opt/vpn/backups")
    if not backup_dir.exists():
        return CheckResult("backup_freshness", "warn", "директория /opt/vpn/backups не найдена", weight=3, tier="deep")
    backups = sorted(
        list(backup_dir.glob("*.tar.gz")) + list(backup_dir.glob("*.tar.xz")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not backups:
        return CheckResult("backup_freshness", "warn", "бэкапы не найдены", weight=3, tier="deep")
    age_days = (time.time() - backups[0].stat().st_mtime) / 86400
    if age_days < BACKUP_MAX_AGE_DAYS:
        return CheckResult("backup_freshness", "ok", f"последний {age_days:.1f} дн. назад", weight=3, tier="deep")
    return CheckResult("backup_freshness", "warn",
                       f"последний {age_days:.1f} дн. назад (порог {BACKUP_MAX_AGE_DAYS} дн.)", weight=3, tier="deep")


async def _hc_file_permissions() -> list[CheckResult]:
    results: list[CheckResult] = []
    checks = [
        ("/opt/vpn/.env",                  0o600, "env_600"),
        ("/opt/vpn",                       0o700, "opt_vpn_700"),
        ("/opt/vpn/telegram-bot/data",     0o750, "bot_data_750"),
    ]
    for path_str, expected, check_name in checks:
        p = Path(path_str)
        if not p.exists():
            results.append(CheckResult(f"perm_{check_name}", "warn", f"{path_str} не найден", weight=1, tier="deep"))
            continue
        actual = p.stat().st_mode & 0o777
        if actual == expected:
            results.append(CheckResult(f"perm_{check_name}", "ok", oct(actual), weight=1, tier="deep"))
        else:
            results.append(CheckResult(f"perm_{check_name}", "warn",
                                       f"{oct(actual)} ожидалось {oct(expected)}", weight=1, tier="deep"))
    return results


def _hc_fernet_key() -> CheckResult:
    key = os.getenv("DB_ENCRYPTION_KEY", "")
    if not key:
        return CheckResult("fernet_key_set", "warn", "DB_ENCRYPTION_KEY не задан в .env", weight=1, tier="deep")
    # Fernet key: urlsafe-base64, 44 символа, заканчивается на =, декодируется в 32 байта
    if len(key) == 44 and key.endswith("="):
        try:
            decoded = base64.urlsafe_b64decode(key)
            if len(decoded) == 32:
                return CheckResult("fernet_key_set", "ok", "формат корректен (32-byte base64url)", weight=1, tier="deep")
        except Exception:
            pass
    return CheckResult("fernet_key_set", "warn", "неверный формат (ожидается 44-символьный base64url, =)", weight=1, tier="deep")


async def _hc_vps_ssh() -> CheckResult:
    if state.last_rtt <= 0:
        return CheckResult("vps_ssh_audit", "warn", "tier-2 туннель не установлен, SSH пропущен", weight=3, tier="deep")
    vps_tunnel_ip = VPS_TUNNEL_IP or "10.177.2.2"
    ssh_key  = os.getenv("VPS_SSH_KEY", "/root/.ssh/vpn_id_ed25519")
    ssh_user = os.getenv("BACKUP_VPS_USER", "sysadmin")
    rc, out, _ = await run_cmd(
        [
            "ssh", "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=8",
            "-o", "BatchMode=yes",
            f"{ssh_user}@{vps_tunnel_ip}",
            "systemctl is-active 3x-ui && echo ok",
        ],
        timeout=15,
    )
    if rc == 0 and "ok" in out:
        return CheckResult("vps_ssh_audit", "ok", "3x-ui активен на VPS", weight=3, tier="deep")
    return CheckResult("vps_ssh_audit", "warn", f"SSH rc={rc} или 3x-ui неактивен", weight=3, tier="deep")


async def _health_deep_checks() -> list[CheckResult]:
    """Deep tier: SQLite, бэкап, права, fernet key, VPS SSH."""
    results: list[CheckResult] = []
    results.append(await _hc_sqlite_integrity())
    results.append(await _hc_backup_freshness())
    results.extend(await _hc_file_permissions())
    results.append(_hc_fernet_key())
    results.append(await _hc_vps_ssh())
    return results


# ---------------------------------------------------------------------------
# Health Check — HealthChecker класс
# ---------------------------------------------------------------------------
class HealthChecker:
    """Агрегирует результаты проверок в единый score 0–100."""

    def __init__(self) -> None:
        self._report: dict = {
            "score": 0.0, "status": "unknown", "checks": [],
            "summary": {}, "tier": "none", "timestamp": "",
            "post_deploy_watch": False,
        }
        self._alert_dedup_ts: float = 0.0

    def get_cached(self) -> dict:
        return self._report

    def _compute(self, results: list[CheckResult], tier: str) -> dict:
        total_w = sum(r.weight for r in results)
        ok_w    = sum(r.weight for r in results if r.status == "ok")
        score   = round(ok_w / total_w * 100, 1) if total_w else 100.0
        if score >= 80:
            status = "ok"
        elif score >= 50:
            status = "degraded"
        else:
            status = "critical"
        return {
            "score":  score,
            "status": status,
            "tier":   tier,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "ok":   sum(1 for r in results if r.status == "ok"),
                "warn": sum(1 for r in results if r.status == "warn"),
                "fail": sum(1 for r in results if r.status == "fail"),
            },
            "post_deploy_watch": time.time() < state.post_deploy_until,
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail,
                 "weight": r.weight, "tier": r.tier}
                for r in results
            ],
        }

    def _save(self, report: dict) -> None:
        self._report = report
        state.health_score = report["score"]
        state.health_report = report

    def _maybe_alert(self, report: dict) -> None:
        now = time.time()
        if report["score"] < HEALTH_SCORE_THRESHOLD:
            if now - self._alert_dedup_ts > 1800:
                failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
                alert(
                    f"⚠️ *Health Score: {report['score']:.0f}/100* ({report['status']})\n"
                    f"Проблемы: {', '.join(failed[:5]) if failed else 'см. /health'}"
                )
                self._alert_dedup_ts = now
        else:
            self._alert_dedup_ts = 0.0  # сброс после восстановления

    async def run_quick(self) -> dict:
        results = await _health_quick_checks()
        report  = self._compute(results, "quick")
        self._save(report)
        self._maybe_alert(report)
        return report

    async def run_standard(self) -> dict:
        results = await _health_quick_checks()
        results += await _health_standard_checks()
        report  = self._compute(results, "standard")
        self._save(report)
        self._maybe_alert(report)
        return report

    async def run_deep(self) -> dict:
        results = await _health_quick_checks()
        results += await _health_standard_checks()
        results += await _health_deep_checks()
        report  = self._compute(results, "deep")
        self._save(report)
        self._maybe_alert(report)
        return report


# Singleton — создаётся при импорте, используется везде
health_checker = HealthChecker()


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


async def _dpi_warmup_domains(domains: list[str]) -> None:
    """Прогреть dnsmasq по активным DPI-доменам, чтобы nft set dpi_direct наполнился сразу."""
    unique_domains: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        d = (domain or "").strip().lower().strip(".")
        if not d or d in seen:
            continue
        seen.add(d)
        unique_domains.append(d)

    warmed = 0
    for domain in unique_domains[:100]:
        rc, _, _ = await run_cmd(["dig", "+short", "@127.0.0.1", domain, "A"], timeout=5)
        if rc == 0:
            warmed += 1
        await asyncio.sleep(0.05)

    logger.info("[DPI] dns warmup completed for %s/%s domains", warmed, len(unique_domains[:100]))


async def _dpi_sync_active_domains() -> None:
    """Синхронизировать dnsmasq + сразу прогреть активные DPI-домены."""
    await _regen_dpi_dnsmasq()
    await _dpi_warmup_domains([
        domain
        for svc in state.dpi_services
        if svc.get("enabled")
        for domain in svc.get("domains", [])
    ])


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
    """Включить DPI bypass: routing + zapret start + dnsmasq."""
    state.dpi_enabled = True
    state.save()
    await _dpi_apply_routing()
    zp = plugins.get("zapret")
    if zp:
        if not (await zp.test(timeout=5))[0]:
            await zp.start()
        await zp.activate()   # добавить NFQUEUE-правила в nftables (inet zapret_main)
    await _regen_dpi_dnsmasq()
    await _dpi_warmup_domains([
        domain
        for svc in state.dpi_services
        if svc.get("enabled")
        for domain in svc.get("domains", [])
    ])
    enabled_names = [s["display"] for s in state.dpi_services if s.get("enabled")]
    alert(
        f"⚡ *DPI bypass включён*\n"
        f"Сервисы: {', '.join(enabled_names) if enabled_names else 'нет (добавьте /dpi add)'}\n"
        f"Трафик к ним идёт напрямую через zapret, минуя VPS."
    )
    logger.info("[DPI] включён")


async def _startup_reconcile() -> None:
    """Агрессивное восстановление runtime сразу после загрузки/рестарта."""
    actions: list[str] = []
    failures: list[str] = []

    try:
        await check_dnsmasq()
        actions.append("dnsmasq")
    except Exception as exc:
        failures.append(f"dnsmasq: {exc}")

    try:
        await check_dnsmasq_config_sync()
        actions.append("dnsmasq-config")
    except Exception as exc:
        failures.append(f"dnsmasq-config: {exc}")

    try:
        await check_telegram_bot_runtime_sync()
        actions.append("telegram-bot-runtime")
    except Exception as exc:
        failures.append(f"telegram-bot-runtime: {exc}")

    try:
        await check_compose_runtime_sync()
        actions.append("compose-runtime")
    except Exception as exc:
        failures.append(f"compose-runtime: {exc}")

    try:
        await check_watchdog_runtime_sync()
        actions.append("watchdog-runtime")
    except Exception as exc:
        failures.append(f"watchdog-runtime: {exc}")

    try:
        await reconcile_wg_runtime_from_db()
        actions.append("wg-peer-reconcile")
    except Exception as exc:
        failures.append(f"wg-peer-reconcile: {exc}")

    try:
        await check_nftset_counts()
        actions.append("nftset-counts")
    except Exception as exc:
        failures.append(f"nftset-counts: {exc}")

    try:
        await check_nfqws_counter()
        actions.append("nfqws")
    except Exception as exc:
        failures.append(f"nfqws: {exc}")

    try:
        await check_containers()
        actions.append("containers")
    except Exception as exc:
        failures.append(f"containers: {exc}")

    report = await health_checker.run_quick()
    summary = report.get("summary", {})
    if failures:
        logger.warning("startup reconcile completed with failures: %s", "; ".join(failures))
        alert(
            "⚠️ *startup reconcile completed with issues*\n"
            f"Actions: `{', '.join(actions)}`\n"
            f"Health: ✅ {summary.get('ok', 0)}  ⚠️ {summary.get('warn', 0)}  ❌ {summary.get('fail', 0)}\n"
            + "\n".join(f"• `{item[:180]}`" for item in failures[:8])
        )
        return

    logger.info("startup reconcile completed successfully: %s", ", ".join(actions))
    alert(
        "✅ *startup reconcile completed*\n"
        f"Actions: `{', '.join(actions)}`\n"
        f"Health: ✅ {summary.get('ok', 0)}  ⚠️ {summary.get('warn', 0)}  ❌ {summary.get('fail', 0)}"
    )


async def _dpi_disable_impl() -> None:
    """Выключить DPI bypass: routing убрать, dnsmasq очистить."""
    state.dpi_enabled = False
    state.save()
    await _dpi_remove_routing()
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
    for name in plugins.auto_names():
        if name == current:
            continue
        plugin = plugins.get(name)
        if not plugin:
            continue

        async with _LOCK:
            ok, mbps = await _test_stack_runtime(plugin, name, timeout=15)
        if not ok:
            failed.append(name)
            alert(f"⚠️ Standby стек *{name}* не прошёл проверку")
        else:
            logger.info(f"Standby {name}: OK ({mbps:.1f} Mbps)")

    total = len(plugins.auto_names())
    state.stacks_ok_count = total - len(failed)
    state.stacks_checked = True

    if not failed:
        logger.info("Все standby туннели в норме")


# ---------------------------------------------------------------------------
# Decision Engine — единый цикл failover + ротация
# ---------------------------------------------------------------------------
_LOCK = asyncio.Lock()   # глобальный mutex decision engine


def _get_stack_socks_port(stack_name: str) -> int:
    """Возвращает SOCKS5-порт плагина, читая из client.yaml."""
    plugin = plugins.get(stack_name)
    if not plugin:
        return 1080
    cy_path = plugin.dir / "client.yaml"
    if not cy_path.exists():
        return 1080
    try:
        import yaml as _yaml
        cy = _yaml.safe_load(cy_path.read_text())
        sp = cy.get("socks_port")
        if sp is None:
            listen = cy.get("socks5", {}).get("listen", "127.0.0.1:1080")
            sp = listen.split(":")[-1]
        return int(sp)
    except Exception:
        return 1080


def _write_vpn_state_files(stack_name: str) -> None:
    """Атомарно записывает /var/run/vpn-active-{socks-port,stack,tun}.
    Используется ssh-proxy.sh для адаптивного туннелирования SSH.
    """
    socks_port = _get_stack_socks_port(stack_name)
    plugin = plugins.get(stack_name)
    tun_name = ""
    if plugin and not plugin.meta.get("direct_mode"):
        candidate = plugin.meta.get("tun_name", f"tun-{stack_name}")
        if (Path("/sys/class/net") / candidate).exists():
            tun_name = candidate

    for path, content in (
        ("/var/run/vpn-active-socks-port", str(socks_port)),
        ("/var/run/vpn-active-stack",      stack_name),
    ):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"Не удалось записать {path}: {e}")

    tun_path = Path("/var/run/vpn-active-tun")
    try:
        if tun_name:
            tmp = tun_path.with_suffix(".tmp")
            tmp.write_text(tun_name)
            os.replace(tmp, tun_path)
        else:
            tun_path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Не удалось обновить {tun_path}: {e}")


async def _set_marked_route_for_stack(stack_name: str) -> bool:
    """Установить маршрут table marked для указанного стека.
    Возвращает True если маршрут установлен, False если стек не готов.
    """
    plugin = plugins.get(stack_name)
    if not plugin:
        logger.warning(f"_set_marked_route_for_stack: плагин {stack_name} не найден")
        return False

    if plugin.meta.get("direct_mode"):
        gw = GATEWAY_IP
        eth = NET_INTERFACE
        cmd = (
            ["ip", "route", "replace", "default", "via", gw, "dev", eth, "table", "marked"]
            if gw else
            ["ip", "route", "replace", "default", "dev", eth, "table", "marked"]
        )
        rc, _, err = await run_cmd(cmd, timeout=5)
        if rc != 0:
            logger.warning(f"Не удалось установить direct route для {stack_name}: {err}")
            return False
        return True

    tun_name = plugin.meta.get("tun_name", f"tun-{stack_name}")
    rc, _, _ = await run_cmd(["ip", "link", "show", tun_name], timeout=3)
    if rc != 0:
        logger.warning(f"tun интерфейс {tun_name} отсутствует — table marked не обновлена")
        return False

    rc, _, err = await run_cmd(
        ["ip", "route", "replace", "default", "dev", tun_name, "table", "marked"],
        timeout=5,
    )
    if rc != 0:
        logger.warning(f"Не удалось установить маршрут table marked → {tun_name}: {err}")
        return False
    return True


async def _set_marked_route_unreachable() -> None:
    """Fail-closed: если активный стек не поднялся, blocked-трафик не должен уходить в stale tun."""
    await run_cmd(["ip", "route", "replace", "unreachable", "default", "table", "marked"], timeout=5)
    try:
        Path("/run/vpn-active-tun").unlink(missing_ok=True)
    except Exception as exc:
        logger.debug(f"Не удалось удалить /run/vpn-active-tun: {exc}")
    logger.warning("table marked переведена в unreachable — активный tun отсутствует")


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
    if not await _set_marked_route_for_stack(new_stack):
        logger.error(f"Не удалось переключить table marked на стек {new_stack}")
        await plugin.stop()
        alert(f"⚠️ Failover на *{new_stack}* не удался: маршрут не установлен")
        return False

    # 3. Останавливаем старый стек
    old_plugin = plugins.get(old_stack)
    if old_plugin:
        await old_plugin.stop()

    state.active_stack = new_stack
    state.last_failover = datetime.now()
    state.all_stacks_down_since = None
    state.failover_count += 1
    state.rotation_log.append({
        "ts":     datetime.now().isoformat(timespec="seconds"),
        "from":   old_stack,
        "to":     new_stack,
        "reason": reason,
    })
    if len(state.rotation_log) > 20:
        state.rotation_log = state.rotation_log[-20:]
    state.save()
    _write_vpn_state_files(new_stack)

    # Перезапускаем tier-2 SSH туннель — он зависит от активного стека (SOCKS5).
    # После смены стека меняется порт прокси, autossh должен переподключиться.
    asyncio.create_task(run_cmd(
        ["systemctl", "restart", "autossh-tier2"],
        timeout=15,
    ))

    logger.info(f"Стек переключён: {old_stack} → {new_stack}")
    alert(f"🔄 VPN стек переключён: *{old_stack}* → *{new_stack}*\nПричина: {reason}")
    return True


async def _failover_impl(reason: str) -> None:
    """Внутренняя логика failover. Вызывается под _LOCK (не захватывает его сама)."""
    state.failover_in_progress = True
    try:
        current = state.active_stack
        ordered = plugins.auto_names()
        try:
            cur_pos = ordered.index(current)
        except ValueError:
            cur_pos = len(ordered) - 1
        candidates = ordered[:cur_pos] + ordered[cur_pos + 1:]
        for candidate in candidates:
            plugin = plugins.get(candidate)
            if not plugin:
                continue
            logger.info(f"Тест кандидата: {candidate}")
            ok, mbps = await _test_stack_runtime(plugin, candidate, timeout=10)
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

    # Поднимаем стек по умолчанию, затем оцениваем остальные честным runtime-test.
    starter = plugins.get(DEFAULT_STACK)
    if starter and not starter.meta.get("direct_mode"):
        await starter.start()
        await _set_marked_route_for_stack(DEFAULT_STACK)
        state.active_stack = DEFAULT_STACK
        state.primary_stack = DEFAULT_STACK

    best_stack: Optional[str] = None
    best_mbps = 0.0

    for name in [n for n in STACK_ORDER if n in plugins.auto_names()]:
        plugin = plugins.get(name)
        if not plugin:
            continue
        ok, mbps = await _test_stack_runtime(plugin, name, timeout=10)
        logger.info(f"Оценка {name}: {'OK' if ok else 'FAIL'} {mbps:.1f} Mbps")
        if ok and mbps > best_mbps:
            best_mbps = mbps
            best_stack = name

    if best_stack and best_stack != state.active_stack:
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

        for name in [n for n in STACK_ORDER if n in plugins.auto_names()]:
            plugin = plugins.get(name)
            if not plugin:
                continue
            ok, mbps = await _test_stack_runtime(plugin, name, timeout=10)
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
    rtt_degrade_count = 0
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

    # Если после рестарта active_stack — direct_mode стек (zapret),
    # немедленно переключиться на лучший VPN-стек
    _active = plugins.get(state.active_stack)
    if _active and _active.meta.get("direct_mode"):
        logger.warning(f"active_stack={state.active_stack} — direct_mode, запускаем failover...")
        await _do_failover("direct_mode_recovery")

    while True:
        try:
            ok, rtt = await ping_vps()

            state.last_rtt = rtt if ok else 0.0
            state.ping_results.append(1 if ok else 0)

            if ok:
                ping_fails = 0
                state.all_stacks_down_since = None
                state.degraded_mode = False

                # Проверяем RTT деградацию (ДО добавления в baseline)
                avg = state.rtt_avg(state.active_stack)
                if avg > 0 and rtt > avg * RTT_DEGRADATION_FACTOR:
                    rtt_degrade_count += 1
                    logger.warning(f"RTT деградация #{rtt_degrade_count}: {rtt:.0f}ms (avg {avg:.0f}ms)")
                    if rtt_degrade_count >= 3:
                        rtt_degrade_count = 0
                        await _do_failover("rtt_degradation")
                else:
                    rtt_degrade_count = 0

                # Обновляем RTT baseline (только для VPN-стеков из STACK_ORDER)
                if state.active_stack in state.rtt_baseline:
                    state.rtt_baseline[state.active_stack].append(rtt)

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
    deep_check_done_today   = False
    last_standby_check_date = datetime.now().date()
    logger.info("monitoring_loop запущен")

    # Прогреваем состояние сразу после старта, чтобы health checks не ловили
    # значения по умолчанию после рестарта watchdog.
    await check_wg_interfaces()
    await probe_vps_reachability()
    await check_containers()
    await check_telegram_bot_runtime_sync()
    await check_compose_runtime_sync()
    await check_watchdog_runtime_sync()
    state.last_monitoring_tick = time.time()
    await health_checker.run_quick()

    while True:
        try:
            now = time.time()
            tick += 1

            # Обновляем timestamp каждого тика — для детектирования зависшего event loop
            state.last_monitoring_tick = now

            # Каждые 30 сек: dnsmasq
            if tick % 3 == 0:
                await check_dnsmasq()

            # Каждые 60 сек: проверка доступности VPS через Tier-2
            if now - last_heartbeat >= 60:
                await probe_vps_reachability()
                last_heartbeat = now

            # Каждые 5 мин: внешний IP, диск, small speedtest, блок. сайты, upload
            if tick % 30 == 0:
                await check_external_ip()
                await check_disk()
                await check_wg_interfaces()
                mbps = await speedtest_small()
                logger.debug(f"Speedtest down (100KB): {mbps:.1f} Mbps")
                up_mbps = await speedtest_upload()
                logger.debug(f"Speedtest up (100KB): {up_mbps:.1f} Mbps")
                vol_shaping = detect_volume_shaping()
                if vol_shaping:
                    alert(f"⚠️ {vol_shaping}")
                await check_blocked_sites()
                await check_upload_utilization()
                # Quick health check (5 мин)
                asyncio.create_task(health_checker.run_quick(), name="health-quick")
                # Post-deploy watch: немедленный алерт при любом FAIL
                if state.post_deploy_until > 0 and now < state.post_deploy_until:
                    _pdw_report = health_checker.get_cached()
                    _pdw_fails = [c["name"] for c in _pdw_report.get("checks", []) if c["status"] == "fail"]
                    if _pdw_fails:
                        alert(
                            f"🚨 *Post-deploy watch*: проблемы обнаружены\n"
                            + "\n".join(f"• {f}" for f in _pdw_fails[:5])
                        )

            # Каждые 10 мин: WG peers, контейнеры
            if tick % 60 == 0:
                await check_wg_peers()
                await check_containers()
                await check_telegram_bot_runtime_sync()
                await check_compose_runtime_sync()
                await check_watchdog_runtime_sync()
                await check_dnsmasq_config_sync()
                await reconcile_wg_runtime_from_db()

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
                await check_nftset_counts()
                await check_nfqws_counter()
                # Standard health check (1 час)
                asyncio.create_task(health_checker.run_standard(), name="health-standard")

            # В 04:00: deep health check (ежесуточно)
            now_dt = datetime.now()
            if now_dt.date() != last_standby_check_date:
                standby_checked_today = False
                zapret_probe_done_today = False
                deep_check_done_today   = False
                last_standby_check_date = now_dt.date()
            if not deep_check_done_today and now_dt.hour == 4 and now_dt.minute < 30:
                asyncio.create_task(health_checker.run_deep(), name="health-deep")
                deep_check_done_today = True

            # В 04:30 каждый день: проверка standby туннелей
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
    if credentials is None or not compare_digest(credentials.credentials, API_TOKEN):
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
    ip: Optional[str] = None        # желаемый IP (выдаётся ботом из DB-пула)

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
    ssh_port: int = 22
    tunnel_ip: str = ""

class VpsInstallRequest(BaseModel):
    ip: str
    password: str
    ssh_port: int = 22

class DpiServiceRequest(BaseModel):
    name: str = ""
    display: Optional[str] = None
    domains: Optional[list[str]] = None
    preset: Optional[str] = None   # "youtube"

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
    result: dict[str, Any] = {
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

    # Gateway Mode: добавляем счётчик LAN-клиентов из conntrack
    server_mode = os.getenv("SERVER_MODE", "hosted")
    lan_subnet = os.getenv("LAN_SUBNET", "")
    home_server_ip = os.getenv("HOME_SERVER_IP", "")
    if server_mode == "gateway" and lan_subnet:
        try:
            rc, out, _ = await run_cmd(
                ["conntrack", "-L", "--output", "extended"],
                timeout=10,
            )
            lan_ips: set[str] = set()
            net_prefix = ".".join(lan_subnet.split(".")[:3])  # напр. "192.168.1"
            for line in out.splitlines():
                m = re.search(r"src=(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    ip = m.group(1)
                    if ip.startswith(net_prefix) and ip != home_server_ip:
                        lan_ips.add(ip)
            result["lan_clients"] = len(lan_ips)
            result["lan_client_ips"] = sorted(lan_ips)
        except Exception:
            pass

    return result


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
        f'vpn_tunnel_upload_mbps {state.last_upload_mbps}',
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

    # Gateway Mode метрики
    if os.getenv("SERVER_MODE") == "gateway":
        lines.append("# HELP vpn_gateway_mode Gateway mode active")
        lines.append("# TYPE vpn_gateway_mode gauge")
        lines.append("vpn_gateway_mode 1")
        # lan_clients берётся из /status (state не хранит — вычисляется по запросу)
        lines.append("# HELP vpn_lan_clients_count LAN clients detected via conntrack")
        lines.append("# TYPE vpn_lan_clients_count gauge")
        lines.append("vpn_lan_clients_count 0")
    else:
        lines.append("vpn_gateway_mode 0")

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

    # Health score
    _hr = health_checker.get_cached()
    lines += [
        "# HELP vpn_health_score Overall VPN infrastructure health score (0-100)",
        "# TYPE vpn_health_score gauge",
        f'vpn_health_score{{tier="{_hr.get("tier", "none")}"}} {state.health_score}',
    ]

    return Response(content="\n".join(lines), media_type="text/plain")


@app.get("/health")
async def get_health(_: bool = Depends(_auth)):
    """Агрегированный health report со score 0–100."""
    return health_checker.get_cached()


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
                env=_child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate(privkey.encode())
            pubkey = out.decode().strip()

        # IP: используем запрошенный ботом, иначе ищем свободный в WireGuard
        wg = _wg_tool(iface)
        if req.ip:
            peer_ip = req.ip
        else:
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
        [sys.executable, "/opt/vpn/scripts/update-routes.py"], timeout=600
    )
    if rc == 0:
        alert("✅ Маршруты обновлены")
    else:
        alert(f"⚠️ Ошибка обновления маршрутов:\n`{err[:300]}`")


@app.post("/service/restart")
@limiter.limit("10/second")
async def post_service_restart(request: Request, req: ServiceRestartRequest, _: bool = Depends(_auth)):
    allowed = {"dnsmasq", "watchdog", "hysteria2", "nftables", "docker",
               "awg-quick@wg0", "wg-quick@wg1"}
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
    rc, out, err = await run_cmd(cmd, timeout=600)
    if rc != 0:
        # Ищем строки с ошибкой в stdout (deploy.sh пишет туда диагностику)
        combined = (out or "") + "\n" + (err or "")
        error_lines = [l for l in combined.splitlines() if any(
            kw in l for kw in ("❌", "FAIL", "Error", "error", "failed", "Cannot", "cannot")
        )]
        snippet = "\n".join(error_lines[-10:]) if error_lines else combined.strip()[-600:]
        alert(f"❌ Deploy завершился с ошибкой:\n`{snippet[:600]}`")
    else:
        ver = ""
        try:
            with open("/opt/vpn/version") as f:
                ver = f.read().strip()
        except Exception as exc:
            logger.debug("Не удалось прочитать /opt/vpn/version: %s", exc)
        # Определить что произошло по выводу
        output = (out or "").strip()
        if "не требуется" in output or "актуальна" in output:
            alert(f"ℹ️ Deploy: обновлений нет, версия `{ver}` актуальна")
        elif "Откат" in output:
            alert(f"⚠️ Deploy завершён откатом к предыдущей версии")
        else:
            alert(f"✅ Deploy завершён успешно, версия `{ver}`")
            # Запускаем post-deploy watch: усиленный мониторинг 15 мин
            state.post_deploy_until = time.time() + 900
            alert("🔍 *Post-deploy watch* активен — усиленный мониторинг 15 мин")


@app.post("/rollback")
@limiter.limit("10/second")
async def post_rollback(request: Request, bg: BackgroundTasks, _: bool = Depends(_auth)):
    bg.add_task(run_cmd, ["bash", "/opt/vpn/deploy.sh", "--rollback"], 300)
    return {"status": "rolling_back"}


class SkipVersionRequest(BaseModel):
    version: str


@app.post("/deploy/skip")
@limiter.limit("10/second")
async def post_deploy_skip(request: Request, req: SkipVersionRequest, _: bool = Depends(_auth)):
    if not re.match(r'^\d+\.\d+\.\d+(\.\d+)?$', req.version):
        raise HTTPException(status_code=400, detail="Invalid version format")
    skip_file = "/opt/vpn/.skip-version"
    try:
        with open(skip_file, "w") as f:
            f.write(req.version.strip())
        return {"status": "skipped", "version": req.version}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


class AdminNotifyRequest(BaseModel):
    text: str
    chat_id: str = ""


@app.post("/admin-notify")
@limiter.limit("10/second")
async def post_admin_notify(request: Request, req: AdminNotifyRequest, _: bool = Depends(_auth)):
    alert(req.text, req.chat_id)
    return {"status": "queued"}


@app.post("/admin-notify/reload")
@limiter.limit("10/second")
async def post_admin_notify_reload(request: Request, _: bool = Depends(_auth)):
    count = _reload_admin_cache()
    return {"status": "ok", "admin_count": count}




async def _manual_reassessment() -> None:
    """Тест всех стеков по запросу пользователя — отправляет отчёт в Telegram."""
    alert("🔍 Запуск теста скорости всех стеков...")

    # Базовые линии последовательно — параллельный запуск насыщает канал
    # и мешает точному измерению каждого теста
    vps_mbps = await speedtest_iperf_vps()
    direct_mbps = await speedtest_direct()

    results: list[tuple[str, str, float]] = []
    async with _LOCK:
        for name in plugins.all_names():
            plugin = plugins.get(name)
            if not plugin or plugin.meta.get("direct_mode"):
                continue
            if not plugin.auto_enabled:
                results.append((name, "disabled", 0.0))
                continue
            ok, mbps = await _test_stack_runtime(plugin, name, timeout=10)
            results.append((name, "ok" if ok else "fail", mbps))

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

    for name, status, mbps in results:
        icon = "✅" if status == "ok" else ("⚪" if status == "disabled" else "❌")
        marker = " ← активный" if name == state.active_stack else ""
        if status == "ok":
            pct = f"  ({round(mbps / base_mbps * 100)}%)" if base_mbps > 0 else ""
            speed = f"{mbps:.1f} Mbps{pct}"
        elif status == "disabled":
            speed = "не включён"
        else:
            speed = "недоступен"
        lines.append(f"{icon} {name}: {speed}{marker}")
        if status == "ok" and mbps > best_mbps:
            best_mbps, best_stack = mbps, name

    report = "\n".join(lines)
    if best_stack and best_stack != state.active_stack:
        async with _LOCK:
            await _do_switch(best_stack, "manual_reassessment")
        alert(
            f"📊 *Тест завершён*\n\n{report}\n\n"
            f"🔄 Переключено на `{best_stack}` ({best_mbps:.1f} Mbps)"
        )
    elif best_stack:
        alert(
            f"📊 *Тест завершён*\n\n{report}\n\n"
            f"✅ Текущий стек `{state.active_stack}` уже оптимален"
        )
    else:
        alert(f"📊 *Тест завершён*\n\n{report}\n\n⚠️ Все стеки недоступны")

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
        except Exception as exc:
            logger.debug("zapret test failed: %s", exc)
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
        asyncio.create_task(_dpi_sync_active_domains())
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
        asyncio.create_task(_dpi_sync_active_domains())
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
                asyncio.create_task(_dpi_sync_active_domains())
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
    elif GRAFANA_PASSWORD:
        import base64
        _creds = base64.b64encode(f"admin:{GRAFANA_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {_creds}"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    png = await r.read()
                    return Response(content=png, media_type="image/png")
                raise HTTPException(status_code=502, detail=f"Grafana вернул {r.status}")
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana недоступна: {exc}")


@app.post("/check")
@limiter.limit("10/second")
async def post_check_domain(request: Request, _: bool = Depends(_auth)):
    """Проверить домен: резолв → nft set lookup → manual lists."""
    body = await request.json()
    domain = body.get("domain", "").strip().lower().strip(".").split("/")[0]
    if not domain:
        raise HTTPException(status_code=400, detail="domain required")
    if not re.match(r'^[a-z0-9][a-z0-9.\-]*[a-z0-9]$', domain) or len(domain) > 253:
        raise HTTPException(status_code=400, detail="Invalid domain")

    result: dict[str, Any] = {"domain": domain}

    # 1. Резолв
    rc, out, _ = await run_cmd(["dig", "+short", "+time=3", domain], timeout=8)
    ips = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith(";") and "." in ln]
    result["ips"] = ips

    # 2. Проверка в nft sets
    in_static = False
    in_dynamic = False
    for ip in ips:
        rc_s, _, _ = await run_cmd(["nft", "get", "element", "inet", "vpn", "blocked_static",  f"{{ {ip} }}"], timeout=3)
        if rc_s == 0:
            in_static = True
        rc_d, _, _ = await run_cmd(["nft", "get", "element", "inet", "vpn", "blocked_dynamic", f"{{ {ip} }}"], timeout=3)
        if rc_d == 0:
            in_dynamic = True

    result["in_blocked_static"]  = in_static
    result["in_blocked_dynamic"] = in_dynamic

    # 3. Manual lists
    MANUAL_VPN    = Path("/etc/vpn-routes/manual-vpn.txt")
    MANUAL_DIRECT = Path("/etc/vpn-routes/manual-direct.txt")
    result["in_manual_vpn"]    = MANUAL_VPN.exists()    and domain in MANUAL_VPN.read_text()
    result["in_manual_direct"] = MANUAL_DIRECT.exists() and domain in MANUAL_DIRECT.read_text()

    # 4. Итоговый вердикт
    if result["in_manual_vpn"] or in_static or in_dynamic:
        result["verdict"] = "vpn"
    elif result["in_manual_direct"]:
        result["verdict"] = "direct"
    else:
        result["verdict"] = "unknown"

    return result


@app.post("/diagnose/{device}")
@limiter.limit("10/second")
async def post_diagnose(request: Request, device: str, _: bool = Depends(_auth)):
    results: dict[str, Any] = {"device": device, "ts": datetime.now().isoformat()}

    # WG peer (проверяем оба стека)
    # Ищем публичный ключ устройства в БД
    peer_pubkey = ""
    if BOT_DB_PATH.exists():
        try:
            import sqlite3 as _sqlite3
            with _sqlite3.connect(f"file:{BOT_DB_PATH}?mode=ro", uri=True, timeout=3) as conn:
                row = conn.execute(
                    "SELECT d.public_key FROM devices d WHERE LOWER(d.device_name) = LOWER(?) LIMIT 1",
                    (device,),
                ).fetchone()
            if row:
                peer_pubkey = row[0] or ""
        except Exception as exc:
            logger.debug("Не удалось найти peer_pubkey для устройства: %s", exc)

    wg_peer_ok = False
    if peer_pubkey:
        for tool in ("awg", "wg"):
            rc, out, _ = await run_cmd([tool, "show", "all", "latest-handshakes"], timeout=10)
            if rc == 0:
                for line in out.splitlines():
                    parts = line.split()
                    # формат: <iface> <pubkey> <timestamp>
                    if len(parts) >= 3 and parts[1] == peer_pubkey:
                        try:
                            hs_age = int(time.time()) - int(parts[2])
                            wg_peer_ok = hs_age < PEER_STALE_SECONDS
                        except ValueError:
                            pass
                        break
            if wg_peer_ok:
                break
    results["wg_peer_found"] = wg_peer_ok

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


@app.post("/vps/install")
@limiter.limit("5/minute")
async def post_vps_install(request: Request, req: VpsInstallRequest, _: bool = Depends(_auth)):
    """Запустить установку нового VPS через add-vps.sh (background task)."""
    if not re.match(r'^(\d{1,3}\.){3}\d{1,3}$', req.ip):
        raise HTTPException(status_code=400, detail="Invalid IP address")
    if not (1 <= req.ssh_port <= 65535):
        raise HTTPException(status_code=400, detail="Invalid SSH port")
    script = Path("/opt/vpn/add-vps.sh")
    if not script.exists():
        raise HTTPException(status_code=500, detail="add-vps.sh не найден")
    asyncio.create_task(_run_vps_install(req.ip, req.password, req.ssh_port))
    return {"status": "started", "ip": req.ip}


async def _run_vps_install(ip: str, password: str, ssh_port: int) -> None:
    """Запускает add-vps.sh, отправляет прогресс в Telegram."""
    tg.enqueue(f"🚀 <b>Установка VPS {ip} началась...</b>\nЭто займёт 5–10 минут.")
    last_error_lines: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "/opt/vpn/add-vps.sh", ip, password, str(ssh_port),
            env=_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            # Шаги установки — отправляем в Telegram
            if "━━━" in line:
                step = line.replace("━", "").strip()
                tg.enqueue(f"⏳ <b>{step}</b>")
            # Успешные шаги — копим для финального отчёта
            elif "[✓]" in line or "[✗]" in line:
                last_error_lines.append(line)
                if len(last_error_lines) > 10:
                    last_error_lines.pop(0)

        await proc.wait()

        if proc.returncode == 0:
            tg.enqueue(
                f"✅ <b>VPS {ip} установлен успешно!</b>\n\n"
                f"⚠️ Осталось вручную настроить 3x-ui inbounds на VPS:\n"
                f"1. Открыть панель 3x-ui\n"
                f"2. Добавить inbounds: REALITY, gRPC, Hysteria2\n"
                f"3. Скопировать UUID/ключи в плагины watchdog"
            )
        else:
            summary = "\n".join(last_error_lines[-5:]) if last_error_lines else "Нет деталей"
            tg.enqueue(
                f"❌ <b>Установка VPS {ip} провалилась</b> (код {proc.returncode})\n\n"
                f"<code>{summary}</code>\n\n"
                f"Подробности: <code>journalctl -u watchdog -n 50</code> на сервере"
            )
    except Exception as exc:
        logger.error(f"_run_vps_install {ip}: {exc}")
        tg.enqueue(f"❌ <b>Ошибка установки VPS {ip}:</b> {exc}")


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
# NFT sets stats
# ---------------------------------------------------------------------------
@app.get("/nft/stats")
async def get_nft_stats(_: bool = Depends(_auth)):
    """Количество элементов в каждом nft set."""
    sets = {
        "blocked_static":  ("inet", "vpn", "blocked_static"),
        "blocked_dynamic": ("inet", "vpn", "blocked_dynamic"),
        "dpi_direct":      ("inet", "vpn", "dpi_direct"),
    }
    result = {}
    for name, (family, table, set_name) in sets.items():
        rc, out, _ = await run_cmd(
            ["nft", "list", "set", family, table, set_name], timeout=5
        )
        if rc == 0:
            count = out.count(",") + (1 if "elements" in out and "{" in out else 0)
            result[name] = count
        else:
            result[name] = -1
    return result


# ---------------------------------------------------------------------------
# Rotation log
# ---------------------------------------------------------------------------
@app.get("/rotation-log")
async def get_rotation_log(_: bool = Depends(_auth)):
    """История переключений стека (последние 20)."""
    return {"log": list(reversed(state.rotation_log))}


# ---------------------------------------------------------------------------
# DPI test
# ---------------------------------------------------------------------------
class DpiTestRequest(BaseModel):
    domains: Optional[list[str]] = None  # None = тест всех активных сервисов


@app.post("/dpi/test")
@limiter.limit("5/minute")
async def post_dpi_test(request: Request, req: DpiTestRequest, _: bool = Depends(_auth)):
    """Проверить что домены резолвятся в dpi_direct nft set."""
    if not state.dpi_enabled:
        return {"status": "disabled", "results": []}

    # Домены для теста
    test_domains: list[str] = []
    if req.domains:
        test_domains = req.domains
    else:
        for svc in state.dpi_services:
            if svc.get("enabled") and svc.get("domains"):
                test_domains.extend(svc["domains"][:1])  # первый домен каждого сервиса

    results = []
    for domain in test_domains[:10]:
        # 1. Резолвим через dnsmasq
        rc, out, _ = await run_cmd(["dig", "+short", "@127.0.0.1", domain, "A"], timeout=5)
        resolved_ips = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith(";")]

        # 2. Проверяем наличие IP в dpi_direct
        in_set = False
        for ip in resolved_ips:
            rc2, out2, _ = await run_cmd(
                ["nft", "list", "set", "inet", "vpn", "dpi_direct"], timeout=5
            )
            if rc2 == 0 and ip in out2:
                in_set = True
                break

        results.append({
            "domain":      domain,
            "resolved":    resolved_ips[:3],
            "in_dpi_set":  in_set,
            "ok":          in_set,
        })

    all_ok = all(r["ok"] for r in results) if results else False
    return {"status": "ok" if all_ok else "partial", "results": results}


# ---------------------------------------------------------------------------
# zapret on-demand probe + history
# ---------------------------------------------------------------------------
ZAPRET_HISTORY_FILE = PLUGINS_DIR / "zapret" / "preset_history.log"
_probe_lock = asyncio.Lock()


class ZapretProbeRequest(BaseModel):
    mode: str = "quick"  # "quick" | "full"


@app.post("/zapret/probe")
@limiter.limit("3/hour")
async def post_zapret_probe(request: Request, req: ZapretProbeRequest, bg: BackgroundTasks, _: bool = Depends(_auth)):
    """Запустить on-demand probe zapret (quick или full) в фоне."""
    if req.mode not in ("quick", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'quick' or 'full'")
    probe_script = PLUGINS_DIR / "zapret" / "probe.py"
    if not probe_script.exists():
        raise HTTPException(status_code=503, detail="zapret plugin не установлен")
    if _probe_lock.locked():
        raise HTTPException(status_code=409, detail="Probe already running")

    async def _run_probe():
        async with _probe_lock:
            tg.enqueue(f"🔍 zapret: запущен {req.mode} probe...")
            rc, out, err = await run_cmd(
                [sys.executable, str(probe_script), req.mode],
                timeout=600 if req.mode == "full" else 120,
            )
            if rc == 0:
                best = next((l.strip() for l in out.splitlines() if "Лучший пресет:" in l), "")
                msg = f"✅ zapret {req.mode} probe завершён.\n{best}" if best else f"✅ zapret {req.mode} probe завершён."
                tg.enqueue(msg)
            else:
                tg.enqueue(f"⚠️ zapret probe завершился с ошибкой:\n<code>{err.strip()[:300]}</code>")

    bg.add_task(_run_probe)
    return {"status": "started", "mode": req.mode}


@app.get("/zapret/history")
async def get_zapret_history(_: bool = Depends(_auth)):
    """Последние 20 записей истории смен пресета zapret."""
    if not ZAPRET_HISTORY_FILE.exists():
        return {"history": []}
    lines = [l for l in ZAPRET_HISTORY_FILE.read_text().splitlines() if l.strip()]
    return {"history": list(reversed(lines[-20:]))}


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
@app.post("/backup")
@limiter.limit("2/minute")
async def post_backup(request: Request, bg: BackgroundTasks, _: bool = Depends(_auth)):
    """Запустить backup.sh в фоне. Скрипт сам отправляет архив в Telegram."""
    async def _run_backup():
        rc, out, err = await run_cmd(["/opt/vpn/scripts/backup.sh"], timeout=120)
        if rc != 0:
            tg.enqueue(f"⚠️ Backup завершился с ошибкой (rc={rc}):\n<code>{err[:400]}</code>")

    bg.add_task(_run_backup)
    return {"status": "started"}


@app.post("/backup/export")
@limiter.limit("1/minute")
async def post_backup_export(request: Request, bg: BackgroundTasks, _: bool = Depends(_auth)):
    """Запустить backup.sh --full-export в фоне."""
    async def _run_export():
        rc, out, err = await run_cmd(
            ["/opt/vpn/scripts/backup.sh", "--full-export"],
            timeout=180,
        )
        msg = (
            "✓ Полный экспорт создан и отправлен"
            if rc == 0
            else f"✗ Ошибка экспорта (код {rc}): {err[:200]}"
        )
        tg.enqueue(msg)

    bg.add_task(_run_export)
    return {"status": "started", "message": "Full export запущен (~30–60 сек)"}


# ---------------------------------------------------------------------------
# mTLS renew
# ---------------------------------------------------------------------------
@app.post("/renew-cert")
@limiter.limit("3/minute")
async def post_renew_cert(request: Request, _: bool = Depends(_auth)):
    """Обновить клиентский сертификат mTLS."""
    rc, out, err = await run_cmd(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "client"], timeout=60
    )
    return {"ok": rc == 0, "output": (out or err)[:500]}


@app.post("/renew-ca")
@limiter.limit("1/minute")
async def post_renew_ca(request: Request, _: bool = Depends(_auth)):
    """Обновить CA (корневой сертификат)."""
    rc, out, err = await run_cmd(
        ["bash", "/opt/vpn/scripts/renew-mtls.sh", "ca"], timeout=60
    )
    return {"ok": rc == 0, "output": (out or err)[:500]}


# ---------------------------------------------------------------------------
# Fail2ban
# ---------------------------------------------------------------------------
async def _f2b_jails(ssh_prefix: list[str], use_sudo: bool = False) -> list[dict]:
    """Получить список jails и забаненных IP. ssh_prefix=[] для localhost."""
    sudo = ["sudo"] if use_sudo else []
    rc, out, _ = await run_cmd(ssh_prefix + sudo + ["fail2ban-client", "status"], timeout=15)
    if rc != 0:
        return []
    jails = []
    for line in out.splitlines():
        if "Jail list" in line or "список" in line.lower():
            # Jail list:   sshd, xray
            parts = line.split(":", 1)
            if len(parts) == 2:
                jails = [j.strip() for j in parts[1].split(",") if j.strip()]
    result = []
    for jail in jails:
        rc2, out2, _ = await run_cmd(
            ssh_prefix + sudo + ["fail2ban-client", "status", jail], timeout=15
        )
        banned: list[str] = []
        total_banned = 0
        if rc2 == 0:
            for line in out2.splitlines():
                if "Banned IP list" in line or "Список забаненных" in line.lower():
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        banned = [ip.strip() for ip in parts[1].split() if ip.strip()]
                if "Currently banned" in line or "Заблокировано" in line.lower():
                    try:
                        total_banned = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
        result.append({"jail": jail, "banned": banned, "total_banned": total_banned})
    return result


def _vps_ssh_prefix(vps: dict) -> list[str]:
    ssh_key = os.getenv("VPS_SSH_KEY", "/root/.ssh/vpn_id_ed25519")
    ssh_user = os.getenv("BACKUP_VPS_USER", "sysadmin")
    # VPS_SSH_PORT из .env имеет приоритет над ssh_port из state (который хранит внешний порт)
    ssh_port = os.getenv("VPS_SSH_PORT", str(vps.get("ssh_port", 22)))
    cmd = [
        "ssh", "-i", ssh_key,
        "-p", ssh_port,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
    ]
    # SOCKS5-прокси через активный Xray-клиент (порт задаётся в VPS_SSH_PROXY)
    proxy = os.getenv("VPS_SSH_PROXY", "")  # socks5://127.0.0.1:1081
    if proxy:
        proxy_addr = proxy.replace("socks5://", "").replace("socks4://", "")
        cmd += ["-o", f"ProxyCommand=nc -X 5 -x {proxy_addr} %h %p"]
    cmd.append(f"{ssh_user}@{vps['ip']}")
    return cmd


@app.get("/fail2ban/status")
async def get_fail2ban_status(_: bool = Depends(_auth)):
    """Статус fail2ban: домашний сервер + все VPS."""
    home_jails = await _f2b_jails([])
    vps_results = []
    for i, vps in enumerate(state.vps_list):
        prefix = _vps_ssh_prefix(vps)
        jails = await _f2b_jails(prefix, use_sudo=True)
        vps_results.append({"ip": vps["ip"], "idx": i, "jails": jails})
    # Добавить первичный VPS если его нет в списке
    if VPS_IP and not any(v["ip"] == VPS_IP for v in state.vps_list):
        ssh_port = int(os.getenv("VPS_SSH_PORT", "443"))
        prefix = _vps_ssh_prefix({"ip": VPS_IP, "ssh_port": ssh_port})
        jails = await _f2b_jails(prefix, use_sudo=True)
        vps_results.insert(0, {"ip": VPS_IP, "idx": -1, "jails": jails})
    return {"home": home_jails, "vps": vps_results}


class F2bUnbanRequest(BaseModel):
    server: str          # "home" или IP VPS
    jail: str
    ip: str


@app.post("/fail2ban/unban")
@limiter.limit("20/minute")
async def post_fail2ban_unban(request: Request, req: F2bUnbanRequest, _: bool = Depends(_auth)):
    """Разбанить IP в указанном jail."""
    if req.server == "home":
        ssh_prefix: list[str] = []
    else:
        # Найти VPS по IP
        vps = next((v for v in state.vps_list if v["ip"] == req.server), None)
        if not vps and req.server == VPS_IP:
            vps = {"ip": VPS_IP, "ssh_port": int(os.getenv("VPS_SSH_PORT", "443"))}
        if not vps:
            raise HTTPException(status_code=404, detail="VPS не найден")
        ssh_prefix = _vps_ssh_prefix(vps)
    sudo = [] if req.server == "home" else ["sudo"]
    rc, out, err = await run_cmd(
        ssh_prefix + sudo + ["fail2ban-client", "set", req.jail, "unbanip", req.ip],
        timeout=15,
    )
    return {"ok": rc == 0, "output": (out or err)[:200]}


# ---------------------------------------------------------------------------
# Startup / Shutdown / Signals
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tier-2 proxy — стабильный SOCKS5 порт для autossh-tier2
# ---------------------------------------------------------------------------
# Всегда слушает на 127.0.0.1:1089 и форвардит на socks_port активного стека.
# При смене стека старые соединения рвутся (autossh переподключается),
# новые соединения идут через новый стек автоматически.

def _get_active_socks_port() -> int:
    """Возвращает socks_port активного стека из metadata.yaml."""
    plugin = plugins.get(state.active_stack)
    if plugin:
        sp = plugin.meta.get("socks_port")
        if sp:
            return int(sp)
    return 1080  # fallback


async def _tier2_proxy_pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _tier2_proxy_handler(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    socks_port = _get_active_socks_port()
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", socks_port
        )
    except Exception as exc:
        logger.debug(f"tier2-proxy: не удалось подключиться к :{socks_port}: {exc}")
        try:
            client_writer.close()
        except Exception:
            pass
        return
    logger.debug(
        f"tier2-proxy: соединение → 127.0.0.1:{socks_port} (стек: {state.active_stack})"
    )
    await asyncio.gather(
        _tier2_proxy_pipe(client_reader, upstream_writer),
        _tier2_proxy_pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def _run_tier2_proxy() -> None:
    server = await asyncio.start_server(
        _tier2_proxy_handler, "127.0.0.1", TIER2_PROXY_PORT
    )
    logger.info(
        f"Tier-2 proxy запущен на 127.0.0.1:{TIER2_PROXY_PORT} "
        f"→ socks5://127.0.0.1:{_get_active_socks_port()} (стек: {state.active_stack})"
    )
    async with server:
        await server.serve_forever()


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("=" * 60)
    version_label = installed_version_label()
    logger.info("Watchdog%s запускается...", f" {version_label}" if version_label else "")

    # Загружаем плагины
    plugins.load()

    # Загружаем состояние
    state.load()

    # Обновляем state files для ssh-proxy.sh на основе загруженного состояния
    _write_vpn_state_files(state.active_stack)

    # Инициализируем vps_list из .env если список пустой
    if VPS_IP and not any(v["ip"] == VPS_IP for v in state.vps_list):
        state.vps_list.insert(0, {
            "ip": VPS_IP,
            "ssh_port": int(os.getenv("VPS_SSH_PORT", "443")),
            "tunnel_ip": os.getenv("VPS_TUNNEL_IP", "10.177.2.2"),
            "active": True,
        })
        logger.info(f"VPS {VPS_IP} добавлен в vps_list из конфига")

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
            if await _set_marked_route_for_stack(state.active_stack):
                _write_vpn_state_files(state.active_stack)
                logger.info(f"Маршрут table marked восстановлен для стека {state.active_stack}")
            else:
                await _set_marked_route_unreachable()
                active_plugin = None
        else:
            await _set_marked_route_unreachable()

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
        await _dpi_apply_routing()
        zp_restore = plugins.get("zapret")
        if zp_restore:
            await zp_restore.activate()   # добавить NFQUEUE-правила (nfqws уже запущен выше)
        await _dpi_sync_active_domains()
        logger.info("[DPI] DPI bypass восстановлен при старте (NFQUEUE активирован)")

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
    asyncio.create_task(_run_tier2_proxy(),    name="tier2-proxy")
    asyncio.create_task(_startup_reconcile(),  name="startup-reconcile")

    _notify_systemd(b"READY=1")
    logger.info(f"Watchdog готов. Стек: {state.active_stack}, degraded={state.degraded_mode}")
    startup_title = f"✅ *Watchdog {version_label} запущен*" if version_label else "✅ *Watchdog запущен*"
    alert(
        f"{startup_title}\n"
        f"Стек: {state.active_stack}\n"
        f"VPS: {VPS_IP or 'не задан'}"
    )


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

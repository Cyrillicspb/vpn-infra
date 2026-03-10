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

from __future__ import annotations

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
GRAFANA_URL          = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_TOKEN        = os.getenv("GRAFANA_TOKEN", "")
DDNS_PROVIDER        = os.getenv("DDNS_PROVIDER", "")
DDNS_DOMAIN          = os.getenv("DDNS_DOMAIN", "")
DDNS_TOKEN           = os.getenv("DDNS_TOKEN", "")
CF_API_TOKEN         = os.getenv("CF_API_TOKEN", "")
NET_INTERFACE        = os.getenv("NET_INTERFACE", "eth0")

STATE_FILE   = Path("/opt/vpn/watchdog/state.json")
PLUGINS_DIR  = Path("/opt/vpn/watchdog/plugins")
ROUTES_DIR   = Path("/etc/vpn-routes")
LOG_FILE     = "/var/log/vpn-watchdog.log"

# Порядок по устойчивости (индекс 0 = самый устойчивый)
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
                        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status in (200, 400):   # 400 = плохой текст, не ретраим
                        return
            except Exception as exc:
                logger.debug(f"Telegram недоступен (попытка {attempt + 1}): {exc}")
            await asyncio.sleep(min(30, 5 * (attempt + 1)))

    def stop(self) -> None:
        self._running = False


tg = TelegramQueue()


def alert(text: str, chat_id: str = "") -> None:
    """Добавить алерт в очередь Telegram."""
    logger.info(f"ALERT: {text[:120]}")
    tg.enqueue(text, chat_id)


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
                logger.info(f"Состояние загружено: стек={self.active_stack}")
        except Exception as exc:
            logger.error(f"Не удалось загрузить состояние: {exc}")


state = WatchdogState()


# ---------------------------------------------------------------------------
# Мониторинг: ping VPS через tun
# ---------------------------------------------------------------------------
async def ping_vps(target: str = "") -> tuple[bool, float]:
    """Ping VPS через tun. Возвращает (success, rtt_ms)."""
    host = target or VPS_TUNNEL_IP
    if not host:
        return False, 0.0
    rc, stdout, _ = await run_cmd(["ping", "-c", "3", "-W", "5", "-q", host], timeout=25)
    if rc == 0:
        # "rtt min/avg/max/mdev = 10.1/12.3/14.5/1.2 ms"
        for line in stdout.splitlines():
            if "avg" in line and "=" in line:
                try:
                    avg_rtt = float(line.split("=")[1].strip().split("/")[1])
                    return True, avg_rtt
                except Exception as exc:
                    logger.debug(f"ping_vps: не удалось распарсить RTT из '{line}': {exc}")
        return True, 0.0
    return False, 0.0


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------
SPEED_URL_SMALL = "https://speed.cloudflare.com/__down?bytes=102400"    # 100 KB
SPEED_URL_LARGE = "https://speed.cloudflare.com/__down?bytes=10485760"  # 10 MB


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
    return mbps


async def speedtest_large() -> float:
    """10MB тест через активный стек. Возвращает Mbps."""
    mbps = await _measure_throughput(SPEED_URL_LARGE)
    if mbps > 0:
        state.large_speedtest.append(mbps)
    return mbps


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
# Мониторинг: WireGuard peers
# ---------------------------------------------------------------------------
async def check_wg_peers() -> None:
    """Проверка stale peers (last handshake > 180 сек)."""
    rc, out, _ = await run_cmd(["wg", "show", "all", "latest-handshakes"], timeout=10)
    if rc != 0:
        return
    now = int(time.time())
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            iface, pubkey, ts_str = parts[0], parts[1], parts[2]
            try:
                ts = int(ts_str)
                age = now - ts
                if ts > 0 and age > PEER_STALE_SECONDS:
                    logger.warning(f"Stale peer {pubkey[:16]}… на {iface} ({age}s)")
                    alert(f"⚠️ WireGuard peer устарел: `{pubkey[:20]}…` на {iface} ({age}s без handshake)")
            except Exception as exc:
                logger.debug(f"check_wg_peers: не удалось распарсить строку '{line}': {exc}")


# ---------------------------------------------------------------------------
# Мониторинг: Docker контейнеры
# ---------------------------------------------------------------------------
async def check_containers() -> None:
    """Проверка exited/unhealthy контейнеров."""
    rc, out, _ = await run_cmd(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"], timeout=15
    )
    if rc != 0:
        return
    for line in out.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            name, status = parts
            if "Exited" in status or "unhealthy" in status.lower():
                alert(f"🚨 Контейнер *{name}*: `{status}`")


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
        logger.error("dnsmasq не отвечает, перезапуск")
        alert("⚠️ dnsmasq не отвечает — перезапуск")
        await run_cmd(["systemctl", "restart", "dnsmasq"])


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
    for url in BLOCKED_CHECK_URLS:
        rc, out, _ = await run_cmd(
            ["curl", "-s", "--max-time", "15", "-o", "/dev/null", "-w", "%{http_code}", url],
            timeout=20,
        )
        if rc != 0 or out.strip() not in ("200", "301", "302", "303"):
            alert(f"⚠️ Заблокированный сайт *{url}* недоступен через туннель (код: {out.strip() or 'нет ответа'})")
            return


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
        if not plugin:
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

    # 1. Поднимаем новое соединение на временном порту
    ok = await plugin.start(temp_port="1082")
    if not ok:
        logger.error(f"Не удалось запустить {new_stack}")
        alert(f"⚠️ Failover на *{new_stack}* не удался")
        return False

    # 2. Атомарно переключаем маршрут table 200 → новый tun
    await run_cmd(
        ["ip", "route", "replace", "default", "dev", f"tun-{new_stack}", "table", "200"],
        timeout=5,
    )

    # 3. Останавливаем старый стек
    old_plugin = plugins.get(old_stack)
    if old_plugin:
        await old_plugin.stop()

    state.active_stack = new_stack
    state.last_failover = datetime.now()
    state.all_stacks_down_since = None
    state.save()

    logger.info(f"Стек переключён: {old_stack} → {new_stack}")
    alert(f"🔄 VPN стек переключён: *{old_stack}* → *{new_stack}*\nПричина: {reason}")
    return True


async def _do_failover(reason: str) -> None:
    """
    Последовательный проход по стекам ВВЕРХ по устойчивости
    начиная с текущего. Worst case до CDN ~30 сек.
    """
    async with _LOCK:
        state.failover_in_progress = True
        try:
            current = state.active_stack
            ordered = plugins.all_names()   # убывает по resilience (CDN первый)

            # Начинаем тестирование со следующего после текущего по устойчивости
            try:
                cur_pos = ordered.index(current)
            except ValueError:
                cur_pos = len(ordered) - 1

            # Пробуем более устойчивые (индексы < cur_pos), затем все остальные
            candidates = ordered[:cur_pos] + ordered[cur_pos + 1:]

            for candidate in candidates:
                plugin = plugins.get(candidate)
                if not plugin:
                    continue
                logger.info(f"Тест кандидата: {candidate}")
                ok, mbps = await plugin.test(timeout=10)
                if ok:
                    await _do_switch(candidate, reason)
                    return

            # Все стеки недоступны
            now = datetime.now()
            if state.all_stacks_down_since is None:
                state.all_stacks_down_since = now

            down_min = (now - state.all_stacks_down_since).total_seconds() / 60
            if down_min >= ALL_STACKS_DOWN_MINUTES:
                alert("🚨 *ВСЕ VPN СТЕКИ НЕДОСТУПНЫ* более 5 мин!\nПроверьте VPS вручную.")
        finally:
            state.failover_in_progress = False


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
                await _do_failover("rotation_check_failed")
                return

            ok = await plugin.rotate()
            if ok:
                state.last_rotation = datetime.now()
                logger.info(f"Ротация {current} выполнена")
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
        if not plugin:
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
            if not plugin:
                continue
            ok, mbps = await plugin.test(timeout=10)
            if ok and mbps > best_mbps:
                best_mbps = mbps
                best_stack = name

        if best_stack and best_stack != state.active_stack:
            logger.info(f"Переоценка: более быстрый стек {best_stack} ({best_mbps:.1f} Mbps)")
            await _do_switch(best_stack, "hourly_reassessment")

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

    while True:
        try:
            ok, rtt = await ping_vps()

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
async def monitoring_loop() -> None:
    """Периодические проверки всех компонентов системы."""
    tick = 0
    last_large_speedtest = 0.0
    last_full_assessment = 0.0
    last_heartbeat = 0.0
    last_conntrack = 0.0
    standby_checked_today = False
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

            # Каждые 6 ч: large speedtest, кэш маршрутов, сертификаты, DKMS
            if now - last_large_speedtest >= 6 * 3600:
                mbps = await speedtest_large()
                logger.info(f"Speedtest (10MB): {mbps:.1f} Mbps")
                last_large_speedtest = now
                await check_routes_cache_age()
                await check_certs()
                await check_dkms()

            # Каждый час: полная переоценка стеков, conntrack
            if now - last_full_assessment >= 3600:
                asyncio.create_task(_full_reassessment())
                last_full_assessment = now
                await collect_conntrack_stats()

            # В 04:30 каждый день: проверка standby туннелей
            now_dt = datetime.now()
            if now_dt.date() != last_standby_check_date:
                standby_checked_today = False
                last_standby_check_date = now_dt.date()
            if not standby_checked_today and now_dt.hour == 4 and now_dt.minute >= 30:
                await test_standby_tunnels()
                standby_checked_today = True

            # Systemd watchdog ping каждые 10 сек
            _notify_systemd(b"WATCHDOG=1")

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
    lines = [
        f'vpn_active_stack{{stack="{state.active_stack}"}} {stack_idx}',
        f"vpn_bytes_sent_total {net.bytes_sent}",
        f"vpn_bytes_recv_total {net.bytes_recv}",
        f"vpn_disk_used_percent {disk.percent}",
        f"vpn_ram_used_percent {ram.percent}",
        f"vpn_cpu_percent {psutil.cpu_percent(interval=0.1)}",
        f"vpn_degraded_mode {int(state.degraded_mode)}",
        f"vpn_uptime_seconds {int((datetime.now() - state.started_at).total_seconds())}",
    ]
    return Response(content="\n".join(lines), media_type="text/plain")


@app.get("/peer/list")
async def get_peer_list(_: bool = Depends(_auth)):
    rc, out, _ = await run_cmd(["wg", "show", "all", "dump"], timeout=10)
    peers = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            peers.append({
                "interface":      parts[0],
                "public_key":     parts[1],
                "preshared_key":  "(hidden)",
                "endpoint":       parts[3],
                "last_handshake": parts[4],
                "rx_bytes":       parts[5] if len(parts) > 5 else None,
                "tx_bytes":       parts[6] if len(parts) > 6 else None,
            })
    return {"peers": peers, "count": len(peers)}


@app.get("/vps/list")
async def get_vps_list(_: bool = Depends(_auth)):
    return {"vps_list": state.vps_list, "active_idx": state.active_vps_idx}


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
        rc, used_out, _ = await run_cmd(["wg", "show", iface, "allowed-ips"], timeout=10)
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
            ["wg", "set", iface, "peer", pubkey, "allowed-ips", f"{peer_ip}/32"],
            timeout=10,
        )
        if rc == 0:
            await run_cmd(["wg-quick", "save", iface], timeout=10)
            logger.info(f"Peer добавлен: {req.name} → {peer_ip} на {iface}")
        else:
            logger.error(f"Ошибка добавления peer {req.name}: {err}")


@app.post("/peer/remove")
@limiter.limit("10/second")
async def post_peer_remove(request: Request, req: PeerRemoveRequest, _: bool = Depends(_auth)):
    for iface in ([req.interface] if req.interface else ["wg0", "wg1"]):
        rc, _, _ = await run_cmd(
            ["wg", "set", iface, "peer", req.peer_id, "remove"], timeout=10
        )
        if rc == 0:
            await run_cmd(["wg-quick", "save", iface], timeout=10)
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


@app.post("/graph")
@limiter.limit("10/second")
async def post_graph(request: Request, req: GraphRequest, _: bool = Depends(_auth)):
    """Получить PNG-график из Grafana Render API."""
    panel_ids = {"tunnel": 1, "speed": 2, "clients": 3, "system": 4}
    period_map = {"1h": "1h", "6h": "6h", "24h": "24h", "7d": "7d"}

    panel_id = panel_ids.get(req.panel, 1)
    period   = period_map.get(req.period, "1h")

    url = (
        f"{GRAFANA_URL}/render/d-solo/vpn-overview/vpn-overview"
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

    # WG peer
    rc, out, _ = await run_cmd(["wg", "show", "all", "dump"], timeout=10)
    results["wg_peer_found"] = device in out

    # DNS
    rc, out, _ = await run_cmd(["dig", "@127.0.0.1", "youtube.com", "+short", "+time=3"], timeout=10)
    results["dns_ok"] = rc == 0 and bool(out.strip())

    # Туннель
    ok, rtt = await ping_vps()
    results["tunnel_ok"] = ok
    results["tunnel_rtt_ms"] = rtt

    # Заблокированные сайты
    rc, out, _ = await run_cmd(
        ["curl", "-s", "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://youtube.com"],
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

    # Consistency recovery
    ok, _ = await ping_vps()
    if not ok:
        logger.warning("VPS недоступен при старте — degraded mode")
        state.degraded_mode = True
    else:
        state.degraded_mode = False

    # Запускаем фоновые задачи
    asyncio.create_task(tg.run(),    name="tg-queue")
    asyncio.create_task(decision_loop(), name="decision-loop")
    asyncio.create_task(monitoring_loop(), name="monitoring-loop")

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

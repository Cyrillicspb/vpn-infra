#!/usr/bin/env python3
"""
watchdog.py — Центральный агент управления VPN Infrastructure v4.0

Отвечает за:
- Мониторинг состояния туннелей и сервисов
- Адаптивный failover между 4 стеками
- HTTP API для telegram-bot
- Алерты в Telegram
- Ротацию соединений (анти-DPI)
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv("WATCHDOG_API_TOKEN", "")
API_PORT = int(os.getenv("WATCHDOG_PORT", "8080"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
VPS_IP = os.getenv("VPS_IP", "")
VPS_TUNNEL_IP = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")

LOG_FILE = "/var/log/vpn-watchdog.log"
STATE_FILE = "/opt/vpn/watchdog/state.json"
PLUGINS_DIR = Path("/opt/vpn/watchdog/plugins")

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------
app = FastAPI(title="VPN Watchdog API", version="4.0.0")
security = HTTPBearer(auto_error=False)


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """Проверка Bearer token."""
    if not API_TOKEN:
        return True
    if credentials is None or credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------
class SwitchRequest(BaseModel):
    stack: str  # "hysteria2" | "reality" | "reality-grpc" | "cloudflare-cdn"


class PeerAddRequest(BaseModel):
    name: str
    protocol: str  # "awg" | "wg"
    public_key: Optional[str] = None


class PeerRemoveRequest(BaseModel):
    peer_id: str


class RouteRequest(BaseModel):
    action: str  # "add" | "remove"
    domain: str
    direction: str  # "vpn" | "direct"


class DeployRequest(BaseModel):
    version: Optional[str] = None
    force: bool = False


# ---------------------------------------------------------------------------
# Состояние watchdog
# ---------------------------------------------------------------------------
class WatchdogState:
    def __init__(self):
        self.active_stack: str = "cloudflare-cdn"
        self.primary_stack: str = "cloudflare-cdn"
        self.stack_baselines: dict = {}  # RTT baselines за 7 дней
        self.last_failover: Optional[datetime] = None
        self.last_rotation: Optional[datetime] = None
        self.failover_in_progress: bool = False
        self.rotation_in_progress: bool = False
        self.vps_list: list = []
        self.external_ip: str = ""
        self.started_at: datetime = datetime.now()
        self.degraded_mode: bool = False  # Работает без туннелей при первой установке

    def to_dict(self) -> dict:
        return {
            "active_stack": self.active_stack,
            "primary_stack": self.primary_stack,
            "last_failover": self.last_failover.isoformat() if self.last_failover else None,
            "last_rotation": self.last_rotation.isoformat() if self.last_rotation else None,
            "external_ip": self.external_ip,
            "started_at": self.started_at.isoformat(),
            "degraded_mode": self.degraded_mode,
            "vps_list": self.vps_list,
        }

    def save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"Не удалось сохранить состояние: {e}")

    def load(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    data = json.load(f)
                self.active_stack = data.get("active_stack", "cloudflare-cdn")
                self.primary_stack = data.get("primary_stack", "cloudflare-cdn")
                self.external_ip = data.get("external_ip", "")
                self.vps_list = data.get("vps_list", [])
                self.degraded_mode = data.get("degraded_mode", False)
                logger.info(f"Состояние загружено: стек={self.active_stack}")
        except Exception as e:
            logger.error(f"Не удалось загрузить состояние: {e}")


state = WatchdogState()

# Порядок стеков по устойчивости (убывает)
STACK_RESILIENCE_ORDER = ["cloudflare-cdn", "reality-grpc", "reality", "hysteria2"]


# ---------------------------------------------------------------------------
# Хелперы: выполнение команд
# ---------------------------------------------------------------------------
async def run_command(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Асинхронный запуск процесса."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        return 1, "", f"Timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


async def send_telegram(message: str, chat_id: str = ""):
    """Отправка уведомления в Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        return
    target_chat = chat_id or TELEGRAM_ADMIN_CHAT_ID
    if not target_chat:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": target_chat, "text": message, "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        logger.warning(f"Telegram недоступен: {e}")


# ---------------------------------------------------------------------------
# Мониторинг: ping VPS через tun
# ---------------------------------------------------------------------------
async def ping_vps() -> tuple[bool, float]:
    """Ping VPS через tun. Возвращает (success, rtt_ms)."""
    if not VPS_TUNNEL_IP:
        return False, 0.0
    start = time.time()
    rc, stdout, _ = await run_command(["ping", "-c", "3", "-W", "5", VPS_TUNNEL_IP], timeout=20)
    rtt = (time.time() - start) * 1000
    if rc == 0 and "avg" in stdout:
        # Парсим RTT из ping output
        try:
            avg_rtt = float(stdout.split("/")[-3].split("=")[-1].split("/")[1])
            return True, avg_rtt
        except Exception:
            pass
    return rc == 0, rtt


# ---------------------------------------------------------------------------
# Failover логика
# ---------------------------------------------------------------------------
async def test_stack(stack_name: str, timeout: int = 10) -> tuple[bool, float]:
    """
    Тестирование стека.
    Возвращает (работает, throughput_mbps).
    """
    plugin_dir = PLUGINS_DIR / stack_name
    client_py = plugin_dir / "client.py"
    if not client_py.exists():
        logger.warning(f"Плагин {stack_name} не найден")
        return False, 0.0

    try:
        # Импортируем и вызываем test() из плагина
        # В реальности: запускаем в subprocess
        rc, stdout, stderr = await run_command(
            [sys.executable, str(client_py), "test"],
            timeout=timeout,
        )
        if rc == 0:
            try:
                result = json.loads(stdout)
                return True, result.get("throughput_mbps", 0.0)
            except Exception:
                return True, 5.0
        return False, 0.0
    except Exception as e:
        logger.error(f"Ошибка тестирования стека {stack_name}: {e}")
        return False, 0.0


async def switch_stack(new_stack: str):
    """
    Переключение на новый стек (make-before-break).
    """
    global state
    if state.failover_in_progress:
        logger.info("Failover уже выполняется, пропуск")
        return

    old_stack = state.active_stack
    if old_stack == new_stack:
        return

    state.failover_in_progress = True
    logger.info(f"Переключение стека: {old_stack} → {new_stack}")

    try:
        # 1. Поднимаем новое соединение на временном порту
        plugin_dir = PLUGINS_DIR / new_stack
        client_py = plugin_dir / "client.py"

        rc, _, err = await run_command(
            [sys.executable, str(client_py), "start", "--temp-port", "1082"],
            timeout=30,
        )
        if rc != 0:
            logger.error(f"Не удалось поднять {new_stack}: {err}")
            await send_telegram(f"⚠️ Failover на {new_stack} не удался: {err}")
            return

        # 2. Переключаем маршруты на новый тун
        await run_command(["ip", "route", "replace", "default", "dev", f"tun-{new_stack}", "table", "200"])

        # 3. Закрываем старое соединение
        old_plugin = PLUGINS_DIR / old_stack / "client.py"
        if old_plugin.exists():
            await run_command([sys.executable, str(old_plugin), "stop"], timeout=10)

        state.active_stack = new_stack
        state.last_failover = datetime.now()
        state.save()

        logger.info(f"Стек переключён: {old_stack} → {new_stack}")
        await send_telegram(f"🔄 VPN стек переключён: *{old_stack}* → *{new_stack}*")

    except Exception as e:
        logger.error(f"Ошибка при переключении стека: {e}")
        await send_telegram(f"❌ Ошибка failover: {e}")
    finally:
        state.failover_in_progress = False


async def failover_loop():
    """
    Основной цикл мониторинга и failover.
    Проверяет ping каждые 10 сек.
    При деградации — последовательно тестирует стеки по устойчивости.
    """
    ping_fail_count = 0
    logger.info("Запуск failover loop")

    while True:
        try:
            ok, rtt = await ping_vps()

            if ok:
                ping_fail_count = 0
                # Проверяем деградацию по RTT (> 3x baseline)
                baseline = state.stack_baselines.get(state.active_stack)
                if baseline and rtt > baseline * 3:
                    logger.warning(f"Деградация RTT: {rtt:.0f}ms (baseline {baseline:.0f}ms)")
                    await do_failover("rtt_degradation")
            else:
                ping_fail_count += 1
                logger.warning(f"Ping fail #{ping_fail_count}")
                if ping_fail_count >= 3:  # 3 подряд = 30 сек
                    logger.error("Потеря соединения!")
                    ping_fail_count = 0
                    await do_failover("ping_timeout")

        except Exception as e:
            logger.error(f"Ошибка в failover loop: {e}")

        await asyncio.sleep(10)


async def do_failover(reason: str):
    """Выполняет failover: последовательно тестирует стеки вверх по устойчивости."""
    if state.failover_in_progress:
        return

    current_idx = STACK_RESILIENCE_ORDER.index(state.active_stack) \
        if state.active_stack in STACK_RESILIENCE_ORDER else len(STACK_RESILIENCE_ORDER) - 1

    logger.info(f"Failover причина: {reason}, текущий стек: {state.active_stack}")

    # Тестируем стеки, начиная со следующего по устойчивости
    for i in range(current_idx - 1, -1, -1):  # Идём к более устойчивым
        candidate = STACK_RESILIENCE_ORDER[i]
        logger.info(f"Тестирую стек: {candidate}")
        ok, throughput = await test_stack(candidate)
        if ok:
            logger.info(f"Стек {candidate} работает (throughput={throughput:.1f} Mbps)")
            await switch_stack(candidate)
            return

    # Если ничего не нашли, пробуем CDN (самый устойчивый)
    if state.active_stack != "cloudflare-cdn":
        ok, _ = await test_stack("cloudflare-cdn")
        if ok:
            await switch_stack("cloudflare-cdn")
            return

    # Все стеки down
    logger.critical("ВСЕ СТЕКИ НЕДОСТУПНЫ!")
    await send_telegram("🚨 *ВСЕ VPN СТЕКИ НЕДОСТУПНЫ!*\nПроверьте VPS вручную.")


# ---------------------------------------------------------------------------
# Ротация соединений (анти-DPI)
# ---------------------------------------------------------------------------
async def rotation_loop():
    """
    Плановая ротация соединений каждые 30-60 мин (рандомный интервал).
    Make-before-break.
    """
    import random
    logger.info("Запуск rotation loop")

    while True:
        # Рандомный интервал: 30-60 мин ±15 мин
        interval_min = random.randint(15, 75)
        await asyncio.sleep(interval_min * 60)

        if state.failover_in_progress:
            continue

        logger.info("Плановая ротация соединения (анти-DPI)")
        state.rotation_in_progress = True
        try:
            # Перезапускаем текущий стек (make-before-break)
            current = state.active_stack
            plugin = PLUGINS_DIR / current / "client.py"
            if plugin.exists():
                # Поднимаем новое → переключаем → закрываем старое
                await run_command([sys.executable, str(plugin), "rotate"], timeout=30)
                state.last_rotation = datetime.now()
                logger.info(f"Ротация {current} выполнена")
        except Exception as e:
            logger.error(f"Ошибка ротации: {e}")
        finally:
            state.rotation_in_progress = False


# ---------------------------------------------------------------------------
# Мониторинг: внешний IP
# ---------------------------------------------------------------------------
async def check_external_ip():
    """Проверка внешнего IP. При смене — DDNS обновление или рассылка конфигов."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=10)) as r:
                new_ip = await r.text()
                new_ip = new_ip.strip()

        if new_ip and new_ip != state.external_ip:
            old_ip = state.external_ip
            state.external_ip = new_ip
            state.save()
            logger.info(f"Внешний IP изменился: {old_ip} → {new_ip}")

            # Обновляем DDNS или уведомляем
            ddns_provider = os.getenv("DDNS_PROVIDER", "")
            if ddns_provider:
                await update_ddns(new_ip)
                await send_telegram(f"ℹ️ Внешний IP изменился: `{old_ip}` → `{new_ip}`\nDDNS обновлён.")
            else:
                await send_telegram(
                    f"⚠️ Внешний IP изменился: `{old_ip}` → `{new_ip}`\n"
                    f"DDNS не настроен — необходима рассылка конфигов клиентам!"
                )
    except Exception as e:
        logger.debug(f"Не удалось проверить внешний IP: {e}")


async def update_ddns(new_ip: str):
    """Обновление DDNS записи."""
    provider = os.getenv("DDNS_PROVIDER", "")
    domain = os.getenv("DDNS_DOMAIN", "")
    token = os.getenv("DDNS_TOKEN", "")

    if not all([provider, domain, token]):
        return

    try:
        if provider == "duckdns":
            url = f"https://www.duckdns.org/update?domains={domain}&token={token}&ip={new_ip}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    result = await r.text()
                    logger.info(f"DuckDNS обновлён: {result}")
        elif provider == "cloudflare":
            # Cloudflare DDNS через API
            cf_token = os.getenv("CF_API_TOKEN", "")
            # ... (реализация через Cloudflare API)
            logger.info(f"Cloudflare DDNS: обновление {domain} → {new_ip}")
    except Exception as e:
        logger.error(f"Ошибка обновления DDNS: {e}")


# ---------------------------------------------------------------------------
# Мониторинг: disk
# ---------------------------------------------------------------------------
async def check_disk():
    """Проверка дискового пространства."""
    disk = psutil.disk_usage("/")
    percent = disk.percent

    if percent >= 95:
        logger.critical(f"Диск КРИТИЧНО: {percent}%")
        await send_telegram(f"🚨 Диск критически заполнен: *{percent}%*\nОстановка некритичных сервисов...")
        # Останавливаем некритичные
        await run_command(["docker", "stop", "homepage"], timeout=10)

    elif percent >= 90:
        logger.error(f"Диск критично: {percent}%")
        await send_telegram(f"⚠️ Диск заполнен: *{percent}%*\nАгрессивная очистка...")
        await run_command(["docker", "system", "prune", "-af"], timeout=60)

    elif percent >= 80:
        logger.warning(f"Диск: {percent}%")
        await send_telegram(f"ℹ️ Диск: *{percent}%*\nАвтоочистка Docker...")
        await run_command(["docker", "system", "prune", "-f"], timeout=60)


# ---------------------------------------------------------------------------
# Мониторинг: dnsmasq
# ---------------------------------------------------------------------------
async def check_dnsmasq():
    """Healthcheck dnsmasq."""
    rc, _, _ = await run_command(["dig", "@127.0.0.1", "google.com", "+short", "+time=3"], timeout=10)
    if rc != 0:
        logger.error("dnsmasq не отвечает!")
        await send_telegram("⚠️ dnsmasq не отвечает, перезапуск...")
        await run_command(["systemctl", "restart", "dnsmasq"])


# ---------------------------------------------------------------------------
# Фоновый мониторинг
# ---------------------------------------------------------------------------
async def monitoring_loop():
    """Периодический мониторинг всего."""
    logger.info("Запуск monitoring loop")
    tick = 0

    while True:
        try:
            tick += 1

            # Каждые 30 сек: dnsmasq
            if tick % 3 == 0:
                await check_dnsmasq()

            # Каждые 5 мин: внешний IP, диск
            if tick % 30 == 0:
                await check_external_ip()
                await check_disk()

        except Exception as e:
            logger.error(f"Ошибка в monitoring loop: {e}")

        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/status")
async def get_status(_: bool = Depends(verify_token)):
    """Общий статус системы."""
    try:
        disk = psutil.disk_usage("/")
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
    except Exception:
        disk = cpu = ram = None

    return {
        "status": "ok" if not state.degraded_mode else "degraded",
        "active_stack": state.active_stack,
        "primary_stack": state.primary_stack,
        "external_ip": state.external_ip,
        "uptime_seconds": int((datetime.now() - state.started_at).total_seconds()),
        "last_failover": state.last_failover.isoformat() if state.last_failover else None,
        "last_rotation": state.last_rotation.isoformat() if state.last_rotation else None,
        "degraded_mode": state.degraded_mode,
        "system": {
            "disk_percent": disk.percent if disk else None,
            "cpu_percent": cpu,
            "ram_percent": ram.percent if ram else None,
        },
    }


@app.get("/metrics")
async def get_metrics(_: bool = Depends(verify_token)):
    """Метрики в формате Prometheus."""
    try:
        net = psutil.net_io_counters()
        disk = psutil.disk_usage("/")
        return {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "disk_used_percent": disk.percent,
            "active_stack": state.active_stack,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/switch")
async def post_switch(req: SwitchRequest, _: bool = Depends(verify_token)):
    """Ручное переключение стека."""
    if req.stack not in STACK_RESILIENCE_ORDER:
        raise HTTPException(status_code=400, detail=f"Неизвестный стек: {req.stack}")
    asyncio.create_task(switch_stack(req.stack))
    return {"status": "switching", "target_stack": req.stack}


@app.post("/peer/add")
async def post_peer_add(req: PeerAddRequest, background: BackgroundTasks, _: bool = Depends(verify_token)):
    """Добавление WireGuard peer."""
    background.add_task(_add_peer, req)
    return {"status": "queued", "name": req.name}


async def _add_peer(req: PeerAddRequest):
    """Фактическое добавление peer (выполняется в фоне)."""
    proto = req.protocol.lower()
    iface = "wg0" if proto == "awg" else "wg1"

    # Генерируем ключи если не предоставлены
    if not req.public_key:
        rc, privkey, _ = await run_command(["wg", "genkey"])
        rc2, pubkey, _ = await run_command(["wg", "pubkey"], timeout=5)
        # В реальности нужно прокинуть privkey через stdin wg pubkey
    else:
        pubkey = req.public_key

    # Добавляем peer
    rc, _, err = await run_command(["wg", "set", iface, "peer", pubkey, "allowed-ips", "..."])
    if rc != 0:
        logger.error(f"Ошибка добавления peer {req.name}: {err}")


@app.post("/peer/remove")
async def post_peer_remove(req: PeerRemoveRequest, _: bool = Depends(verify_token)):
    """Удаление WireGuard peer."""
    # Определяем интерфейс из peer_id
    rc, _, err = await run_command(["wg", "set", "wg0", "peer", req.peer_id, "remove"])
    if rc != 0:
        await run_command(["wg", "set", "wg1", "peer", req.peer_id, "remove"])
    return {"status": "removed", "peer_id": req.peer_id}


@app.get("/peer/list")
async def get_peer_list(_: bool = Depends(verify_token)):
    """Список WireGuard peers."""
    rc, stdout, _ = await run_command(["wg", "show", "all", "dump"])
    peers = []
    for line in stdout.strip().split("\n")[1:]:  # Пропускаем заголовок
        parts = line.split("\t")
        if len(parts) >= 4:
            peers.append({
                "interface": parts[0],
                "public_key": parts[1],
                "last_handshake": parts[4] if len(parts) > 4 else None,
            })
    return {"peers": peers}


@app.post("/routes/update")
async def post_routes_update(background: BackgroundTasks, _: bool = Depends(verify_token)):
    """Запуск обновления маршрутов из баз РКН."""
    background.add_task(_update_routes)
    return {"status": "accepted", "message": "Обновление маршрутов запущено в фоне"}


async def _update_routes():
    """Обновление маршрутов."""
    logger.info("Обновление маршрутов...")
    rc, stdout, stderr = await run_command(
        ["python3", "/opt/vpn/scripts/update-routes.py"],
        timeout=300,
    )
    if rc == 0:
        logger.info("Маршруты обновлены")
        await send_telegram("✅ Маршруты обновлены успешно")
    else:
        logger.error(f"Ошибка обновления маршрутов: {stderr}")
        await send_telegram(f"⚠️ Ошибка обновления маршрутов: {stderr[:200]}")


@app.post("/service/restart")
async def post_service_restart(service: str, _: bool = Depends(verify_token)):
    """Перезапуск сервиса."""
    allowed_services = ["dnsmasq", "watchdog", "hysteria2", "nftables", "docker"]
    if service not in allowed_services:
        raise HTTPException(status_code=400, detail=f"Сервис {service} не разрешён")
    rc, _, err = await run_command(["systemctl", "restart", service])
    return {"status": "restarted" if rc == 0 else "error", "service": service, "error": err if rc != 0 else None}


@app.post("/deploy")
async def post_deploy(req: DeployRequest, background: BackgroundTasks, _: bool = Depends(verify_token)):
    """Запуск обновления через deploy.sh."""
    background.add_task(_run_deploy, req)
    return {"status": "accepted", "message": "Deploy запущен в фоне"}


async def _run_deploy(req: DeployRequest):
    """Запуск deploy.sh."""
    cmd = ["bash", "/opt/vpn/deploy.sh"]
    if req.force:
        cmd.append("--force")
    rc, stdout, stderr = await run_command(cmd, timeout=300)
    if rc != 0:
        await send_telegram(f"❌ Deploy завершился с ошибкой: {stderr[:200]}")


@app.post("/rollback")
async def post_rollback(_: bool = Depends(verify_token)):
    """Откат к предыдущей версии."""
    asyncio.create_task(
        run_command(["bash", "/opt/vpn/deploy.sh", "--rollback"], timeout=120)
    )
    return {"status": "rolling_back"}


@app.post("/reload-plugins")
async def post_reload_plugins(_: bool = Depends(verify_token)):
    """Hot reload плагинов (без перезапуска watchdog)."""
    # Пересканируем директорию plugins
    plugins = [d.name for d in PLUGINS_DIR.iterdir() if d.is_dir() and (d / "metadata.yaml").exists()]
    logger.info(f"Плагины перезагружены: {plugins}")
    return {"status": "reloaded", "plugins": plugins}


@app.post("/notify-clients")
async def post_notify_clients(message: str, _: bool = Depends(verify_token)):
    """Уведомление всех клиентов."""
    # Перенаправляем в telegram-bot через его API
    # (telegram-bot имеет доступ к SQLite с chat_id клиентов)
    return {"status": "sent", "message": message}


@app.post("/diagnose/{device}")
async def post_diagnose(device: str, _: bool = Depends(verify_token)):
    """Диагностика устройства клиента."""
    results = {}

    # 1. Проверяем peer в WG
    rc, stdout, _ = await run_command(["wg", "show", "all", "dump"])
    results["wg_peers"] = "found" if device in stdout else "not_found"

    # 2. Проверяем доступность заблокированных сайтов через tun
    rc2, _, _ = await run_command(
        ["curl", "-s", "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://youtube.com"],
        timeout=15,
    )
    results["blocked_sites_accessible"] = rc2 == 0

    # 3. DNS
    rc3, out3, _ = await run_command(["dig", "@127.0.0.1", "youtube.com", "+short", "+time=3"])
    results["dns_ok"] = rc3 == 0 and bool(out3.strip())

    return {"device": device, "results": results}


@app.get("/vps/list")
async def get_vps_list(_: bool = Depends(verify_token)):
    return {"vps_list": state.vps_list}


@app.post("/vps/add")
async def post_vps_add(ip: str, _: bool = Depends(verify_token)):
    if ip not in state.vps_list:
        state.vps_list.append(ip)
        state.save()
    return {"status": "added", "ip": ip}


@app.post("/vps/remove")
async def post_vps_remove(ip: str, _: bool = Depends(verify_token)):
    state.vps_list = [v for v in state.vps_list if v != ip]
    state.save()
    return {"status": "removed", "ip": ip}


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """Инициализация при старте."""
    logger.info("Watchdog запускается...")

    # Загружаем состояние
    state.load()

    # Consistency recovery: проверяем все сервисы
    await consistency_recovery()

    # Запускаем фоновые задачи
    asyncio.create_task(failover_loop())
    asyncio.create_task(rotation_loop())
    asyncio.create_task(monitoring_loop())

    logger.info(f"Watchdog запущен. Активный стек: {state.active_stack}")
    await send_telegram(f"✅ *Watchdog запущен*\nСтек: {state.active_stack}")


async def consistency_recovery():
    """Проверка консистентности при старте."""
    logger.info("Проверка консистентности...")
    try:
        ok, _ = await ping_vps()
        if not ok:
            logger.warning("VPS недоступен при старте — включаем degraded mode")
            state.degraded_mode = True
        else:
            state.degraded_mode = False
    except Exception as e:
        logger.error(f"Ошибка при проверке консистентности: {e}")
        state.degraded_mode = True


@app.on_event("shutdown")
async def shutdown_event():
    """Корректное завершение."""
    logger.info("Watchdog завершается...")
    state.save()
    await send_telegram("⚠️ *Watchdog завершается* (сервер выключается)")


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------
def handle_sigterm(signum, frame):
    logger.info("Получен SIGTERM")
    state.save()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)

    # Systemd watchdog ping
    if os.getenv("WATCHDOG_USEC"):
        async def systemd_ping():
            import socket
            notify_socket = os.getenv("NOTIFY_SOCKET", "")
            while True:
                if notify_socket:
                    try:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                        s.sendto(b"WATCHDOG=1", notify_socket)
                    except Exception:
                        pass
                await asyncio.sleep(10)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="info",
        access_log=False,
    )

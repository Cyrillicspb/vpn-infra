#!/usr/bin/env python3
"""
update-routes.py — Обновление маршрутов из баз РКН/CDN

Запуск:
  python3 update-routes.py             — полное обновление
  python3 update-routes.py --dry-run   — показать результат без применения
  python3 update-routes.py --force     — применить даже без изменений

Источники (10+):
  1.  antifilter.download/list/ip.lst
  2.  antifilter.download/list/subnet.lst
  3.  antifilter.download/list/allyouneed.lst       (14K CIDR агрегированных)
  4.  antifilter.download/list/domains.lst           (1.3M → root domains)
  5.  iplist.opencck.org/?format=text&data=cidr4    (2.7K CIDR)
  6.  iplist.opencck.org/?format=text&data=domains  (26K доменов)
  7.  github.com/zapret-info/z-i dump-00..18.csv    (через SOCKS5, IP + домены)
  8.  github.com/RockBlack-VPN/ip-address/Global    (через SOCKS5, .bat + _domain)
  9.  /etc/vpn-routes/manual-vpn.txt                (ручные IP/домены)
  10. Статические AS-блоки CDN                      (Google, CF, Meta, Akamai)

Выходные файлы:
  /etc/nftables-blocked-static.conf   — атомарное обновление nft set
  /etc/vpn-routes/combined.cidr       — нормализованные CIDR для AllowedIPs (временно ≤12000)
  /etc/dnsmasq.d/vpn-domains.conf     — nftset= + server= для баз РКН
  /etc/dnsmasq.d/vpn-force.conf       — nftset= + server= для manual-vpn.txt

Защита:
  - per-source кэш: при недоступности → предыдущая версия
  - Алерт если кэш > 3 дней
  - Дельта-проверка: если изменений > 50% → не применять
  - Валидация формата и размера каждого источника
  - flock /var/run/vpn-routes.lock (cron обеспечивает)
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Логирование ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Пути ──────────────────────────────────────────────────────────────────────
ROUTES_DIR      = Path("/etc/vpn-routes")
CACHE_DIR       = ROUTES_DIR / "cache"
COMBINED_CIDR   = ROUTES_DIR / "combined.cidr"
HASH_FILE       = ROUTES_DIR / "combined.hash"
NFT_STATIC      = Path("/etc/nftables-blocked-static.conf")
DNSMASQ_DOMAINS = Path("/etc/dnsmasq.d/vpn-domains.conf")
DNSMASQ_FORCE   = Path("/etc/dnsmasq.d/vpn-force.conf")
DNSMASQ_DIRECT  = Path("/etc/dnsmasq.d/vpn-direct.conf")
WATCHDOG_STATE  = Path("/opt/vpn/watchdog/state.json")
MANUAL_VPN      = ROUTES_DIR / "manual-vpn.txt"
MANUAL_DIRECT   = ROUTES_DIR / "manual-direct.txt"

# ── Переменные из окружения ────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
WATCHDOG_TOKEN  = os.getenv("WATCHDOG_API_TOKEN", "")
WATCHDOG_URL    = os.getenv("WATCHDOG_URL", "http://localhost:8080")
VPS_TUNNEL_IP   = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")

# ── Константы ─────────────────────────────────────────────────────────────────
MAX_CIDR_ALLOWED_IPS = 12_000  # Временный безопасный лимит: correctness важнее компактности
ALERT_CACHE_AGE_DAYS = 3      # Алерт если кэш старше N дней
MAX_DELTA_PCT        = 50     # Максимальная дельта изменений (%)
FETCH_TIMEOUT        = 45     # Таймаут загрузки источника (сек)
FETCH_TIMEOUT_ZIP    = 180    # Таймаут для ZIP (ZIP архив ~15 MB)
FETCH_WORKERS        = 6      # Параллельные загрузки
ZAPRET_MAX_LINES     = 500_000  # Лимит строк из всех dump-*.csv
FORBIDDEN_ALLOWED_PREFIXES = frozenset({7, 8})
CONDITIONAL_ALLOWED_PREFIXES = frozenset({9, 10})
EXPANSION_RATIO_OK = 4.0
EXPANSION_RATIO_WARN = 8.0

# ── TLD которые всегда идут напрямую (не через VPN) ───────────────────────────
# .рф в punycode = xn--p1acf
# Российские домены: не нужен VPN, российские сервисы блокируют иностранные IP
DIRECT_TLDS: frozenset[str] = frozenset({".ru", ".xn--p1acf"})

# ── Источники ─────────────────────────────────────────────────────────────────
# type: "ip_list" | "cidr_list" | "zapret_csv" | "plain"
#       "root_domains" | "domain_list" | "rockblack_zip"
# urls: список URL (для multi-URL источников, скачиваются по очереди)
# proxy: True — требуется SOCKS5 (источник блокируется из РФ)
SOURCES: dict[str, dict] = {
    # ── Antifilter ────────────────────────────────────────────────────────────
    "antifilter_ip": {
        "url":       "https://antifilter.download/list/ip.lst",
        "type":      "ip_list",
        "min_lines": 100,
        "desc":      "Antifilter IP list",
    },
    "antifilter_subnet": {
        "url":       "https://antifilter.download/list/subnet.lst",
        "type":      "cidr_list",
        "min_lines": 50,
        "desc":      "Antifilter subnet list",
        "cidr_for_allowed_ips": True,
    },
    "antifilter_allyouneed": {
        "url":       "https://antifilter.download/list/allyouneed.lst",
        "type":      "cidr_list",
        "min_lines": 100,
        "desc":      "Antifilter all-you-need (14K агрегированных CIDR)",
        "cidr_for_allowed_ips": True,
    },
    "antifilter_domains": {
        "url":       "https://antifilter.download/list/domains.lst",
        "type":      "root_domains",
        "min_lines": 10_000,
        "desc":      "Antifilter domains (1.3M → root domains)",
    },
    # ── OpenCCK (iplist) ──────────────────────────────────────────────────────
    "opencck_cidr4": {
        "url":       "https://iplist.opencck.org/?format=text&data=cidr4",
        "type":      "cidr_list",
        "min_lines": 50,
        "desc":      "OpenCCK CIDR4 (2.7K)",
        "cidr_for_allowed_ips": True,
    },
    "opencck_domains": {
        "url":       "https://iplist.opencck.org/?format=text&data=domains",
        "type":      "domain_list",
        "min_lines": 100,
        "desc":      "OpenCCK domains (26K)",
    },
    # ── Zapret-info (GitHub raw заблокирован из РФ → SOCKS5) ─────────────────
    "zapret_info": {
        "urls": [
            f"https://raw.githubusercontent.com/zapret-info/z-i/master/dump-{i:02d}.csv"
            for i in range(19)
        ],
        "type":      "zapret_csv",
        "min_lines": 100,
        "desc":      "Zapret-info dump CSV (19 файлов, IP + домены)",
        "proxy":     True,
    },
    # ── RockBlack сервисы (GitHub заблокирован из РФ → SOCKS5) ───────────────
    "rockblack_global": {
        "url":       "https://github.com/RockBlack-VPN/ip-address/archive/refs/heads/main.zip",
        "type":      "rockblack_zip",
        "min_lines": 10,
        "desc":      "RockBlack 230 сервисов (Windows route → CIDR + domain)",
        "proxy":     True,
    },
}

# ── Статические AS-блоки CDN (широкий забор, не убирается самообучением) ──────
CDN_SUBNETS: list[str] = [
    # Google (AS15169) — YouTube, GDrive, Gmail, Play Store
    "8.34.208.0/20", "8.35.192.0/20",
    "34.0.0.0/15", "34.2.0.0/16", "34.64.0.0/11",
    "35.184.0.0/14", "35.190.0.0/16",
    "64.233.160.0/19", "66.102.0.0/20", "66.249.64.0/19",
    "72.14.192.0/18", "74.125.0.0/16",
    "104.154.0.0/15", "104.196.0.0/14",
    "108.177.8.0/21", "108.177.96.0/19",
    "130.211.0.0/22", "172.217.0.0/16", "173.194.0.0/16",
    "209.85.128.0/17", "216.58.192.0/19", "216.239.32.0/19",
    # Google Cloud CDN / Googleapis
    "142.250.0.0/15", "199.36.154.0/23", "199.36.156.0/24",
    # Cloudflare (AS13335) — CDN, CF Pages, Workers
    "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "104.16.0.0/12",
    "108.162.192.0/18", "131.0.72.0/22",
    "141.101.64.0/18", "162.158.0.0/15",
    "172.64.0.0/13", "188.114.96.0/20",
    "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17",
    # Meta / Facebook / Instagram (AS32934)
    "31.13.24.0/21", "31.13.64.0/18",
    "66.220.144.0/20", "69.63.176.0/20", "69.171.224.0/19",
    "74.119.76.0/22", "102.132.96.0/20", "103.4.96.0/22",
    "157.240.0.0/16", "163.70.128.0/17",
    "173.252.64.0/18", "185.60.216.0/22", "204.15.20.0/22",
    # Twitter/X (AS13414)
    "104.244.40.0/21", "192.133.76.0/22",
    "199.16.156.0/22", "199.59.148.0/22",
    # Akamai (AS20940) — крупный CDN-провайдер
    "2.16.0.0/13", "23.0.0.0/12", "23.32.0.0/11",
    "60.254.128.0/18", "80.67.64.0/18",
    "92.122.0.0/15", "95.100.0.0/15",
    "96.6.0.0/15", "96.16.0.0/15",
    "104.64.0.0/10", "118.214.0.0/16",
    "173.222.0.0/15", "184.24.0.0/13",
    "184.84.0.0/14", "204.14.208.0/21",
]

# ── Популярные блокируемые домены (статическая база для dnsmasq) ───────────────
STATIC_BLOCKED_DOMAINS: list[str] = [
    # Видео
    "youtube.com", "youtu.be", "googlevideo.com",
    "ggpht.com", "ytimg.com",
    # Соцсети
    "instagram.com", "cdninstagram.com",
    "facebook.com", "fbcdn.net", "fb.com", "fb.me",
    "twitter.com", "x.com", "twimg.com", "t.co",
    # Google сервисы
    "google.com", "googleapis.com", "gstatic.com",
    "googleusercontent.com", "googletagmanager.com",
    "googlesyndication.com", "google-analytics.com",
    # Новости/СМИ
    "meduza.io", "bbc.com", "bbc.co.uk", "dw.com",
    "rferl.org", "currenttime.tv", "svoboda.org",
    # Privacy/VPN
    "proton.me", "protonmail.com", "protonvpn.com",
    "torproject.org",
    # Dev/Tech
    "github.com", "raw.githubusercontent.com",
    "stackoverflow.com",
]

# ── Tier-1 домены — всегда в blocked_static, неудаляемые ──────────────────────
# Сервер в России — эти домены заблокированы на уровне ISP.
# Вносятся в dnsmasq и nft blocked_static независимо от баз РКН и ручных списков.
# НЕ могут быть удалены через /direct add или правкой manual-direct.txt.
TIER1_DOMAINS: list[str] = [
    # Telegram API — сервер использует для отправки алертов
    "api.telegram.org",
    "web.telegram.org",
    "telegram.org",
    "t.me",
]


# =============================================================================
# Определение SOCKS5 прокси
# =============================================================================
def _get_socks5_proxy() -> Optional[str]:
    """
    Определяет активный SOCKS5 порт watchdog.
    Читает state.json, fallback — пробует порты напрямую.
    """
    port_map = {
        "cloudflare-cdn": 1082,
        "reality-xhttp":  1081,
        "hysteria2":      1083,
        "vless-reality-vision": 1084,
    }

    state_paths = [
        Path("/opt/vpn/watchdog/state.json"),
        Path("/tmp/watchdog-state.json"),
    ]
    for sf in state_paths:
        if sf.exists():
            try:
                state = json.loads(sf.read_text())
                stack = state.get("active_stack", "")
                port = port_map.get(stack)
                if port:
                    log.debug(f"SOCKS5 прокси: стек={stack} порт={port}")
                    return f"socks5://127.0.0.1:{port}"
            except Exception:
                pass

    # Fallback: проверяем известные порты
    for port in [1083, 1084, 1081, 1082]:
        try:
            s = socket.socket()
            s.settimeout(1.0)
            s.connect(("127.0.0.1", port))
            s.close()
            log.debug(f"SOCKS5 прокси: порт {port} доступен (fallback)")
            return f"socks5://127.0.0.1:{port}"
        except Exception:
            pass

    return None


# =============================================================================
# Telegram уведомления
# =============================================================================
def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    for parse_mode in ("Markdown", None):
        try:
            payload: dict = {"chat_id": ADMIN_CHAT_ID, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return
        except urllib.error.HTTPError as e:
            if e.code == 400 and parse_mode:
                continue  # Retry without Markdown
            log.debug(f"Telegram HTTP {e.code}: {e}")
            return
        except Exception as exc:
            log.debug(f"Telegram недоступен: {exc}")
            return


# =============================================================================
# Сигнал watchdog → запустить авторассылку конфигов
# =============================================================================
def notify_watchdog_routes_updated() -> None:
    if not WATCHDOG_TOKEN:
        return
    try:
        payload = json.dumps({"reason": "routes_updated"}).encode()
        req = urllib.request.Request(
            f"{WATCHDOG_URL}/notify-clients",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {WATCHDOG_TOKEN}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"Watchdog уведомлён: {resp.status}")
    except Exception as exc:
        log.warning(f"Уведомление watchdog не удалось: {exc}")


# =============================================================================
# Загрузка — низкоуровневые функции
# =============================================================================
def _fetch_raw(url: str, timeout: int = FETCH_TIMEOUT, proxy: Optional[str] = None) -> str:
    """Скачать URL → str. Если proxy задан — использует curl."""
    if proxy:
        result = subprocess.run(
            [
                "curl", "-sL",
                "--max-time", str(timeout),
                "--proxy", proxy,
                "-A", "vpn-routes-updater/2.0",
                url,
            ],
            capture_output=True,
            timeout=timeout + 10,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="ignore")[:200]
            raise OSError(f"curl rc={result.returncode}: {err}")
        return result.stdout.decode("utf-8", errors="ignore")
    else:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "vpn-routes-updater/2.0 (+https://github.com/Cyrillicspb/vpn-infra)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")


def _fetch_bytes(url: str, timeout: int = FETCH_TIMEOUT_ZIP, proxy: Optional[str] = None) -> bytes:
    """Скачать URL → bytes (для ZIP и бинарных файлов)."""
    if proxy:
        result = subprocess.run(
            [
                "curl", "-sL",
                "--max-time", str(timeout),
                "--proxy", proxy,
                "-A", "vpn-routes-updater/2.0",
                url,
            ],
            capture_output=True,
            timeout=timeout + 10,
        )
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="ignore")[:200]
            raise OSError(f"curl rc={result.returncode}: {err}")
        return result.stdout
    else:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "vpn-routes-updater/2.0 (+https://github.com/Cyrillicspb/vpn-infra)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()


# =============================================================================
# Кэш
# =============================================================================
def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.cache"


def _check_cache_age(cache_file: Path, key: str) -> None:
    age_days = (time.time() - cache_file.stat().st_mtime) / 86400
    if age_days > ALERT_CACHE_AGE_DAYS:
        msg = f"⚠️ *Кэш маршрутов `{key}` устарел*: {age_days:.1f} дней\nИсточник недоступен!"
        log.warning(msg.replace("*", "").replace("`", ""))
        send_telegram(msg)


# =============================================================================
# Парсинг источников
# =============================================================================
def _parse_network(s: str) -> Optional[ipaddress.IPv4Network]:
    s = s.strip()
    if not s or s.startswith("#"):
        return None
    try:
        net = ipaddress.ip_network(s, strict=False)
        if net.version == 4:
            return net
    except ValueError:
        pass
    return None


def _is_valid_domain(s: str) -> bool:
    if not s or len(s) > 253:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9.\-]{1,251}[a-z0-9]$", s) and "." in s)


def _to_root_domain(domain: str) -> str:
    """sub.example.com → example.com (простое eTLD+1 без PSL)."""
    parts = domain.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def _is_direct_tld(domain: str) -> bool:
    """True если домен должен идти напрямую (не через VPN)."""
    for tld in DIRECT_TLDS:
        if domain.endswith(tld):
            return True
    return False


def _parse_bat_routes(content: str) -> set[ipaddress.IPv4Network]:
    """
    Парсинг Windows route commands → IPv4Network.
    Формат: route [ADD|add] IP [MASK|mask] SUBNET_MASK GATEWAY
    Пример: route add 104.16.0.0 mask 255.255.0.0 0.0.0.0
    """
    networks: set[ipaddress.IPv4Network] = set()
    for line in content.splitlines():
        line = line.strip()
        m = re.match(
            r"route\s+[Aa][Dd][Dd]\s+(\d+\.\d+\.\d+\.\d+)\s+[Mm][Aa][Ss][Kk]\s+(\d+\.\d+\.\d+\.\d+)",
            line,
        )
        if m:
            ip, mask = m.group(1), m.group(2)
            try:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                networks.add(net)
            except ValueError:
                pass
    return networks


def _parse_source(
    raw: str, src_type: str, key: str, min_lines: int
) -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """Парсинг источника по типу."""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    if len(lines) < min_lines:
        log.warning(f"[{key}] Слишком мало строк: {len(lines)} < {min_lines}")
        return set(), set()

    networks: set[ipaddress.IPv4Network] = set()
    domains: set[str] = set()

    if src_type in ("ip_list", "cidr_list"):
        for line in lines:
            line = line.split("#")[0].strip()
            if not line:
                continue
            net = _parse_network(line)
            if net:
                networks.add(net)

    elif src_type == "plain":
        for line in lines:
            line = line.split("#")[0].strip().lower()
            if not line:
                continue
            net = _parse_network(line)
            if net:
                networks.add(net)
            elif _is_valid_domain(line):
                domains.add(line)

    elif src_type == "domain_list":
        # Список доменов (один на строку)
        for line in lines:
            line = line.split("#")[0].strip().lower().lstrip("*.")
            if line and _is_valid_domain(line):
                domains.add(line)

    elif src_type == "root_domains":
        # Большой список (1M+) → только уникальные root domains
        seen: set[str] = set()
        for line in lines:
            line = line.split("#")[0].strip().lower().lstrip("*.")
            if not line or not _is_valid_domain(line):
                continue
            root = _to_root_domain(line)
            if root not in seen and _is_valid_domain(root):
                seen.add(root)
                domains.add(root)

    elif src_type == "zapret_csv":
        # dump-NN.csv: IP;Домен;Организация;...
        parsed = 0
        for line in lines[:ZAPRET_MAX_LINES]:
            if parsed == 0 and line.startswith("Updated"):
                continue
            parts = line.split(";")
            if len(parts) < 2:
                continue
            # Столбец 0: IP (может быть несколько через |)
            for ip_part in re.split(r"[|\s]+", parts[0].strip()):
                ip_part = ip_part.strip()
                if ip_part:
                    net = _parse_network(ip_part)
                    if net:
                        networks.add(net)
            # Столбец 1: домен (может быть несколько через |)
            for dom in re.split(r"[|\s]+", parts[1].strip()):
                dom = dom.strip().lower().lstrip("*.")
                if dom and _is_valid_domain(dom):
                    domains.add(dom)
            parsed += 1

    return networks, domains


# =============================================================================
# Загрузка RockBlack ZIP
# =============================================================================
def _fetch_and_parse_rockblack_zip(
    proxy: Optional[str],
) -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """
    Скачивает ZIP архив RockBlack ip-address репозитория.
    Парсит все .bat файлы (route add IP mask MASK) и _domain файлы из Global/.
    Пропускает *_Old.bat и *_old.bat — устаревшие версии.
    """
    url = "https://github.com/RockBlack-VPN/ip-address/archive/refs/heads/main.zip"
    log.info(f"[rockblack_global] Скачивание ZIP (~15 MB): {url}")
    data = _fetch_bytes(url, timeout=FETCH_TIMEOUT_ZIP, proxy=proxy)
    log.info(f"[rockblack_global] ZIP скачан: {len(data) // 1024} КБ")

    networks: set[ipaddress.IPv4Network] = set()
    domains: set[str] = set()
    bat_files = 0
    domain_files = 0

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            # Только файлы в Global/*/
            if "/Global/" not in name:
                continue
            fname = name.split("/")[-1]
            if not fname:
                continue  # directory entry

            if fname.endswith(".bat") and "_Old" not in fname and "_old" not in fname:
                try:
                    content = zf.read(name).decode("utf-8", errors="ignore")
                    nets = _parse_bat_routes(content)
                    networks.update(nets)
                    bat_files += 1
                except Exception as e:
                    log.debug(f"[rockblack_global] Ошибка {name}: {e}")

            elif fname.endswith("_domain") or fname == "domain":
                try:
                    content = zf.read(name).decode("utf-8", errors="ignore")
                    for line in content.splitlines():
                        line = line.strip().lower().lstrip("*.")
                        if line and _is_valid_domain(line):
                            domains.add(line)
                    domain_files += 1
                except Exception as e:
                    log.debug(f"[rockblack_global] Ошибка {name}: {e}")

    log.info(
        f"[rockblack_global] Обработано: {bat_files} .bat + {domain_files} domain файлов"
        f" → {len(networks)} CIDR, {len(domains)} доменов"
    )
    return networks, domains


# =============================================================================
# Загрузка одного источника с кэшем
# =============================================================================
def fetch_source(key: str, cfg: dict) -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """
    Загрузить один источник → (set[IPv4Network], set[str] domains).
    При ошибке → кэш. При отсутствии кэша → пустые множества.
    """
    src_type   = cfg["type"]
    desc       = cfg.get("desc", key)
    min_lines  = cfg.get("min_lines", 10)
    need_proxy = cfg.get("proxy", False)

    cache_ips_file  = _cache_path(key)
    cache_dom_file  = _cache_path(f"{key}_domains")

    def _load_from_cache() -> tuple[set[ipaddress.IPv4Network], set[str]]:
        nets: set[ipaddress.IPv4Network] = set()
        doms: set[str] = set()
        if src_type == "rockblack_zip":
            if cache_ips_file.exists():
                _check_cache_age(cache_ips_file, key)
                for line in cache_ips_file.read_text().splitlines():
                    net = _parse_network(line.strip())
                    if net:
                        nets.add(net)
            if cache_dom_file.exists():
                for line in cache_dom_file.read_text().splitlines():
                    line = line.strip()
                    if line and _is_valid_domain(line):
                        doms.add(line)
        else:
            if cache_ips_file.exists():
                _check_cache_age(cache_ips_file, key)
                raw = cache_ips_file.read_text(encoding="utf-8")
                log.info(f"[{key}] Из кэша ({cache_ips_file.stat().st_size // 1024} КБ)")
                nets, doms = _parse_source(raw, src_type, key, min_lines)
        return nets, doms

    try:
        proxy: Optional[str] = None
        if need_proxy:
            proxy = _get_socks5_proxy()
            if not proxy:
                raise OSError("SOCKS5 прокси недоступен")

        if src_type == "rockblack_zip":
            networks, domains = _fetch_and_parse_rockblack_zip(proxy)
            # Кэшируем распарсенный результат
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_ips_file.write_text(
                "\n".join(str(n) for n in networks), encoding="utf-8"
            )
            cache_dom_file.write_text("\n".join(domains), encoding="utf-8")
            log.info(f"[{key}] ✓ {desc}: {len(networks)} CIDR + {len(domains)} доменов")
            return networks, domains

        elif "urls" in cfg:
            # Multi-URL: скачиваем по очереди, объединяем
            all_parts: list[str] = []
            for url in cfg["urls"]:
                try:
                    part = _fetch_raw(url, proxy=proxy)
                    all_parts.append(part)
                except Exception as exc:
                    log.debug(f"[{key}] Skip {url}: {exc}")
            if not all_parts:
                raise OSError("Все URL недоступны")
            raw = "\n".join(all_parts)
            cache_ips_file.write_text(raw, encoding="utf-8")
            networks, domains = _parse_source(raw, src_type, key, min_lines)
            log.info(f"[{key}] ✓ {desc}: {len(networks)} CIDR + {len(domains)} доменов")
            return networks, domains

        else:
            url = cfg["url"]
            log.info(f"[{key}] Загрузка: {url}")
            raw = _fetch_raw(url, proxy=proxy)
            cache_ips_file.write_text(raw, encoding="utf-8")
            log.info(f"[{key}] Загружено {len(raw.splitlines())} строк")
            networks, domains = _parse_source(raw, src_type, key, min_lines)
            log.info(f"[{key}] ✓ {desc}: {len(networks)} CIDR + {len(domains)} доменов")
            return networks, domains

    except Exception as exc:
        log.warning(f"[{key}] Ошибка загрузки: {exc}")
        if cache_ips_file.exists():
            nets, doms = _load_from_cache()
            log.info(f"[{key}] Из кэша: {len(nets)} CIDR + {len(doms)} доменов")
            return nets, doms
        log.error(f"[{key}] Кэш отсутствует — пропуск источника")
        return set(), set()


# =============================================================================
# Параллельная загрузка всех источников
# =============================================================================
def fetch_all_sources() -> tuple[set[ipaddress.IPv4Network], set[ipaddress.IPv4Network], set[str]]:
    """
    Возвращает (all_networks, cidr_networks, all_domains).
    all_networks  — все IP/CIDR из всех источников (для nft blocked_static).
    cidr_networks — только CIDR-based источники с cidr_for_allowed_ips=True
                    (для combined.cidr AllowedIPs — без /32 IP, чтобы не раздувать маски).
    """
    all_networks: set[ipaddress.IPv4Network] = set()
    cidr_networks: set[ipaddress.IPv4Network] = set()
    all_domains: set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(fetch_source, key, cfg): (key, cfg)
            for key, cfg in SOURCES.items()
        }
        for future in concurrent.futures.as_completed(futures):
            key, cfg = futures[future]
            try:
                nets, doms = future.result()
                all_networks.update(nets)
                all_domains.update(doms)
                if cfg.get("cidr_for_allowed_ips"):
                    cidr_networks.update(nets)
            except Exception as exc:
                log.error(f"[{key}] Неожиданная ошибка: {exc}")

    return all_networks, cidr_networks, all_domains


# =============================================================================
# Ручные списки
# =============================================================================
def load_manual_vpn() -> tuple[set[ipaddress.IPv4Network], set[str]]:
    networks: set[ipaddress.IPv4Network] = set()
    domains: set[str] = set()

    if not MANUAL_VPN.exists():
        return networks, domains

    for line in MANUAL_VPN.read_text().splitlines():
        line = line.split("#")[0].strip().lower()
        if not line:
            continue
        net = _parse_network(line)
        if net:
            networks.add(net)
        elif _is_valid_domain(line):
            domains.add(line)

    log.info(f"[manual-vpn] {len(networks)} CIDR + {len(domains)} доменов")
    return networks, domains


def load_active_dpi_domains() -> set[str]:
    """
    Считать домены активных DPI bypass сервисов из watchdog state.
    Эти домены нельзя одновременно писать в blocked_dynamic, иначе dnsmasq
    начинает заполнять только один set и DPI path теряет смысл.
    """
    domains: set[str] = set()
    if not WATCHDOG_STATE.exists():
        return domains
    try:
        data = json.loads(WATCHDOG_STATE.read_text(encoding="utf-8"))
        for svc in data.get("dpi_services", []):
            if not svc.get("enabled", True):
                continue
            for domain in svc.get("domains", []):
                domain = str(domain).strip().lower().lstrip("*.")
                if domain and _is_valid_domain(domain):
                    domains.add(domain)
    except Exception as exc:
        log.warning(f"[dpi] Не удалось прочитать {WATCHDOG_STATE}: {exc}")
    if domains:
        log.info(f"[dpi] Активных DPI-доменов: {len(domains)}")
    return domains


# =============================================================================
# Агрегация CIDR
# =============================================================================
def aggregate_networks(networks: set[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    if not networks:
        return []
    return sorted(ipaddress.collapse_addresses(networks))


def prefix_distribution(networks: list[ipaddress.IPv4Network]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for net in networks:
        counts[net.prefixlen] = counts.get(net.prefixlen, 0) + 1
    return dict(sorted(counts.items()))


def total_address_count(networks: list[ipaddress.IPv4Network]) -> int:
    return sum(net.num_addresses for net in networks)


def expansion_ratio(
    original_networks: list[ipaddress.IPv4Network],
    merged_networks: list[ipaddress.IPv4Network],
) -> float:
    original_size = total_address_count(original_networks)
    if original_size == 0:
        return 1.0
    return total_address_count(merged_networks) / original_size


def normalize_allowed_networks(
    networks: set[ipaddress.IPv4Network],
) -> tuple[list[ipaddress.IPv4Network], dict[str, int | float | dict[int, int]]]:
    """Временная нормализация AllowedIPs без агрессивного минимизатора.

    Правила:
    - не оставляем /7 и /8;
    - /9 и /10 временно допускаем только как exact-coverage после collapse, без
      дополнительного расширения адресного пространства;
    - если после нормализации список всё ещё слишком большой, лучше зафейлить
      обновление, чем снова размыть маршрутизацию.
    """
    exact_collapsed = aggregate_networks(networks)
    normalized: list[ipaddress.IPv4Network] = []
    split_count = 0

    for net in exact_collapsed:
        if net.prefixlen in FORBIDDEN_ALLOWED_PREFIXES:
            subnets = list(net.subnets(new_prefix=9))
            normalized.extend(subnets)
            split_count += len(subnets) - 1
        else:
            normalized.append(net)

    normalized = sorted(set(normalized))
    stats: dict[str, int | float | dict[int, int]] = {
        "before_count": len(exact_collapsed),
        "after_count": len(normalized),
        "split_count": split_count,
        "before_distribution": prefix_distribution(exact_collapsed),
        "after_distribution": prefix_distribution(normalized),
        "expansion_ratio": expansion_ratio(exact_collapsed, normalized),
        "forbidden_count": sum(1 for net in normalized if net.prefixlen in FORBIDDEN_ALLOWED_PREFIXES),
        "conditional_count": sum(1 for net in normalized if net.prefixlen in CONDITIONAL_ALLOWED_PREFIXES),
    }
    return normalized, stats


# =============================================================================
# Дельта-проверка
# =============================================================================
def validate_delta(old_set: set[str], new_set: set[str]) -> bool:
    if not old_set:
        return True  # Первый запуск

    added   = len(new_set - old_set)
    removed = len(old_set - new_set)
    delta   = (added + removed) / max(len(old_set), 1) * 100

    log.info(f"Дельта маршрутов: +{added} -{removed} = {delta:.1f}%")

    if delta > MAX_DELTA_PCT:
        msg = (
            f"⚠️ *Большая дельта маршрутов*: {delta:.1f}%\n"
            f"+{added} добавлено, -{removed} удалено\n"
            "Обновление *не применено* — проверьте источники вручную."
        )
        log.warning(msg.replace("*", ""))
        send_telegram(msg)
        return False

    return True


# =============================================================================
# Запись nftables-blocked-static.conf
# =============================================================================
def write_nftables_static(networks: list[ipaddress.IPv4Network]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = [
        f"# Автогенерировано update-routes.py  {now}",
        f"# Записей: {len(networks)}",
        "# Применение: nft -f /etc/nftables-blocked-static.conf",
        "# ВАЖНО: flush + add в одном файле = атомарная транзакция (нет окна утечки)",
        "",
        "# Шаг 1: очищаем set",
        "flush set inet vpn blocked_static",
        "",
        "# Шаг 2: заполняем (в той же транзакции что и flush)",
    ]

    if networks:
        CHUNK = 500
        chunks = [networks[i:i+CHUNK] for i in range(0, len(networks), CHUNK)]
        for chunk in chunks:
            elements = ",\n    ".join(str(n) for n in chunk)
            lines.append(f"add element inet vpn blocked_static {{\n    {elements}\n}}")
    else:
        lines.append("add element inet vpn blocked_static { }")

    NFT_STATIC.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"nftables-blocked-static.conf: {len(networks)} записей")


# =============================================================================
# Запись dnsmasq конфига
# =============================================================================
def render_dnsmasq_config(
    domains: list[str],
    out_name: str,
    vps_dns: str,
    header_comment: str = "Автогенерировано update-routes.py",
    exclude_domains: set[str] | None = None,
) -> tuple[str, int, int]:
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = [
        f"# {out_name} — {header_comment}",
        f"# Обновлено: {now}",
        f"# Доменов: {len(domains)}",
        "# НЕ РЕДАКТИРОВАТЬ ВРУЧНУЮ — перезаписывается update-routes.py",
        "",
    ]

    written = 0
    excluded = 0
    exclude_domains = exclude_domains or set()
    for domain in sorted(set(domains)):
        domain = domain.strip().lower().lstrip("*.")
        if not domain or not _is_valid_domain(domain):
            continue
        if _is_direct_tld(domain):
            continue  # .ru/.рф → всегда прямое, не добавлять через VPS
        if domain in exclude_domains:
            excluded += 1
            continue
        lines.append(f"server=/{domain}/{vps_dns}")
        lines.append(f"nftset=/{domain}/4#inet#vpn#blocked_dynamic")
        lines.append("")
        written += 1

    return "\n".join(lines), written, excluded


def write_dnsmasq_config(
    domains: list[str],
    out_file: Path,
    vps_dns: str,
    header_comment: str = "Автогенерировано update-routes.py",
    exclude_domains: set[str] | None = None,
) -> str:
    content, written, excluded = render_dnsmasq_config(
        domains,
        out_file.name,
        vps_dns,
        header_comment=header_comment,
        exclude_domains=exclude_domains,
    )

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(content, encoding="utf-8")
    log.info(
        f"{out_file.name}: {written} доменов "
        f"(пропущено: direct TLD/invalid/DPI duplicates = {len(domains) - written}, "
        f"из них DPI duplicates = {excluded})"
    )
    return content


# =============================================================================
# Запись vpn-direct.conf — прямые TLD (.ru, .рф)
# =============================================================================
def render_dnsmasq_direct() -> str:
    """
    vpn-direct.conf: .ru и .рф всегда резолвятся через Яндекс DNS напрямую.
    server=/.ru/77.88.8.8 — явный upstream, НЕ пустой.
    Пустой server=/.ru/ в dnsmasq означает "только локальные данные" (local-only),
    что ломает резолвинг .ru доменов. Нужны явные адреса серверов.
    1.1.1.1/8.8.8.8 заблокированы ISP в России — используем Яндекс DNS 77.88.8.8/77.88.8.1.
    НЕТ nftset — IP не добавляются в blocked_dynamic → пакеты идут напрямую.
    """
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# vpn-direct.conf — TLD с прямым подключением (без VPN)",
        f"# Обновлено: {now}",
        "# .ru и .рф (punycode: xn--p1acf) — резолвятся через Яндекс DNS напрямую",
        "# IP этих доменов НЕ добавляются в blocked_dynamic → трафик идёт напрямую",
        "# Без этого файла zapret/antifilter отправляли бы .ru домены через VPS DNS",
        "# ВАЖНО: server=/.ru/ без адреса = local-only (NXDOMAIN), нужен явный upstream",
        "",
        "# .ru — российские домены",
        "server=/.ru/77.88.8.8",
        "server=/.ru/77.88.8.1",
        "",
        "# .рф — российские домены в кириллице (IDN punycode)",
        "server=/.xn--p1acf/77.88.8.8",
        "server=/.xn--p1acf/77.88.8.1",
        "",
    ]
    return "\n".join(lines)


def write_dnsmasq_direct() -> None:
    content = render_dnsmasq_direct()
    DNSMASQ_DIRECT.parent.mkdir(parents=True, exist_ok=True)
    DNSMASQ_DIRECT.write_text(content, encoding="utf-8")
    log.info("vpn-direct.conf: .ru/.рф → прямое подключение (локальный DNS)")


# =============================================================================
# Применение изменений
# =============================================================================
def apply_nftables() -> bool:
    try:
        result = subprocess.run(
            ["nft", "-f", str(NFT_STATIC)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.info("nftables blocked_static применён")
            return True
        else:
            log.error(f"nft -f ошибка:\n{result.stderr.strip()}")
            send_telegram(f"❌ *nft -f ошибка* при обновлении маршрутов:\n```{result.stderr[:500]}```")
            return False
    except subprocess.TimeoutExpired:
        log.error("nft -f timeout")
        return False
    except FileNotFoundError:
        log.warning("nft не найден — пропуск применения")
        return False


def reload_dnsmasq() -> None:
    try:
        result = subprocess.run(
            ["systemctl", "restart", "dnsmasq"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("dnsmasq перезапущен (cache flushed)")
        else:
            log.warning(f"dnsmasq restart: {result.stderr.strip()}")
    except Exception as exc:
        log.warning(f"dnsmasq restart failed: {exc}")


# =============================================================================
# Diff и hash
# =============================================================================
def compute_diff(old_lines: list[str], new_lines: list[str]) -> dict:
    old_set = {l for l in old_lines if l and not l.startswith("#")}
    new_set = {l for l in new_lines if l and not l.startswith("#")}
    added   = new_set - old_set
    removed = old_set - new_set
    return {
        "added":         sorted(added)[:20],
        "removed":       sorted(removed)[:20],
        "added_total":   len(added),
        "removed_total": len(removed),
        "old_total":     len(old_set),
        "new_total":     len(new_set),
    }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_generated_text(text: str) -> str:
    """Нормализует автогенерированный текст для change-detection.

    Убирает только volatile header-поля вроде timestamp, чтобы повторный no-op
    прогон не выглядел как изменение.
    """
    lines = []
    for line in text.splitlines():
        if line.startswith("# Обновлено:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def build_stable_hash_payload(
    allowed_cidrs: list[str],
    nft_cidrs: list[str],
    dnsmasq_domains_content: str,
    dnsmasq_force_content: str,
    dnsmasq_direct_content: str,
) -> str:
    """Возвращает стабильный payload для HASH_FILE.

    Должен менять hash при изменении любого реально применяемого артефакта, но
    игнорировать volatile заголовки с timestamp.
    """
    return json.dumps(
        {
            "allowed_ips": allowed_cidrs,
            "nft_blocked_static": nft_cidrs,
            "dnsmasq_domains": normalize_generated_text(dnsmasq_domains_content),
            "dnsmasq_force": normalize_generated_text(dnsmasq_force_content),
            "dnsmasq_direct": normalize_generated_text(dnsmasq_direct_content),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    dry_run = "--dry-run" in sys.argv
    force   = "--force"   in sys.argv

    if dry_run:
        log.info("=== DRY-RUN: изменения не применяются ===")

    log.info("=== Обновление маршрутов (update-routes.py) ===")
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Предыдущий combined.cidr ───────────────────────────────────────────────
    old_lines: list[str] = []
    if COMBINED_CIDR.exists():
        old_lines = COMBINED_CIDR.read_text().splitlines()
    old_hash = HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""

    # ── Загрузка всех источников (параллельно) ────────────────────────────────
    all_networks, cidr_networks, all_domains = fetch_all_sources()

    # Добавляем статические AS-блоки CDN (в оба set-а)
    for cidr in CDN_SUBNETS:
        net = _parse_network(cidr)
        if net:
            all_networks.add(net)
            cidr_networks.add(net)
    log.info(f"CDN блоки: {len(CDN_SUBNETS)} добавлено")

    # Добавляем статические домены
    all_domains.update(STATIC_BLOCKED_DOMAINS)
    # Tier-1 домены — всегда включены, неудаляемые (Telegram API и др.)
    all_domains.update(TIER1_DOMAINS)

    # Загружаем ручные списки (manual-vpn.txt → в оба set-а)
    manual_networks, manual_domains = load_manual_vpn()
    all_networks.update(manual_networks)
    cidr_networks.update(manual_networks)
    dpi_domains = load_active_dpi_domains()

    if not all_networks:
        log.error("Нет данных для обновления!")
        send_telegram("❌ *update-routes.py*: нет данных из всех источников")
        sys.exit(1)

    log.info(f"Всего до агрегации: {len(all_networks)} сетей, {len(all_domains)} доменов")
    log.info(f"CIDR-only (для AllowedIPs): {len(cidr_networks)} сетей")

    # ── Агрегация: полный set для nft blocked_static ───────────────────────────
    log.info("Агрегация nft blocked_static (без лимита)...")
    nft_networks = aggregate_networks(all_networks)
    log.info(f"nft blocked_static: {len(all_networks)} → {len(nft_networks)} после агрегации")

    # ── Нормализация: AllowedIPs (combined.cidr) ──────────────────────────────
    # Используем только CIDR-based источники (не /32 IP-листы), но больше не
    # аггрегируем список агрессивно под маленький лимит. Для клиентского routing
    # correctness важнее компактности; временно запрещаем только /7 и /8.
    # /9 и /10 допускаем как exact-coverage после collapse, без дальнейшего merge.
    log.info("Нормализация AllowedIPs (temporary safety policy, correctness-first)...")
    allowed_networks, allowed_stats = normalize_allowed_networks(cidr_networks)
    log.info(
        "AllowedIPs normalized: %s → %s (split=%s, expansion_ratio=%.2f)",
        allowed_stats["before_count"],
        allowed_stats["after_count"],
        allowed_stats["split_count"],
        allowed_stats["expansion_ratio"],
    )
    log.info("AllowedIPs prefixes before: %s", allowed_stats["before_distribution"])
    log.info("AllowedIPs prefixes after:  %s", allowed_stats["after_distribution"])

    if allowed_stats["forbidden_count"]:
        log.error("AllowedIPs still contain forbidden /7-/8 prefixes after normalization")
        sys.exit(1)
    if allowed_stats["conditional_count"]:
        log.warning(
            "AllowedIPs contain conditional /9-/10 prefixes: %s",
            allowed_stats["conditional_count"],
        )
    if float(allowed_stats["expansion_ratio"]) > EXPANSION_RATIO_WARN:
        log.error(
            "AllowedIPs expansion_ratio too high: %.2f > %.2f",
            allowed_stats["expansion_ratio"],
            EXPANSION_RATIO_WARN,
        )
        sys.exit(1)
    if float(allowed_stats["expansion_ratio"]) > EXPANSION_RATIO_OK:
        log.warning(
            "AllowedIPs expansion_ratio elevated: %.2f > %.2f",
            allowed_stats["expansion_ratio"],
            EXPANSION_RATIO_OK,
        )
    if len(allowed_networks) > MAX_CIDR_ALLOWED_IPS:
        log.error(
            "AllowedIPs exceed safe temporary limit: %s > %s",
            len(allowed_networks),
            MAX_CIDR_ALLOWED_IPS,
        )
        sys.exit(1)

    log.info(f"AllowedIPs: {len(allowed_networks)} записей")

    if len(allowed_networks) > 50:
        log.info("QR-коды недоступны (>50 AllowedIPs) — отправлять .conf файлами")

    # ── Дельта-проверка ────────────────────────────────────────────────────────
    new_cidr_lines = [str(n) for n in allowed_networks]
    if not force and not validate_delta(
        {l for l in old_lines if l and not l.startswith("#")},
        set(new_cidr_lines),
    ):
        sys.exit(1)

    # ── Diff ──────────────────────────────────────────────────────────────────
    diff = compute_diff(old_lines, new_cidr_lines)
    log.info(
        f"Diff: +{diff['added_total']} -{diff['removed_total']} "
        f"(было {diff['old_total']}, стало {diff['new_total']})"
    )
    if diff["added"][:5]:
        log.info(f"  Добавлены: {', '.join(diff['added'][:5])}{'...' if diff['added_total'] > 5 else ''}")
    if diff["removed"][:5]:
        log.info(f"  Удалены:   {', '.join(diff['removed'][:5])}{'...' if diff['removed_total'] > 5 else ''}")

    # ── Проверяем изменился ли результат ──────────────────────────────────────
    dnsmasq_domains_content, _, _ = render_dnsmasq_config(
        list(all_domains),
        DNSMASQ_DOMAINS.name,
        VPS_TUNNEL_IP,
        header_comment="Базы РКН + статические домены",
        exclude_domains=dpi_domains,
    )
    dnsmasq_force_content, _, _ = render_dnsmasq_config(
        list(manual_domains),
        DNSMASQ_FORCE.name,
        VPS_TUNNEL_IP,
        header_comment="manual-vpn.txt (добавлены через /vpn add)",
        exclude_domains=dpi_domains,
    )
    dnsmasq_direct_content = render_dnsmasq_direct()
    nft_cidr_lines = [str(n) for n in nft_networks]
    normalized_dnsmasq_domains = normalize_generated_text(dnsmasq_domains_content)
    normalized_dnsmasq_force = normalize_generated_text(dnsmasq_force_content)
    normalized_dnsmasq_direct = normalize_generated_text(dnsmasq_direct_content)
    stable_hash_payload = build_stable_hash_payload(
        allowed_cidrs=new_cidr_lines,
        nft_cidrs=nft_cidr_lines,
        dnsmasq_domains_content=dnsmasq_domains_content,
        dnsmasq_force_content=dnsmasq_force_content,
        dnsmasq_direct_content=dnsmasq_direct_content,
    )
    new_hash = content_hash(stable_hash_payload)
    combined_content = (
        f"# combined.cidr — AllowedIPs для WireGuard клиентов\n"
        f"# Обновлено: {datetime.now(timezone.utc).isoformat()}\n"
        f"# Записей: {len(allowed_networks)}\n"
        f"# nft blocked_static: {len(nft_networks)} (полный)\n"
        + "\n".join(new_cidr_lines)
        + "\n"
    )
    current_dnsmasq_domains = normalize_generated_text(
        DNSMASQ_DOMAINS.read_text(encoding="utf-8")
    ) if DNSMASQ_DOMAINS.exists() else ""
    current_dnsmasq_force = normalize_generated_text(
        DNSMASQ_FORCE.read_text(encoding="utf-8")
    ) if DNSMASQ_FORCE.exists() else ""
    current_dnsmasq_direct = normalize_generated_text(
        DNSMASQ_DIRECT.read_text(encoding="utf-8")
    ) if DNSMASQ_DIRECT.exists() else ""
    changed = (
        new_hash != old_hash
        or normalized_dnsmasq_domains != current_dnsmasq_domains
        or normalized_dnsmasq_force != current_dnsmasq_force
        or normalized_dnsmasq_direct != current_dnsmasq_direct
    )

    if not changed and not force:
        log.info("Маршруты не изменились — обновление не требуется (--force для принудительного)")
        return

    if dry_run:
        log.info(f"[DRY-RUN] nft blocked_static: {len(nft_networks)} записей")
        log.info(f"[DRY-RUN] AllowedIPs (combined.cidr): {len(allowed_networks)} записей")
        log.info(f"[DRY-RUN] Доменов (dnsmasq): {len(all_domains)}")
        log.info(f"[DRY-RUN] Изменено: {changed}")
        return

    # ── Запись файлов ──────────────────────────────────────────────────────────
    COMBINED_CIDR.write_text(combined_content, encoding="utf-8")
    HASH_FILE.write_text(new_hash, encoding="utf-8")
    log.info(f"combined.cidr: {len(allowed_networks)} записей")

    write_nftables_static(nft_networks)

    DNSMASQ_DOMAINS.write_text(dnsmasq_domains_content, encoding="utf-8")
    log.info(f"{DNSMASQ_DOMAINS.name}: конфиг записан")
    DNSMASQ_FORCE.write_text(dnsmasq_force_content, encoding="utf-8")
    log.info(f"{DNSMASQ_FORCE.name}: конфиг записан")

    DNSMASQ_DIRECT.write_text(dnsmasq_direct_content, encoding="utf-8")
    log.info(f"{DNSMASQ_DIRECT.name}: конфиг записан")

    # Логируем итоговые счётчики после записи, чтобы было видно исключение DPI-доменов.
    _, dnsmasq_written, dnsmasq_excluded = render_dnsmasq_config(
        list(all_domains),
        DNSMASQ_DOMAINS.name,
        VPS_TUNNEL_IP,
        header_comment="Базы РКН + статические домены",
        exclude_domains=dpi_domains,
    )
    _, force_written, force_excluded = render_dnsmasq_config(
        list(manual_domains),
        DNSMASQ_FORCE.name,
        VPS_TUNNEL_IP,
        header_comment="manual-vpn.txt (добавлены через /vpn add)",
        exclude_domains=dpi_domains,
    )
    log.info(
        f"dnsmasq итог: vpn-domains={dnsmasq_written}, vpn-force={force_written}, "
        f"dpi-excluded={dnsmasq_excluded + force_excluded}"
    )

    # ── Применение (только root) ───────────────────────────────────────────────
    if os.geteuid() == 0:
        apply_nftables()
        reload_dnsmasq()
    else:
        log.warning("Не root — применение nftables/dnsmasq пропущено")

    # ── Сигнал watchdog ────────────────────────────────────────────────────────
    if changed:
        (ROUTES_DIR / "routes-updated").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
        notify_watchdog_routes_updated()

    # ── Итоговый алерт ─────────────────────────────────────────────────────────
    summary = (
        f"✅ *Маршруты обновлены*\n"
        f"nft blocked\\_static: `{len(nft_networks)}` CIDR\n"
        f"AllowedIPs: `{len(allowed_networks)}` CIDR\n"
        f"dnsmasq доменов: `{len(all_domains)}`\n"
        f"+{diff['added_total']} / -{diff['removed_total']}"
    )
    log.info(summary.replace("*", "").replace("`", "").replace("\\_", "_"))
    if changed:
        send_telegram(summary)

    log.info("=== Обновление маршрутов завершено ===")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
update-routes.py — Обновление маршрутов из баз РКН/CDN

Запуск:
  python3 update-routes.py             — полное обновление
  python3 update-routes.py --dry-run   — показать результат без применения
  python3 update-routes.py --force     — применить даже без изменений

Источники (6+):
  1. antifilter.download/list/ip.lst
  2. antifilter.download/list/subnet.lst
  3. community.antifilter.download/list/ip.lst
  4. iplist.opencck.org/lists/ipv4/subnet.txt
  5. github.com/zapret-info/z-i/dump.csv     (IP + домены)
  6. github.com/RockBlack-VPN                (геоблокировки)
  7. /etc/vpn-routes/manual-vpn.txt          (ручные IP/домены)
  8. Статические AS-блоки CDN               (Google, CF, Meta, Akamai)

Выходные файлы:
  /etc/nftables-blocked-static.conf   — атомарное обновление nft set (flush + add)
  /etc/vpn-routes/combined.cidr       — агрегированные CIDR для AllowedIPs (≤500)
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
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
MANUAL_VPN      = ROUTES_DIR / "manual-vpn.txt"
MANUAL_DIRECT   = ROUTES_DIR / "manual-direct.txt"

# ── Переменные из окружения ────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
WATCHDOG_TOKEN  = os.getenv("WATCHDOG_API_TOKEN", "")
WATCHDOG_URL    = os.getenv("WATCHDOG_URL", "http://localhost:8080")
VPS_TUNNEL_IP   = os.getenv("VPS_TUNNEL_IP", "10.177.2.2")

# ── Константы ─────────────────────────────────────────────────────────────────
MAX_CIDR_ALLOWED_IPS = 500    # Лимит записей AllowedIPs (для QR: ≤50)
ALERT_CACHE_AGE_DAYS = 3      # Алерт если кэш старше N дней
MAX_DELTA_PCT        = 50     # Максимальная дельта изменений (%)
FETCH_TIMEOUT        = 45     # Таймаут загрузки источника (сек)
FETCH_WORKERS        = 4      # Параллельные загрузки
ZAPRET_MAX_LINES     = 300_000  # Лимит строк из dump.csv (файл ~100 MB)

# ── Источники ─────────────────────────────────────────────────────────────────
# type: "ip_list" | "cidr_list" | "zapret_csv" | "plain"
SOURCES: dict[str, dict] = {
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
    },
    "antifilter_community": {
        "url":       "https://community.antifilter.download/list/ip.lst",
        "type":      "ip_list",
        "min_lines": 100,
        "desc":      "Antifilter Community IP list",
    },
    "opencck_subnet": {
        "url":       "https://iplist.opencck.org/lists/ipv4/subnet.txt",
        "type":      "cidr_list",
        "min_lines": 50,
        "desc":      "OpenCCK subnet list",
    },
    "zapret_info": {
        "url":       "https://raw.githubusercontent.com/zapret-info/z-i/master/dump.csv",
        "type":      "zapret_csv",
        "min_lines": 100,
        "desc":      "Zapret-info dump.csv (IP + домены)",
    },
    "rockblack_geoblocks": {
        "url":       "https://raw.githubusercontent.com/RockBlack-VPN/ipv4-blacklist/main/ipv4-blacklist.txt",
        "type":      "cidr_list",
        "min_lines": 10,
        "desc":      "RockBlack геоблокировки",
    },
}

# ── Статические AS-блоки CDN (широкий забор, не убирается самообучением) ──────
# Источник: RIPE NCC, AS15169 (Google), AS13335 (Cloudflare), AS32934 (Meta)
CDN_SUBNETS: list[str] = [
    # Google (AS15169) — YouTube, GDrive, Gmail, Play Store
    "8.8.8.8/32", "8.8.4.4/32",
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
    "1.1.1.1/32", "1.0.0.1/32",
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
    "ggpht.com", "ytimg.com", "yt3.ggpht.com",
    # Соцсети
    "instagram.com", "cdninstagram.com",
    "facebook.com", "fbcdn.net", "fb.com", "fb.me",
    "twitter.com", "x.com", "twimg.com", "t.co",
    # Google сервисы
    "google.com", "googleapis.com", "gstatic.com",
    "googleusercontent.com", "googletagmanager.com",
    "googlesyndication.com", "google-analytics.com",
    # Мессенджеры (в случае блокировки)
    "telegram.org", "t.me",
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


# =============================================================================
# Telegram уведомления
# =============================================================================
def send_telegram(text: str, parse_mode: str = "Markdown") -> None:
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id": ADMIN_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.debug(f"Telegram недоступен: {exc}")


# =============================================================================
# Сигнал watchdog → запустить авторассылку конфигов
# =============================================================================
def notify_watchdog_routes_updated() -> None:
    """POST /notify-clients к watchdog — триггер авторассылки конфигов."""
    if not WATCHDOG_TOKEN:
        log.debug("WATCHDOG_API_TOKEN не задан, пропуск уведомления watchdog")
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
# Загрузка источников с кэшем
# =============================================================================
def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.cache"


def _check_cache_age(cache_file: Path, key: str) -> None:
    """Алерт если кэш старше ALERT_CACHE_AGE_DAYS."""
    age_days = (time.time() - cache_file.stat().st_mtime) / 86400
    if age_days > ALERT_CACHE_AGE_DAYS:
        msg = f"⚠️ *Кэш маршрутов `{key}` устарел*: {age_days:.1f} дней\nИсточник недоступен!"
        log.warning(msg.replace("*", "").replace("`", ""))
        send_telegram(msg)


def _fetch_raw(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    """Скачать URL → сырой текст."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "vpn-routes-updater/2.0 (+https://github.com/Cyrillicspb/vpn-infra)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_source(key: str, cfg: dict) -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """
    Скачать один источник → (set[IPv4Network], set[str] domains).
    При ошибке → кэш. При отсутствии кэша → пустые множества.
    """
    cache_file = _cache_path(key)
    url = cfg["url"]
    min_lines = cfg.get("min_lines", 10)
    src_type = cfg["type"]
    desc = cfg.get("desc", key)

    raw: Optional[str] = None
    from_cache = False

    try:
        log.info(f"[{key}] Загрузка: {url}")
        raw = _fetch_raw(url)
        # Сохраняем в кэш
        cache_file.write_text(raw, encoding="utf-8")
        log.info(f"[{key}] Загружено {len(raw.splitlines())} строк")
    except Exception as exc:
        log.warning(f"[{key}] Ошибка загрузки: {exc}")
        if cache_file.exists():
            raw = cache_file.read_text(encoding="utf-8")
            from_cache = True
            _check_cache_age(cache_file, key)
            log.info(f"[{key}] Используется кэш ({cache_file.stat().st_size // 1024} КБ)")
        else:
            log.error(f"[{key}] Кэш отсутствует — пропуск источника")
            return set(), set()

    # Парсинг
    networks, domains = _parse_source(raw, src_type, key, min_lines)

    if not from_cache:
        log.info(f"[{key}] ✓ {desc}: {len(networks)} CIDR + {len(domains)} доменов")

    return networks, domains


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

    if src_type == "ip_list":
        # Файл: по одному IP или CIDR на строку
        for line in lines:
            line = line.split("#")[0].strip()
            if not line:
                continue
            net = _parse_network(line)
            if net:
                networks.add(net)

    elif src_type == "cidr_list":
        # Файл: CIDR-блоки, возможно с комментариями
        for line in lines:
            line = line.split("#")[0].strip()
            if not line:
                continue
            net = _parse_network(line)
            if net:
                networks.add(net)

    elif src_type == "zapret_csv":
        # dump.csv: IP;Домен;Организация;...
        # Первая строка может быть заголовком
        parsed = 0
        for line in lines[:ZAPRET_MAX_LINES]:
            if parsed == 0 and line.startswith("Updated"):
                continue  # Заголовок
            parts = line.split(";")
            if len(parts) < 2:
                continue
            # Столбец 0: IP или CIDR (может быть несколько через |)
            ip_field = parts[0].strip()
            for ip_part in re.split(r"[|\s]+", ip_field):
                ip_part = ip_part.strip()
                if ip_part:
                    net = _parse_network(ip_part)
                    if net:
                        networks.add(net)
            # Столбец 1: домен (может быть несколько через |)
            domain_field = parts[1].strip()
            for dom in re.split(r"[|\s]+", domain_field):
                dom = dom.strip().lower().lstrip("*.")
                if dom and _is_valid_domain(dom):
                    domains.add(dom)
            parsed += 1

    elif src_type == "plain":
        # Простой список: IP, CIDR или домены вперемешку
        for line in lines:
            line = line.split("#")[0].strip().lower()
            if not line:
                continue
            net = _parse_network(line)
            if net:
                networks.add(net)
            elif _is_valid_domain(line):
                domains.add(line)

    return networks, domains


def _parse_network(s: str) -> Optional[ipaddress.IPv4Network]:
    """Строка → IPv4Network или None."""
    s = s.strip()
    if not s or s.startswith("#"):
        return None
    # Убираем /32 → оставляем как /32 (не расширяем)
    try:
        net = ipaddress.ip_network(s, strict=False)
        if net.version == 4:
            return net
    except ValueError:
        pass
    return None


def _is_valid_domain(s: str) -> bool:
    """Простая проверка что строка похожа на домен."""
    if not s or len(s) > 253:
        return False
    if re.match(r"^[a-z0-9][a-z0-9.\-]{1,251}[a-z0-9]$", s) and "." in s:
        return True
    return False


# =============================================================================
# Параллельная загрузка всех источников
# =============================================================================
def fetch_all_sources() -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """Загрузить все источники параллельно."""
    all_networks: set[ipaddress.IPv4Network] = set()
    all_domains: set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(fetch_source, key, cfg): key
            for key, cfg in SOURCES.items()
        }
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                nets, doms = future.result()
                all_networks.update(nets)
                all_domains.update(doms)
            except Exception as exc:
                log.error(f"[{key}] Неожиданная ошибка: {exc}")

    return all_networks, all_domains


# =============================================================================
# Ручные списки
# =============================================================================
def load_manual_vpn() -> tuple[set[ipaddress.IPv4Network], set[str]]:
    """Загрузить /etc/vpn-routes/manual-vpn.txt → (сети, домены)."""
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


# =============================================================================
# Агрегация CIDR
# =============================================================================
def aggregate_networks(networks: set[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    """Схлопнуть перекрывающиеся/смежные сети."""
    if not networks:
        return []
    return sorted(ipaddress.collapse_addresses(networks))


def reduce_to_limit(
    networks: list[ipaddress.IPv4Network], limit: int
) -> list[ipaddress.IPv4Network]:
    """
    Прогрессивная агрегация для AllowedIPs (≤ limit записей).
    Стратегия: расширяем маски шагами /32→/24→/22→/20→/18→/16
    пока не уложимся в лимит.
    """
    if len(networks) <= limit:
        return networks

    log.info(f"Прогрессивная агрегация: {len(networks)} → ≤{limit}")

    # Шаги: минимальная длина префикса для "мелких" сетей
    # т.е. всё что мельче порога → расширяем до порога
    thresholds = [24, 22, 20, 18, 16, 14, 12, 10, 8]

    for min_prefix in thresholds:
        promoted: set[ipaddress.IPv4Network] = set()
        for net in networks:
            if net.prefixlen > min_prefix:
                # Расширяем до min_prefix
                promoted.add(net.supernet(new_prefix=min_prefix))
            else:
                promoted.add(net)
        collapsed = sorted(ipaddress.collapse_addresses(promoted))
        log.info(f"  Порог /{min_prefix}: {len(collapsed)} записей")
        if len(collapsed) <= limit:
            return collapsed
        networks = collapsed

    # Если даже после /8 не уложились — берём первые limit записей
    log.warning(f"Не удалось сократить до {limit}, берём топ {limit} по размеру")
    return sorted(networks, key=lambda n: n.prefixlen)[:limit]


# =============================================================================
# Дельта-проверка
# =============================================================================
def validate_delta(old_set: set[str], new_set: set[str]) -> bool:
    """
    Проверяем что изменений не больше MAX_DELTA_PCT%.
    Защита от применения «мусорного» обновления.
    """
    if not old_set:
        return True   # Первый запуск

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
# КРИТИЧНО: flush set + add element в одном nft -f = одна транзакция без утечки
# Таблица: inet vpn (не inet filter!)
# =============================================================================
def write_nftables_static(networks: list[ipaddress.IPv4Network]) -> None:
    """Атомарный шаблон: flush set → add element → nft -f."""
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
        # Разбиваем на чанки по 500 (nft может не принять один гигантский add element)
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
# Таблица в nftset: inet#vpn (не inet#filter!)
# VPS upstream DNS: VPS_TUNNEL_IP (не 1.1.1.1!)
# =============================================================================
def write_dnsmasq_config(
    domains: list[str],
    out_file: Path,
    vps_dns: str,
    header_comment: str = "Автогенерировано update-routes.py",
) -> None:
    """
    Записать dnsmasq конфиг с правильными директивами:
      server=/<domain>/<vps_dns>           — резолв через VPS
      nftset=/<domain>/4#inet#vpn#blocked_dynamic  — добавить IP в set
    """
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = [
        f"# {out_file.name} — {header_comment}",
        f"# Обновлено: {now}",
        f"# Доменов: {len(domains)}",
        "# НЕ РЕДАКТИРОВАТЬ ВРУЧНУЮ — перезаписывается update-routes.py",
        "#",
        "# Формат:",
        f"#   server=/<domain>/{vps_dns}           — upstream через VPS",
        "#   nftset=/<domain>/4#inet#vpn#blocked_dynamic — fwmark при резолве",
        "",
    ]

    for domain in sorted(set(domains)):
        domain = domain.strip().lower().lstrip("*.")
        if not domain or not _is_valid_domain(domain):
            continue
        lines.append(f"server=/{domain}/{vps_dns}")
        lines.append(f"nftset=/{domain}/4#inet#vpn#blocked_dynamic")
        lines.append("")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"{out_file.name}: {len(domains)} доменов")


# =============================================================================
# Применение изменений
# =============================================================================
def apply_nftables() -> bool:
    """nft -f — атомарное применение blocked_static."""
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
    """SIGHUP dnsmasq — безопасная перезагрузка конфига."""
    try:
        result = subprocess.run(
            ["systemctl", "kill", "-s", "SIGHUP", "dnsmasq"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("dnsmasq перезагружен (SIGHUP)")
        else:
            log.warning(f"dnsmasq reload: {result.stderr.strip()}")
    except Exception as exc:
        log.warning(f"dnsmasq reload failed: {exc}")


# =============================================================================
# Вычисление diff и hash
# =============================================================================
def compute_diff(old_lines: list[str], new_lines: list[str]) -> dict:
    old_set = {l for l in old_lines if l and not l.startswith("#")}
    new_set = {l for l in new_lines if l and not l.startswith("#")}
    added   = new_set - old_set
    removed = old_set - new_set
    return {
        "added":   sorted(added)[:20],    # Показываем первые 20
        "removed": sorted(removed)[:20],
        "added_total":   len(added),
        "removed_total": len(removed),
        "old_total": len(old_set),
        "new_total": len(new_set),
    }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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

    # ── Загружаем предыдущий combined.cidr ────────────────────────────────────
    old_lines: list[str] = []
    if COMBINED_CIDR.exists():
        old_lines = COMBINED_CIDR.read_text().splitlines()

    old_hash = HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""

    # ── Загрузка всех источников (параллельно) ────────────────────────────────
    all_networks, all_domains = fetch_all_sources()

    # Добавляем статические AS-блоки CDN
    for cidr in CDN_SUBNETS:
        net = _parse_network(cidr)
        if net:
            all_networks.add(net)
    log.info(f"CDN блоки: {len(CDN_SUBNETS)} добавлено")

    # Добавляем статические домены
    all_domains.update(STATIC_BLOCKED_DOMAINS)

    # Загружаем ручные списки
    manual_networks, manual_domains = load_manual_vpn()
    all_networks.update(manual_networks)

    if not all_networks:
        log.error("Нет данных для обновления!")
        send_telegram("❌ *update-routes.py*: нет данных из всех источников")
        sys.exit(1)

    log.info(f"Всего до агрегации: {len(all_networks)} сетей, {len(all_domains)} доменов")

    # ── Агрегация: полный set для nft blocked_static ───────────────────────────
    log.info("Агрегация nft blocked_static (без лимита)...")
    nft_networks = aggregate_networks(all_networks)
    log.info(f"nft blocked_static: {len(all_networks)} → {len(nft_networks)} после агрегации")

    # ── Агрегация: ≤500 для AllowedIPs (combined.cidr) ────────────────────────
    log.info(f"Агрегация AllowedIPs (лимит {MAX_CIDR_ALLOWED_IPS})...")
    allowed_networks = reduce_to_limit(nft_networks, MAX_CIDR_ALLOWED_IPS)
    log.info(f"AllowedIPs: {len(allowed_networks)} записей")

    if len(allowed_networks) > 50:
        log.info("QR-коды будут недоступны (>50 AllowedIPs) — отправлять .conf файлами")

    # ── Дельта-проверка ────────────────────────────────────────────────────────
    new_cidr_lines = [str(n) for n in allowed_networks]
    if not validate_delta(
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

    # ── Проверяем изменился ли результат ─────────────────────────────────────
    combined_content = (
        f"# combined.cidr — AllowedIPs для WireGuard клиентов\n"
        f"# Обновлено: {datetime.now(timezone.utc).isoformat()}\n"
        f"# Записей: {len(allowed_networks)}\n"
        f"# nft blocked_static: {len(nft_networks)} (полный)\n"
        + "\n".join(new_cidr_lines)
        + "\n"
    )
    new_hash = content_hash(combined_content)
    changed = (new_hash != old_hash)

    if not changed and not force:
        log.info("Маршруты не изменились — обновление не требуется (--force для принудительного)")
        return

    if dry_run:
        log.info(f"[DRY-RUN] nft blocked_static: {len(nft_networks)} записей")
        log.info(f"[DRY-RUN] AllowedIPs (combined.cidr): {len(allowed_networks)} записей")
        log.info(f"[DRY-RUN] Доменов (dnsmasq): {len(all_domains)}")
        log.info(f"[DRY-RUN] Изменено: {changed}")
        return

    # ── Запись файлов ─────────────────────────────────────────────────────────

    # 1. combined.cidr (AllowedIPs, ≤500)
    COMBINED_CIDR.write_text(combined_content, encoding="utf-8")
    HASH_FILE.write_text(new_hash, encoding="utf-8")
    log.info(f"combined.cidr: {len(allowed_networks)} записей")

    # 2. nftables-blocked-static.conf (полный, атомарный)
    write_nftables_static(nft_networks)

    # 3. dnsmasq vpn-domains.conf (из баз РКН + статические)
    write_dnsmasq_config(
        list(all_domains),
        DNSMASQ_DOMAINS,
        VPS_TUNNEL_IP,
        header_comment="Базы РКН + статические домены",
    )

    # 4. dnsmasq vpn-force.conf (только ручные домены)
    write_dnsmasq_config(
        list(manual_domains),
        DNSMASQ_FORCE,
        VPS_TUNNEL_IP,
        header_comment="manual-vpn.txt (добавлены через /vpn add)",
    )

    # ── Применение (только root) ───────────────────────────────────────────────
    if os.geteuid() == 0:
        apply_nftables()
        reload_dnsmasq()
    else:
        log.warning("Не root — применение nftables/dnsmasq пропущено")

    # ── Сигнал watchdog → авторассылка конфигов ───────────────────────────────
    if changed:
        # Файл-маркер для watchdog (polling)
        (ROUTES_DIR / "routes-updated").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
        # HTTP сигнал watchdog
        notify_watchdog_routes_updated()

    # ── Итоговый алерт ────────────────────────────────────────────────────────
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

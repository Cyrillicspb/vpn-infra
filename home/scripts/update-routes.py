#!/usr/bin/env python3
"""
update-routes.py — Обновление маршрутов из баз РКН

Источники:
- antifilter.download
- community.antifilter.download
- iplist.opencck.org
- github.com/zapret-info/z-i
- Статический список AS-подсетей CDN
- /etc/vpn-routes/manual-vpn.txt

Вывод:
- /etc/vpn-routes/combined.cidr (агрегированный, ≤500 записей)
- /etc/nftables-blocked-static.conf (nft set для blocked_static)
- /etc/dnsmasq.d/vpn-domains.conf (nftset=/ записи)

Требует: flock /var/run/vpn-routes.lock (cron обеспечивает)
"""
import hashlib
import ipaddress
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ROUTES_DIR = Path("/etc/vpn-routes")
COMBINED_CIDR = ROUTES_DIR / "combined.cidr"
NFTABLES_STATIC = Path("/etc/nftables-blocked-static.conf")
DNSMASQ_DOMAINS = Path("/etc/dnsmasq.d/vpn-domains.conf")
CACHE_DIR = ROUTES_DIR / "cache"
ALERT_CACHE_AGE_DAYS = 3
MAX_ALLOWED_IPS = 500  # Лимит для клиентских конфигов

# Telegram для алертов
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------
SOURCES = {
    "antifilter_ip": "https://antifilter.download/list/ip.lst",
    "antifilter_community": "https://community.antifilter.download/list/ip.lst",
    "zapret_info": "https://raw.githubusercontent.com/nicehash/nicehash-calculator/master/bypass/russia-blacklist.txt",
}

# Постоянные AS-подсети CDN (широкий забор — не убираются самообучением)
CDN_SUBNETS = [
    # Google
    "8.8.8.8/32", "8.8.4.4/32", "142.250.0.0/15", "172.217.0.0/16",
    "74.125.0.0/16", "64.233.160.0/19", "66.249.80.0/20",
    # Cloudflare
    "1.1.1.1/32", "1.0.0.1/32", "104.16.0.0/12", "172.64.0.0/13",
    "131.0.72.0/22", "103.21.244.0/22", "103.22.200.0/22",
    # Meta/Facebook
    "31.13.24.0/21", "157.240.0.0/16", "185.60.216.0/22",
    # Akamai
    "23.0.0.0/12",
    # DNS
    "8.8.8.8/32", "8.8.4.4/32", "1.1.1.1/32", "1.0.0.1/32",
]


def send_telegram(message: str):
    """Отправка алерта в Telegram."""
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": ADMIN_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram недоступен: {e}")


def fetch_url(url: str, cache_key: str, timeout: int = 30) -> list[str]:
    """
    Скачать список IP/CIDR с URL.
    При недоступности источника → предыдущая кэшированная версия.
    Алерт если кэш > 3 дней.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}.txt"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vpn-routes-updater/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read().decode("utf-8", errors="ignore")

        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]

        # Валидация
        if len(lines) < 100:
            raise ValueError(f"Слишком мало строк: {len(lines)}")

        # Сохраняем в кэш
        cache_file.write_text("\n".join(lines))
        logger.info(f"Загружено {len(lines)} записей из {cache_key}")
        return lines

    except Exception as e:
        logger.warning(f"Ошибка загрузки {url}: {e}")

        if cache_file.exists():
            # Проверяем возраст кэша
            age_days = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 86400
            if age_days > ALERT_CACHE_AGE_DAYS:
                msg = f"⚠️ Кэш маршрутов {cache_key} устарел: {age_days:.1f} дней"
                logger.warning(msg)
                send_telegram(msg)

            lines = [l.strip() for l in cache_file.read_text().splitlines() if l.strip()]
            logger.info(f"Используем кэш {cache_key}: {len(lines)} записей")
            return lines
        else:
            logger.error(f"Кэш {cache_key} не найден, пропуск источника")
            return []


def parse_ip_list(lines: list[str]) -> set[str]:
    """Парсинг списка IP/CIDR, нормализация."""
    result = set()
    for line in lines:
        line = line.split("#")[0].strip()
        if not line:
            continue
        # Убираем маску /32 для хостов если добавим обратно как /32
        try:
            net = ipaddress.ip_network(line, strict=False)
            if net.version == 4:  # Только IPv4
                result.add(str(net))
        except ValueError:
            # Может быть домен или другой формат
            pass
    return result


def aggregate_cidrs(cidrs: set[str]) -> list[str]:
    """Агрегация CIDR-блоков для уменьшения количества записей."""
    if not cidrs:
        return []

    try:
        networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]
        aggregated = list(ipaddress.collapse_addresses(networks))
        result = [str(n) for n in aggregated]
        logger.info(f"Агрегировано: {len(cidrs)} → {len(result)} записей")
        return sorted(result)
    except Exception as e:
        logger.error(f"Ошибка агрегации: {e}")
        return sorted(cidrs)


def validate_delta(old_cidrs: set[str], new_cidrs: set[str]) -> bool:
    """
    Проверка дельты изменений.
    Если > 50% изменений — подозрительно, не применяем.
    """
    if not old_cidrs:
        return True  # Первый запуск

    added = len(new_cidrs - old_cidrs)
    removed = len(old_cidrs - new_cidrs)
    total = len(old_cidrs)

    delta_pct = (added + removed) / max(total, 1) * 100
    logger.info(f"Дельта: +{added} -{removed} = {delta_pct:.1f}%")

    if delta_pct > 50:
        msg = f"⚠️ Большая дельта маршрутов: {delta_pct:.1f}%. Обновление не применено."
        logger.warning(msg)
        send_telegram(msg)
        return False
    return True


def write_nftables_set(cidrs: list[str]):
    """Запись blocked_static в nft формате."""
    lines = [
        "# Автогенерировано update-routes.py",
        "# ПРИМЕНЯТЬ: nft -f /etc/nftables-blocked-static.conf",
        "",
        "table inet filter {",
        "    set blocked_static {",
        "        type ipv4_addr",
        "        flags interval",
        "        elements = {",
    ]

    for cidr in cidrs:
        lines.append(f"            {cidr},")

    lines += [
        "        }",
        "    }",
        "}",
    ]

    NFTABLES_STATIC.write_text("\n".join(lines))
    logger.info(f"nftables blocked_static: {len(cidrs)} записей")


def write_dnsmasq_config(domains: list[str]):
    """Запись dnsmasq nftset конфига."""
    lines = [
        "# vpn-domains.conf — автогенерировано update-routes.py",
        f"# Обновлено: {datetime.now().isoformat()}",
        "",
    ]

    for domain in sorted(domains):
        lines.append(f"server=/{domain}/1.1.1.1")
        lines.append(f"nftset=/{domain}/4#inet#filter#blocked_dynamic")

    DNSMASQ_DOMAINS.write_text("\n".join(lines))
    logger.info(f"dnsmasq: {len(domains)} доменов")


def apply_nftables():
    """Атомарное применение nftables правил."""
    result = subprocess.run(
        ["nft", "-f", str(NFTABLES_STATIC)],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        logger.error(f"nft -f ошибка: {result.stderr}")
        return False
    logger.info("nftables blocked_static применены")
    return True


def reload_dnsmasq():
    """Перезагрузка dnsmasq (SIGHUP — безопасна)."""
    result = subprocess.run(["systemctl", "kill", "-s", "SIGHUP", "dnsmasq"], timeout=10)
    if result.returncode == 0:
        logger.info("dnsmasq перезагружен")
    else:
        logger.warning("Не удалось перезагрузить dnsmasq")


def main():
    logger.info("=== Обновление маршрутов ===")
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)

    # Загружаем предыдущий combined.cidr для дельта-проверки
    old_cidrs = set()
    if COMBINED_CIDR.exists():
        old_cidrs = {l.strip() for l in COMBINED_CIDR.read_text().splitlines()
                     if l.strip() and not l.startswith("#")}

    # Собираем IP из всех источников
    all_cidrs: set[str] = set(CDN_SUBNETS)

    for source_key, url in SOURCES.items():
        try:
            lines = fetch_url(url, source_key)
            cidrs = parse_ip_list(lines)
            all_cidrs.update(cidrs)
            logger.info(f"[{source_key}] добавлено {len(cidrs)} CIDR")
        except Exception as e:
            logger.error(f"Источник {source_key}: {e}")

    # Загружаем manual-vpn.txt
    manual_vpn = ROUTES_DIR / "manual-vpn.txt"
    if manual_vpn.exists():
        for line in manual_vpn.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    ipaddress.ip_network(line, strict=False)
                    all_cidrs.add(line)
                except ValueError:
                    pass  # Домен — обрабатывается в dnsmasq

    if not all_cidrs:
        logger.error("Нет данных для обновления")
        send_telegram("⚠️ Обновление маршрутов: нет данных!")
        sys.exit(1)

    # Агрегация
    aggregated = aggregate_cidrs(all_cidrs)

    # Проверка дельты
    if not validate_delta(old_cidrs, set(aggregated)):
        sys.exit(1)

    # Запись combined.cidr
    combined_content = "\n".join([
        "# combined.cidr — автогенерировано update-routes.py",
        f"# Обновлено: {datetime.now().isoformat()}",
        f"# Записей: {len(aggregated)}",
        "",
    ] + aggregated)
    COMBINED_CIDR.write_text(combined_content)
    logger.info(f"combined.cidr: {len(aggregated)} записей")

    # Предупреждение о превышении лимита
    if len(aggregated) > MAX_ALLOWED_IPS:
        logger.warning(f"Количество CIDR ({len(aggregated)}) превышает лимит {MAX_ALLOWED_IPS}!")
        logger.warning("QR-коды не будут генерироваться, отправлять .conf файлами")

    # Запись nftables
    write_nftables_set(aggregated)

    # Применение nftables
    if os.geteuid() == 0:
        apply_nftables()

    # dnsmasq (домены из manual-vpn.txt)
    manual_domains = []
    if manual_vpn.exists():
        for line in manual_vpn.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    ipaddress.ip_network(line, strict=False)
                except ValueError:
                    manual_domains.append(line)

    if manual_domains:
        write_dnsmasq_config(manual_domains)
        if os.geteuid() == 0:
            reload_dnsmasq()

    # Проверка хеша (изменились ли маршруты?)
    new_hash = hashlib.md5(combined_content.encode()).hexdigest()
    hash_file = ROUTES_DIR / "combined.hash"
    old_hash = hash_file.read_text().strip() if hash_file.exists() else ""

    if new_hash != old_hash:
        hash_file.write_text(new_hash)
        logger.info("Маршруты изменились — требуется рассылка конфигов")
        # Сигнализируем watchdog через файл-маркер
        (ROUTES_DIR / "routes-updated").write_text(datetime.now().isoformat())
    else:
        logger.info("Маршруты не изменились")

    logger.info("=== Обновление маршрутов завершено ===")


if __name__ == "__main__":
    main()

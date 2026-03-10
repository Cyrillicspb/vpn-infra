"""
services/system.py — Системные утилиты для бота (логи, мониторинг)
"""
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


async def get_docker_status() -> dict:
    """Получить статус Docker контейнеров."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = {}
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    containers[parts[0]] = parts[1]
        return containers
    except Exception as e:
        logger.error(f"Ошибка получения статуса Docker: {e}")
        return {}


async def get_service_logs(service: str, lines: int = 50) -> str:
    """Получить логи systemd сервиса."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout or result.stderr or "(нет логов)"
    except Exception as e:
        return f"Ошибка: {e}"


async def get_disk_info() -> dict:
    """Информация о дисковом пространстве."""
    try:
        import psutil
        disk = psutil.disk_usage("/")
        return {
            "total_gb": round(disk.total / 1024**3, 1),
            "used_gb": round(disk.used / 1024**3, 1),
            "free_gb": round(disk.free / 1024**3, 1),
            "percent": disk.percent,
        }
    except Exception:
        return {}

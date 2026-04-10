"""
services/system.py — Системные утилиты для бота (логи, мониторинг)
"""
import logging
import subprocess

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
    """Получить логи host systemd сервиса через watchdog API."""
    try:
        from config import config
        from services.watchdog_client import WatchdogClient

        payload = await WatchdogClient(
            config.watchdog_url,
            config.watchdog_token,
        ).get_systemd_logs(service, lines)
        return str(payload.get("text") or "(нет логов)")
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

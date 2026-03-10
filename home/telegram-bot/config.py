"""
config.py — Конфигурация Telegram-бота
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    admin_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""))

    # Watchdog API
    watchdog_url: str = field(default_factory=lambda: os.getenv("WATCHDOG_URL", "http://localhost:8080"))
    watchdog_token: str = field(default_factory=lambda: os.getenv("WATCHDOG_API_TOKEN", ""))

    # База данных
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "/app/data/vpn_bot.db"))

    # Лимиты
    device_limit_per_client: int = field(
        default_factory=lambda: int(os.getenv("DEVICE_LIMIT_PER_CLIENT", "5"))
    )

    # FSM timeout (минуты)
    fsm_timeout_minutes: int = 10

    # QR-код только если AllowedIPs <= 50
    qr_max_allowed_ips: int = 50

    # Дебаунс рассылки конфигов (секунды)
    config_debounce_seconds: int = 300

    # Напоминание если клиент не обновил конфиг (часы)
    config_reminder_hours: int = 24

    def validate(self):
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан")
        if not self.admin_chat_id:
            raise ValueError("TELEGRAM_ADMIN_CHAT_ID не задан")
        return self


# Синглтон конфига
config = Config().validate()

"""
database.py — SQLite база данных бота (WAL mode)

Таблицы:
- clients: зарегистрированные клиенты
- domain_requests: запросы на добавление доменов
- invite_codes: одноразовые коды приглашений
- excludes: исключения подсетей per device
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL mode для лучшей конкурентности
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def init(self):
        """Инициализация схемы БД."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS clients (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id     TEXT    NOT NULL UNIQUE,
                        device_name TEXT    NOT NULL,
                        protocol    TEXT    NOT NULL DEFAULT 'awg',
                        peer_id     TEXT,
                        config_version TEXT,
                        is_admin    INTEGER NOT NULL DEFAULT 0,
                        is_disabled INTEGER NOT NULL DEFAULT 0,
                        device_limit INTEGER NOT NULL DEFAULT 5,
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE TABLE IF NOT EXISTS devices (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_id   INTEGER NOT NULL REFERENCES clients(id),
                        device_name TEXT    NOT NULL,
                        protocol    TEXT    NOT NULL DEFAULT 'awg',
                        peer_id     TEXT    UNIQUE,
                        public_key  TEXT,
                        ip_address  TEXT,
                        config_version TEXT,
                        pending_approval INTEGER NOT NULL DEFAULT 0,
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE TABLE IF NOT EXISTS domain_requests (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id     TEXT    NOT NULL,
                        domain      TEXT    NOT NULL,
                        direction   TEXT    NOT NULL,
                        status      TEXT    NOT NULL DEFAULT 'pending',
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE TABLE IF NOT EXISTS invite_codes (
                        code        TEXT    PRIMARY KEY,
                        created_by  TEXT    NOT NULL,
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                        expires_at  TEXT    NOT NULL,
                        reserved_by TEXT,
                        reserved_at TEXT,
                        used_by     TEXT,
                        used_at     TEXT
                    );

                    CREATE TABLE IF NOT EXISTS excludes (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id   INTEGER NOT NULL REFERENCES devices(id),
                        subnet      TEXT    NOT NULL,
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(device_id, subnet)
                    );

                    CREATE INDEX IF NOT EXISTS idx_clients_chat_id ON clients(chat_id);
                    CREATE INDEX IF NOT EXISTS idx_devices_client_id ON devices(client_id);
                    CREATE INDEX IF NOT EXISTS idx_domain_requests_status ON domain_requests(status);
                """)
                conn.commit()
                logger.info("БД инициализирована")
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Clients
    # -----------------------------------------------------------------------
    async def get_client_by_chat_id(self, chat_id: str) -> Optional[dict]:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def register_admin(self, chat_id: str):
        """Авторегистрация администратора."""
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO clients (chat_id, device_name, protocol, is_admin)
                    VALUES (?, 'admin', 'awg', 1)
                """, (str(chat_id),))
                conn.commit()
            finally:
                conn.close()

    async def register_client(self, chat_id: str, device_name: str, protocol: str, invite_code: str) -> dict:
        """Регистрация нового клиента."""
        async with self._lock:
            conn = self._get_connection()
            try:
                # Проверяем invite code
                code_row = conn.execute("""
                    SELECT * FROM invite_codes
                    WHERE code = ? AND used_by IS NULL
                    AND datetime(expires_at) > datetime('now')
                """, (invite_code,)).fetchone()

                if not code_row:
                    raise ValueError("Неверный или истёкший код приглашения")

                # Регистрируем клиента
                conn.execute("""
                    INSERT INTO clients (chat_id, device_name, protocol)
                    VALUES (?, ?, ?)
                """, (str(chat_id), device_name, protocol))

                # Помечаем код использованным
                conn.execute("""
                    UPDATE invite_codes
                    SET used_by = ?, used_at = datetime('now')
                    WHERE code = ?
                """, (str(chat_id), invite_code))

                conn.commit()
                return await self.get_client_by_chat_id(chat_id)
            finally:
                conn.close()

    async def get_all_clients(self) -> list:
        conn = self._get_connection()
        try:
            rows = conn.execute("SELECT * FROM clients WHERE is_admin = 0 ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def disable_client(self, chat_id: str):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("UPDATE clients SET is_disabled = 1 WHERE chat_id = ?", (chat_id,))
                conn.commit()
            finally:
                conn.close()

    async def enable_client(self, chat_id: str):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("UPDATE clients SET is_disabled = 0 WHERE chat_id = ?", (chat_id,))
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Invite codes
    # -----------------------------------------------------------------------
    async def create_invite_code(self, created_by: str, ttl_hours: int = 24) -> str:
        """Создание invite-кода."""
        import secrets
        code = secrets.token_urlsafe(16)
        expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()

        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    INSERT INTO invite_codes (code, created_by, expires_at)
                    VALUES (?, ?, ?)
                """, (code, str(created_by), expires_at))
                conn.commit()
            finally:
                conn.close()
        return code

    async def reserve_invite_code(self, code: str, reserved_by: str) -> bool:
        """Резервирование кода на 10 минут при вводе."""
        async with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute("""
                    SELECT * FROM invite_codes
                    WHERE code = ? AND used_by IS NULL
                    AND (reserved_by IS NULL OR datetime(reserved_at, '+10 minutes') < datetime('now'))
                    AND datetime(expires_at) > datetime('now')
                """, (code,)).fetchone()

                if not row:
                    return False

                conn.execute("""
                    UPDATE invite_codes
                    SET reserved_by = ?, reserved_at = datetime('now')
                    WHERE code = ?
                """, (str(reserved_by), code))
                conn.commit()
                return True
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Devices
    # -----------------------------------------------------------------------
    async def get_devices_by_client(self, chat_id: str) -> list:
        conn = self._get_connection()
        try:
            client = conn.execute("SELECT id FROM clients WHERE chat_id = ?", (str(chat_id),)).fetchone()
            if not client:
                return []
            rows = conn.execute(
                "SELECT * FROM devices WHERE client_id = ? ORDER BY created_at",
                (client["id"],)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def count_devices(self, chat_id: str) -> int:
        devices = await self.get_devices_by_client(chat_id)
        return len(devices)

    async def add_device(self, chat_id: str, device_name: str, protocol: str,
                         public_key: str = "", pending: bool = False) -> dict:
        """Добавление устройства (с опциональной модерацией)."""
        async with self._lock:
            conn = self._get_connection()
            try:
                client = conn.execute(
                    "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
                ).fetchone()
                if not client:
                    raise ValueError("Клиент не найден")

                # Выделяем IP из пула
                proto_subnet = "10.177.1." if protocol == "awg" else "10.177.3."
                used_ips = [
                    row["ip_address"] for row in
                    conn.execute("SELECT ip_address FROM devices WHERE ip_address LIKE ?",
                                 (f"{proto_subnet}%",)).fetchall()
                    if row["ip_address"]
                ]
                # Ищем свободный IP (начиная с .2)
                for i in range(2, 254):
                    candidate = f"{proto_subnet}{i}"
                    if candidate not in used_ips:
                        ip_address = candidate
                        break
                else:
                    raise ValueError(f"Пул IP {proto_subnet}0/24 исчерпан")

                conn.execute("""
                    INSERT INTO devices (client_id, device_name, protocol, public_key, ip_address, pending_approval)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (client["id"], device_name, protocol, public_key, ip_address, 1 if pending else 0))
                conn.commit()

                row = conn.execute("""
                    SELECT * FROM devices WHERE client_id = ? AND device_name = ?
                    ORDER BY id DESC LIMIT 1
                """, (client["id"], device_name)).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Domain requests
    # -----------------------------------------------------------------------
    async def create_domain_request(self, chat_id: str, domain: str, direction: str) -> int:
        async with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute("""
                    INSERT INTO domain_requests (chat_id, domain, direction)
                    VALUES (?, ?, ?)
                """, (str(chat_id), domain, direction))
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

    async def get_pending_requests(self) -> list:
        conn = self._get_connection()
        try:
            rows = conn.execute("""
                SELECT * FROM domain_requests WHERE status = 'pending' ORDER BY created_at
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def approve_request(self, request_id: int):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE domain_requests SET status = 'approved' WHERE id = ?",
                    (request_id,)
                )
                conn.commit()
            finally:
                conn.close()

    async def reject_request(self, request_id: int):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE domain_requests SET status = 'rejected' WHERE id = ?",
                    (request_id,)
                )
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Excludes
    # -----------------------------------------------------------------------
    async def add_exclude(self, device_id: int, subnet: str):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO excludes (device_id, subnet) VALUES (?, ?)",
                    (device_id, subnet)
                )
                conn.commit()
            finally:
                conn.close()

    async def remove_exclude(self, device_id: int, subnet: str):
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "DELETE FROM excludes WHERE device_id = ? AND subnet = ?",
                    (device_id, subnet)
                )
                conn.commit()
            finally:
                conn.close()

    async def get_excludes(self, device_id: int) -> list:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT subnet FROM excludes WHERE device_id = ?", (device_id,)
            ).fetchall()
            return [r["subnet"] for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Backup
    # -----------------------------------------------------------------------
    async def backup(self, backup_path: str):
        """Консистентная копия через SQLite .backup API."""
        async with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(f"VACUUM INTO ?", (backup_path,))
            except Exception:
                # Fallback: старый метод
                import shutil
                backup_conn = sqlite3.connect(backup_path)
                conn.backup(backup_conn)
                backup_conn.close()
            finally:
                conn.close()

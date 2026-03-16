"""
database.py — SQLite база данных бота (WAL mode)

Таблицы:
  clients        — зарегистрированные клиенты
  devices        — устройства клиентов (несколько на клиента)
  domain_requests — запросы на маршруты (vpn/direct)
  invite_codes   — одноразовые коды приглашений
  excludes       — исключения подсетей per device
"""
import asyncio
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Соединение
    # -----------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -----------------------------------------------------------------------
    # Инициализация схемы
    # -----------------------------------------------------------------------
    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            conn = self._conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS clients (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id       TEXT    NOT NULL UNIQUE,
                        username      TEXT,
                        first_name    TEXT,
                        is_admin      INTEGER NOT NULL DEFAULT 0,
                        is_disabled   INTEGER NOT NULL DEFAULT 0,
                        device_limit  INTEGER NOT NULL DEFAULT 5,
                        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE TABLE IF NOT EXISTS devices (
                        id               INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                        device_name      TEXT    NOT NULL,
                        protocol         TEXT    NOT NULL DEFAULT 'awg',
                        is_router        INTEGER NOT NULL DEFAULT 0,
                        peer_id          TEXT    UNIQUE,
                        public_key       TEXT,
                        private_key      TEXT,
                        ip_address       TEXT,
                        config_version   TEXT,
                        config_sent_at   TEXT,
                        pending_approval INTEGER NOT NULL DEFAULT 0,
                        created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(client_id, device_name)
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
                        device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                        subnet      TEXT    NOT NULL,
                        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(device_id, subnet)
                    );

                    CREATE INDEX IF NOT EXISTS idx_clients_chat_id
                        ON clients(chat_id);
                    CREATE INDEX IF NOT EXISTS idx_devices_client_id
                        ON devices(client_id);
                    CREATE INDEX IF NOT EXISTS idx_domain_requests_chat_id
                        ON domain_requests(chat_id);
                    CREATE INDEX IF NOT EXISTS idx_domain_requests_status
                        ON domain_requests(status);
                """)
                conn.commit()
                # Миграция: добавить недостающие колонки clients
                cols = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
                if "first_name" not in cols:
                    conn.execute("ALTER TABLE clients ADD COLUMN first_name TEXT")
                    conn.commit()
                if "username" not in cols:
                    conn.execute("ALTER TABLE clients ADD COLUMN username TEXT")
                    conn.commit()
                if "is_admin" not in cols:
                    conn.execute("ALTER TABLE clients ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
                    conn.commit()
                # Миграция: добавить is_router если отсутствует
                dev_cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
                if "is_router" not in dev_cols:
                    conn.execute("ALTER TABLE devices ADD COLUMN is_router INTEGER NOT NULL DEFAULT 0")
                    conn.commit()
                logger.info("БД инициализирована")
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Clients
    # -----------------------------------------------------------------------
    async def get_client(self, chat_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # Алиас для совместимости
    async def get_client_by_chat_id(self, chat_id: str) -> Optional[dict]:
        return await self.get_client(chat_id)

    async def register_admin(self, chat_id: str, username: str = "") -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO clients (chat_id, username, is_admin) VALUES (?, ?, 1)",
                    (str(chat_id), username),
                )
                conn.commit()
            finally:
                conn.close()

    async def register_client(
        self, chat_id: str, username: str, invite_code: str, first_name: str = ""
    ) -> dict:
        """Регистрация нового клиента по invite-коду."""
        async with self._lock:
            conn = self._conn()
            try:
                code_row = conn.execute("""
                    SELECT * FROM invite_codes
                    WHERE code = ?
                      AND used_by IS NULL
                      AND datetime(expires_at) > datetime('now')
                      AND (
                          reserved_by IS NULL
                          OR reserved_by = ?
                          OR datetime(reserved_at, '+10 minutes') < datetime('now')
                      )
                """, (invite_code, str(chat_id))).fetchone()

                if not code_row:
                    raise ValueError("Неверный, использованный или истёкший код")

                conn.execute(
                    "INSERT INTO clients (chat_id, username, first_name) VALUES (?, ?, ?)",
                    (str(chat_id), username, first_name),
                )
                conn.execute("""
                    UPDATE invite_codes
                    SET used_by = ?, used_at = datetime('now')
                    WHERE code = ?
                """, (str(chat_id), invite_code))
                conn.commit()

                row = conn.execute(
                    "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
                ).fetchone()
                return dict(row)
            finally:
                conn.close()

    async def get_all_clients(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clients ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def set_client_disabled(self, chat_id: str, disabled: bool) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE clients SET is_disabled = ? WHERE chat_id = ?",
                    (1 if disabled else 0, str(chat_id)),
                )
                conn.commit()
            finally:
                conn.close()

    async def set_client_limit(self, chat_id: str, limit: int) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE clients SET device_limit = ? WHERE chat_id = ?",
                    (limit, str(chat_id)),
                )
                conn.commit()
            finally:
                conn.close()

    async def find_client_by_name(self, name: str) -> Optional[dict]:
        """Поиск клиента по username или chat_id (для /client <имя>)."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT c.* FROM clients c WHERE c.username = ? OR c.chat_id = ?",
                (name, name),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Invite codes
    # -----------------------------------------------------------------------
    async def create_invite_code(self, created_by: str, ttl_hours: int = 24) -> str:
        code = secrets.token_urlsafe(16)
        expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO invite_codes (code, created_by, expires_at) VALUES (?, ?, ?)",
                    (code, str(created_by), expires_at),
                )
                conn.commit()
            finally:
                conn.close()
        return code

    async def reserve_invite_code(self, code: str, reserved_by: str) -> bool:
        async with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("""
                    SELECT * FROM invite_codes
                    WHERE code = ?
                      AND used_by IS NULL
                      AND datetime(expires_at) > datetime('now')
                      AND (
                          reserved_by IS NULL
                          OR datetime(reserved_at, '+10 minutes') < datetime('now')
                      )
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

    async def release_invite_reservation(self, chat_id: str) -> None:
        """Снять резерв invite-кода при таймауте FSM."""
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE invite_codes SET reserved_by = NULL, reserved_at = NULL "
                    "WHERE reserved_by = ? AND used_by IS NULL",
                    (str(chat_id),),
                )
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Devices
    # -----------------------------------------------------------------------
    async def get_devices(self, chat_id: str) -> list[dict]:
        conn = self._conn()
        try:
            client = conn.execute(
                "SELECT id FROM clients WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
            if not client:
                return []
            rows = conn.execute(
                "SELECT * FROM devices WHERE client_id = ? ORDER BY created_at",
                (client["id"],),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # Алиас
    async def get_devices_by_client(self, chat_id: str) -> list[dict]:
        return await self.get_devices(chat_id)

    async def count_devices(self, chat_id: str) -> int:
        return len(await self.get_devices(chat_id))

    async def get_device_by_name(self, chat_id: str, device_name: str) -> Optional[dict]:
        conn = self._conn()
        try:
            client = conn.execute(
                "SELECT id FROM clients WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
            if not client:
                return None
            row = conn.execute(
                "SELECT * FROM devices WHERE client_id = ? AND device_name = ?",
                (client["id"], device_name),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_device_by_id(self, device_id: int) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def add_device(
        self,
        chat_id: str,
        device_name: str,
        protocol: str,
        public_key: str = "",
        private_key: str = "",
        pending: bool = False,
        is_router: bool = False,
    ) -> dict:
        async with self._lock:
            conn = self._conn()
            try:
                client = conn.execute(
                    "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
                ).fetchone()
                if not client:
                    raise ValueError("Клиент не найден")

                # IP пул
                subnet = "10.177.1." if protocol == "awg" else "10.177.3."
                used_ips = {
                    r["ip_address"]
                    for r in conn.execute(
                        "SELECT ip_address FROM devices WHERE ip_address LIKE ?",
                        (f"{subnet}%",),
                    ).fetchall()
                    if r["ip_address"]
                }
                ip_address = None
                for i in range(2, 254):
                    candidate = f"{subnet}{i}"
                    if candidate not in used_ips:
                        ip_address = candidate
                        break
                if not ip_address:
                    raise ValueError(f"IP пул {subnet}0/24 исчерпан")

                conn.execute("""
                    INSERT INTO devices
                      (client_id, device_name, protocol, is_router, public_key, private_key,
                       ip_address, pending_approval)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    client["id"], device_name, protocol, 1 if is_router else 0,
                    public_key, private_key,
                    ip_address, 1 if pending else 0,
                ))
                conn.commit()

                row = conn.execute("""
                    SELECT * FROM devices WHERE client_id = ? AND device_name = ?
                    ORDER BY id DESC LIMIT 1
                """, (client["id"], device_name)).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

    async def approve_device(self, device_id: int) -> Optional[dict]:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE devices SET pending_approval = 0 WHERE id = ?",
                    (device_id,),
                )
                conn.commit()
                row = conn.execute(
                    """SELECT d.*, c.chat_id FROM devices d
                       JOIN clients c ON c.id = d.client_id
                       WHERE d.id = ?""", (device_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    async def delete_device(self, device_id: int) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
                conn.commit()
            finally:
                conn.close()

    async def update_device_keys(self, device_id: int, private_key: str, public_key: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE devices SET private_key = ?, public_key = ? WHERE id = ?",
                    (private_key, public_key, device_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def update_config_version(self, device_id: int, version: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE devices SET config_version = ?, config_sent_at = datetime('now') "
                    "WHERE id = ?",
                    (version, device_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def get_pending_devices(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT d.*, c.chat_id, c.username
                FROM devices d
                JOIN clients c ON c.id = d.client_id
                WHERE d.pending_approval = 1
                ORDER BY d.created_at
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_all_devices(self) -> list[dict]:
        """Все активные устройства со всех клиентов (для broadcast)."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT d.*, c.chat_id, c.is_disabled
                FROM devices d
                JOIN clients c ON c.id = d.client_id
                WHERE d.pending_approval = 0 AND c.is_disabled = 0
                ORDER BY c.chat_id, d.device_name
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_stale_configs(self, hours: int = 24) -> list[dict]:
        """Устройства, которым отправлен конфиг, но не подтверждено обновление (>hours назад)."""
        conn = self._conn()
        try:
            rows = conn.execute(f"""
                SELECT d.*, c.chat_id
                FROM devices d
                JOIN clients c ON c.id = d.client_id
                WHERE d.config_sent_at IS NOT NULL
                  AND datetime(d.config_sent_at, '+{hours} hours') < datetime('now')
                  AND c.is_disabled = 0
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Domain requests
    # -----------------------------------------------------------------------
    async def create_domain_request(
        self, chat_id: str, domain: str, direction: str
    ) -> int:
        async with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO domain_requests (chat_id, domain, direction) VALUES (?, ?, ?)",
                    (str(chat_id), domain, direction),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    async def get_pending_requests(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM domain_requests WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_requests_by_client(self, chat_id: str) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM domain_requests WHERE chat_id = ? ORDER BY created_at DESC LIMIT 20",
                (str(chat_id),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def approve_request(self, request_id: int) -> Optional[dict]:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE domain_requests SET status = 'approved' WHERE id = ?",
                    (request_id,),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM domain_requests WHERE id = ?", (request_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    async def get_request_by_id(self, request_id: int) -> Optional[dict]:
        async with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM domain_requests WHERE id = ?", (request_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    async def reject_request(self, request_id: int) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE domain_requests SET status = 'rejected' WHERE id = ?",
                    (request_id,),
                )
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Excludes
    # -----------------------------------------------------------------------
    async def add_exclude(self, device_id: int, subnet: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO excludes (device_id, subnet) VALUES (?, ?)",
                    (device_id, subnet),
                )
                conn.commit()
            finally:
                conn.close()

    async def remove_exclude(self, device_id: int, subnet: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM excludes WHERE device_id = ? AND subnet = ?",
                    (device_id, subnet),
                )
                conn.commit()
            finally:
                conn.close()

    async def get_excludes(self, device_id: int) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT subnet FROM excludes WHERE device_id = ?", (device_id,)
            ).fetchall()
            return [{"subnet": r["subnet"]} for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Backup
    # -----------------------------------------------------------------------
    async def backup(self, backup_path: str) -> None:
        """Консистентная копия через SQLite .backup API."""
        async with self._lock:
            conn = self._conn()
            try:
                backup_conn = sqlite3.connect(backup_path)
                conn.backup(backup_conn)
                backup_conn.close()
                logger.info(f"БД скопирована в {backup_path}")
            finally:
                conn.close()

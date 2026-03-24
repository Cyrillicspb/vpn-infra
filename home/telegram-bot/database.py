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
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from services.crypto import decrypt_key, encrypt_key

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
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    # -----------------------------------------------------------------------
    # Crypto helpers
    # -----------------------------------------------------------------------
    @staticmethod
    def _decrypt_device(d: dict) -> dict:
        """Расшифровать private_key в словаре устройства (если зашифрован)."""
        if d.get("private_key"):
            d["private_key"] = decrypt_key(d["private_key"])
        return d

    @staticmethod
    def _decrypt_invite(d: dict) -> dict:
        """Расшифровать awg_privkey/wg_privkey в словаре инвайта (если зашифрованы)."""
        if d.get("awg_privkey"):
            d["awg_privkey"] = decrypt_key(d["awg_privkey"])
        if d.get("wg_privkey"):
            d["wg_privkey"] = decrypt_key(d["wg_privkey"])
        return d

    # -----------------------------------------------------------------------
    # Инициализация схемы
    # -----------------------------------------------------------------------
    async def init(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        # Ensure restrictive permissions before first write
        db_file.touch(exist_ok=True)
        os.chmod(self.db_path, 0o600)
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
                # Миграция: пересоздать clients если старая схема (device_name NOT NULL)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
                if "device_name" in cols:
                    # Старая схема: 1 строка на устройство, device_name обязателен —
                    # мешает регистрации по новой схеме. Пересоздаём только если таблица пустая.
                    has_data = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] > 0
                    if has_data:
                        # Данные есть — только удаляем колонку через пересоздание с сохранением данных
                        try:
                            conn.executescript("""
                                CREATE TABLE clients_new (
                                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                                    chat_id       TEXT    NOT NULL UNIQUE,
                                    username      TEXT,
                                    first_name    TEXT,
                                    is_admin      INTEGER NOT NULL DEFAULT 0,
                                    is_disabled   INTEGER NOT NULL DEFAULT 0,
                                    device_limit  INTEGER NOT NULL DEFAULT 5,
                                    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                                );
                                INSERT INTO clients_new (id, chat_id, username, first_name, is_admin, is_disabled, device_limit, created_at)
                                    SELECT id, chat_id, username, first_name,
                                           COALESCE(is_admin, 0), COALESCE(is_disabled, 0),
                                           COALESCE(device_limit, 5), COALESCE(created_at, datetime('now'))
                                    FROM clients;
                                DROP TABLE clients;
                                ALTER TABLE clients_new RENAME TO clients;
                                CREATE INDEX IF NOT EXISTS idx_clients_chat_id ON clients(chat_id);
                            """)
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                    else:
                        # Таблица пустая — просто пересоздаём
                        try:
                            conn.executescript("""
                            DROP TABLE IF EXISTS clients;
                            CREATE TABLE clients (
                                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                                chat_id       TEXT    NOT NULL UNIQUE,
                                username      TEXT,
                                first_name    TEXT,
                                is_admin      INTEGER NOT NULL DEFAULT 0,
                                is_disabled   INTEGER NOT NULL DEFAULT 0,
                                device_limit  INTEGER NOT NULL DEFAULT 5,
                                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                            );
                            CREATE INDEX IF NOT EXISTS idx_clients_chat_id ON clients(chat_id);
                        """)
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
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
                # Миграция: bootstrap invite поля
                inv_cols = {r[1] for r in conn.execute("PRAGMA table_info(invite_codes)")}
                if "awg_peer_id" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN awg_peer_id TEXT")
                    conn.commit()
                if "wg_peer_id" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN wg_peer_id TEXT")
                    conn.commit()
                if "is_bootstrap" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN is_bootstrap INTEGER NOT NULL DEFAULT 0")
                    conn.commit()
                if "awg_ip" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN awg_ip TEXT")
                    conn.commit()
                if "wg_ip" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN wg_ip TEXT")
                    conn.commit()
                if "awg_privkey" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN awg_privkey TEXT")
                    conn.commit()
                if "wg_privkey" not in inv_cols:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN wg_privkey TEXT")
                    conn.commit()
                # Миграция: multi-admin поля
                cl_cols = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
                if "admin_added_by" not in cl_cols:
                    conn.execute("ALTER TABLE clients ADD COLUMN admin_added_by TEXT")
                    conn.commit()
                inv_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(invite_codes)")}
                if "grants_admin" not in inv_cols2:
                    conn.execute("ALTER TABLE invite_codes ADD COLUMN grants_admin INTEGER NOT NULL DEFAULT 0")
                    conn.commit()
                # Миграция: платформа устройства
                dev_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
                if "platform" not in dev_cols2:
                    conn.execute("ALTER TABLE devices ADD COLUMN platform TEXT")
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

    async def register_admin(self, chat_id: str, username: str = "", first_name: str = "") -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO clients (chat_id, username, first_name, is_admin) VALUES (?, ?, ?, 1)",
                    (str(chat_id), username, first_name),
                )
                conn.execute(
                    "UPDATE clients SET is_admin = 1 WHERE chat_id = ? AND is_admin = 0",
                    (str(chat_id),),
                )
                conn.commit()
            finally:
                conn.close()

    async def update_client_info(self, chat_id: str, username: str, first_name: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE clients SET username=?, first_name=? WHERE chat_id=?",
                    (username, first_name, str(chat_id)),
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

                grants_admin = bool(code_row["grants_admin"]) if "grants_admin" in code_row.keys() else False
                created_by = code_row["created_by"]

                conn.execute(
                    "INSERT INTO clients (chat_id, username, first_name) VALUES (?, ?, ?)",
                    (str(chat_id), username, first_name),
                )
                if grants_admin:
                    conn.execute(
                        "UPDATE clients SET is_admin = 1, admin_added_by = ? WHERE chat_id = ?",
                        (created_by, str(chat_id)),
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

    async def is_admin(self, chat_id: str) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT is_admin FROM clients WHERE chat_id = ? AND is_admin = 1",
                (str(chat_id),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    async def set_admin(self, chat_id: str, is_admin_flag: bool, added_by: str | None = None) -> bool:
        async with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE clients SET is_admin = ?, admin_added_by = ? WHERE chat_id = ?",
                    (1 if is_admin_flag else 0, added_by, str(chat_id)),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def get_all_admins(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, chat_id, username, first_name, is_admin, created_at, admin_added_by "
                "FROM clients WHERE is_admin = 1 ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
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
    async def create_invite_code(self, created_by: str, ttl_hours: int = 24, grants_admin: bool = False) -> str:
        code = secrets.token_urlsafe(16)
        expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO invite_codes (code, created_by, expires_at, grants_admin) VALUES (?, ?, ?, ?)",
                    (code, str(created_by), expires_at, 1 if grants_admin else 0),
                )
                conn.commit()
            finally:
                conn.close()
        return code

    async def create_bootstrap_invite(
        self,
        created_by: str,
        awg_peer_id: str,
        wg_peer_id: str,
        awg_ip: str,
        wg_ip: str,
        awg_privkey: str = "",
        wg_privkey: str = "",
        ttl_hours: int = 24,
    ) -> str:
        """Bootstrap-инвайт с предсозданными временными пирами. TTL 24ч."""
        code = secrets.token_urlsafe(16)
        expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO invite_codes
                       (code, created_by, expires_at, is_bootstrap,
                        awg_peer_id, wg_peer_id, awg_ip, wg_ip, awg_privkey, wg_privkey)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (code, str(created_by), expires_at,
                     awg_peer_id, wg_peer_id, awg_ip, wg_ip,
                     encrypt_key(awg_privkey) if awg_privkey else awg_privkey,
                     encrypt_key(wg_privkey) if wg_privkey else wg_privkey),
                )
                conn.commit()
            finally:
                conn.close()
        return code

    async def get_invite_bootstrap_info(self, code: str) -> Optional[dict]:
        """Вернуть bootstrap-данные инвайта (peer IDs, IP). None если не bootstrap."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM invite_codes WHERE code = ? AND is_bootstrap = 1",
                (code,)
            ).fetchone()
            return self._decrypt_invite(dict(row)) if row else None
        finally:
            conn.close()

    async def get_expired_bootstrap_invites(self) -> list[dict]:
        """Вернуть истёкшие bootstrap-инвайты с peer IDs для удаления."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT * FROM invite_codes
                WHERE is_bootstrap = 1
                  AND used_by IS NULL
                  AND datetime(expires_at) <= datetime('now')
            """).fetchall()
            return [self._decrypt_invite(dict(r)) for r in rows]
        finally:
            conn.close()

    async def delete_expired_bootstrap_invites(self) -> int:
        """Удалить истёкшие bootstrap-инвайты из БД. Вернуть количество."""
        async with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("""
                    DELETE FROM invite_codes
                    WHERE is_bootstrap = 1
                      AND used_by IS NULL
                      AND datetime(expires_at) <= datetime('now')
                """)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

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
            return [self._decrypt_device(dict(r)) for r in rows]
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
            return self._decrypt_device(dict(row)) if row else None
        finally:
            conn.close()

    async def get_device_by_id(self, device_id: int) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            return self._decrypt_device(dict(row)) if row else None
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
        ip_address: Optional[str] = None,
    ) -> dict:
        async with self._lock:
            conn = self._conn()
            try:
                client = conn.execute(
                    "SELECT * FROM clients WHERE chat_id = ?", (str(chat_id),)
                ).fetchone()
                if not client:
                    raise ValueError("Клиент не найден")

                # IP пул — используем переданный IP (bootstrap) или выделяем новый
                subnet = "10.177.1." if protocol == "awg" else "10.177.3."
                if not ip_address:
                    used_ips = {
                        r["ip_address"]
                        for r in conn.execute(
                            "SELECT ip_address FROM devices WHERE ip_address LIKE ?",
                            (f"{subnet}%",),
                        ).fetchall()
                        if r["ip_address"]
                    }
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
                    public_key, encrypt_key(private_key) if private_key else private_key,
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
                return self._decrypt_device(dict(row)) if row else None
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
                    (encrypt_key(private_key) if private_key else private_key, public_key, device_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def update_device_platform(self, device_id: int, platform: str) -> None:
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE devices SET platform = ? WHERE id = ?",
                    (platform, device_id),
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
            return [self._decrypt_device(dict(r)) for r in rows]
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
            return [self._decrypt_device(dict(r)) for r in rows]
        finally:
            conn.close()

    async def get_stale_configs(self, hours: int = 24) -> list[dict]:
        """Устройства, которым отправлен конфиг, но не подтверждено обновление (>hours назад)."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT d.*, c.chat_id
                FROM devices d
                JOIN clients c ON c.id = d.client_id
                WHERE d.config_sent_at IS NOT NULL
                  AND datetime(d.config_sent_at, '+' || ? || ' hours') < datetime('now')
                  AND c.is_disabled = 0
            """, (int(hours),)).fetchall()
            return [self._decrypt_device(dict(r)) for r in rows]
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
    # Encryption migration
    # -----------------------------------------------------------------------
    async def migrate_encrypt_keys(self) -> int:
        """
        Однократная миграция: зашифровать все незашифрованные приватные ключи в БД.
        Идемпотентна — строки уже с префиксом 'enc:' пропускаются.
        Возвращает количество зашифрованных строк.
        """
        _PREFIX = "enc:"
        count = 0
        async with self._lock:
            conn = self._conn()
            try:
                # devices.private_key
                rows = conn.execute(
                    "SELECT id, private_key FROM devices WHERE private_key IS NOT NULL AND private_key != ''"
                ).fetchall()
                for row in rows:
                    if not row["private_key"].startswith(_PREFIX):
                        conn.execute(
                            "UPDATE devices SET private_key = ? WHERE id = ?",
                            (encrypt_key(row["private_key"]), row["id"]),
                        )
                        count += 1

                # invite_codes.awg_privkey
                rows = conn.execute(
                    "SELECT code, awg_privkey FROM invite_codes WHERE awg_privkey IS NOT NULL AND awg_privkey != ''"
                ).fetchall()
                for row in rows:
                    if not row["awg_privkey"].startswith(_PREFIX):
                        conn.execute(
                            "UPDATE invite_codes SET awg_privkey = ? WHERE code = ?",
                            (encrypt_key(row["awg_privkey"]), row["code"]),
                        )
                        count += 1

                # invite_codes.wg_privkey
                rows = conn.execute(
                    "SELECT code, wg_privkey FROM invite_codes WHERE wg_privkey IS NOT NULL AND wg_privkey != ''"
                ).fetchall()
                for row in rows:
                    if not row["wg_privkey"].startswith(_PREFIX):
                        conn.execute(
                            "UPDATE invite_codes SET wg_privkey = ? WHERE code = ?",
                            (encrypt_key(row["wg_privkey"]), row["code"]),
                        )
                        count += 1

                conn.commit()
                if count:
                    logger.info("migrate_encrypt_keys: зашифровано %d записей", count)
                return count
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    async def close(self) -> None:
        """Flush WAL и закрыть соединение с SQLite."""
        async with self._lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
                logger.info("Database WAL checkpointed")
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
                os.chmod(backup_path, 0o600)
                logger.info(f"БД скопирована в {backup_path}")
            finally:
                conn.close()

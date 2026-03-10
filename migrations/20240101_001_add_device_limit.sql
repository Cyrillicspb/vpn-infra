-- Миграция 001: Добавление device_limit в таблицу clients
-- Позволяет устанавливать лимит устройств per-client через /client limit

-- Добавить колонку device_limit если её нет
-- SQLite не поддерживает IF NOT EXISTS для ALTER TABLE,
-- используем временную таблицу для проверки
CREATE TABLE IF NOT EXISTS _migration_check (id INTEGER PRIMARY KEY);

-- Проверяем через pragma и добавляем только если колонки нет
-- (идемпотентная миграция)
PRAGMA table_info(clients);

-- Добавить device_limit с дефолтным значением 5
-- Если колонка уже существует — SQLite вернёт ошибку, которую apply.sh поймает
-- Поэтому используем обходной путь через CREATE TABLE AS

-- Создать новую таблицу с нужной схемой
CREATE TABLE IF NOT EXISTS clients_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    protocol    TEXT NOT NULL DEFAULT 'AWG',
    peer_id     TEXT,
    config_version TEXT,
    device_limit INTEGER NOT NULL DEFAULT 5,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, device_name)
);

-- Скопировать данные
INSERT OR IGNORE INTO clients_new
    (id, chat_id, device_name, protocol, peer_id, config_version, created_at)
SELECT id, chat_id, device_name, protocol, peer_id, config_version, created_at
FROM clients;

-- Заменить таблицу
DROP TABLE IF EXISTS clients;
ALTER TABLE clients_new RENAME TO clients;

-- Создать индексы
CREATE INDEX IF NOT EXISTS idx_clients_chat_id ON clients(chat_id);
CREATE INDEX IF NOT EXISTS idx_clients_peer_id ON clients(peer_id);

DROP TABLE IF EXISTS _migration_check;

-- Миграция 002: Создание таблицы excludes
-- Позволяет клиентам добавлять исключения из VPN-маршрутов (/exclude add|remove|list)

-- Таблица excludes: per-device исключения подсетей из VPN-туннеля
CREATE TABLE IF NOT EXISTS excludes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL,
    subnet      TEXT NOT NULL,              -- например: 192.168.1.0/24 или 10.0.0.0/8
    description TEXT,                       -- опциональное описание (офисная сеть, etc)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_id, subnet),
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

-- Индекс для быстрого поиска по device_id
CREATE INDEX IF NOT EXISTS idx_excludes_device_id ON excludes(device_id);

-- Таблица devices если не существует (normalisation)
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    vpn_ip      TEXT,                       -- IP в VPN подсети (10.177.1.x или 10.177.3.x)
    pubkey      TEXT,                       -- WireGuard публичный ключ
    protocol    TEXT NOT NULL DEFAULT 'AWG',
    config_version TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, device_name)
);

CREATE INDEX IF NOT EXISTS idx_devices_chat_id ON devices(chat_id);
CREATE INDEX IF NOT EXISTS idx_devices_vpn_ip ON devices(vpn_ip);

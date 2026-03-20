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

-- Индекс для поиска устройств по client_id (реальная схема использует client_id, не chat_id)
CREATE INDEX IF NOT EXISTS idx_devices_client_id ON devices(client_id);

-- Миграция 006: per-device маршруты через сервер
-- Позволяет клиентам добавлять IP/подсети, которые должны идти через VPN-сервер,
-- даже если по умолчанию локальные/LAN адреса у split-tunnel клиентов остаются direct.

CREATE TABLE IF NOT EXISTS server_routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    subnet      TEXT NOT NULL,              -- например: 192.168.1.200/32 или 192.168.1.0/24
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_id, subnet)
);

CREATE INDEX IF NOT EXISTS idx_server_routes_device_id ON server_routes(device_id);

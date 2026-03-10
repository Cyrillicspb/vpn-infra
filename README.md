# VPN Infrastructure v4.0

Двухуровневая самохостинговая VPN-инфраструктура для обхода DPI-фильтрации (ТСПУ/РКН).

## Возможности

- **4 стека с адаптивным failover**: VLESS+CDN → VLESS+gRPC+REALITY → VLESS+REALITY → Hysteria2
- **Split tunneling гибрид B+**: только заблокированные ресурсы через VPN
- **Управление через Telegram-бота**: для администратора и клиентов
- **Автоустановка**: один скрипт, без ручной настройки
- **Автообновление**: через git-зеркало на VPS (обходит блокировку GitHub)
- **Мульти-VPS**: балансировка и failover между несколькими VPS

## Требования

| Компонент | Минимум | Рекомендуется |
|-----------|---------|---------------|
| Домашний сервер | 4 GB RAM, 64 GB SSD | 8 GB RAM, 128 GB SSD |
| VPS | 2 vCPU, 2 GB RAM, 40 GB SSD | — |
| Роутер | Port forwarding, **белый IP** | DDNS при динамическом IP |

**Обязательно**: VPS, Telegram-бот, белый IP (не CGNAT).
**Опционально**: Cloudflare аккаунт (CDN-стек), домен.

## Быстрый старт

```bash
# На домашнем сервере (Ubuntu 24.04):
curl -fsSL https://raw.githubusercontent.com/your-repo/vpn-infra/main/setup.sh | bash
```

Или для Windows/macOS — запустите `installers/windows/install.bat` / `installers/macos/install.command`.

## Архитектура

```
Клиенты → Роутер → Домашний сервер → VPS
              AWG/WG ↑        ↑ VLESS/Hysteria2
```

Подробнее: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Документация

- [Установка](docs/INSTALL.md)
- [Оборудование](docs/HARDWARE.md)
- [Команды бота](docs/COMMANDS.md)
- [FAQ](docs/FAQ.md)
- [Устранение неполадок](docs/TROUBLESHOOTING.md)
- [Безопасность](docs/SECURITY.md)
- [Приватность](docs/PRIVACY.md)
- [Обновление](docs/UPDATE.md)
- [Восстановление](docs/DISASTER-RECOVERY.md)

## Стеки (по устойчивости)

1. **VLESS+WebSocket через Cloudflare CDN** — максимальная устойчивость
2. **VLESS+REALITY+gRPC** (cdn.jsdelivr.net) — очень устойчив
3. **VLESS+REALITY** (microsoft.com) — устойчив
4. **Hysteria2** (QUIC+Salamander) — быстрый, легче блокируется

## Лицензия

MIT

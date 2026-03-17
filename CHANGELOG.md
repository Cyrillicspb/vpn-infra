# Changelog

Все значимые изменения проекта фиксируются здесь.
Формат: [SemVer](https://semver.org/), даты UTC.

---

## [v0.1.0] — 2026-03-17 — Первая рабочая бета

### Что работает

**Ядро инфраструктуры**
- Установка в один скрипт (`setup.sh`, 57 шагов, фазы 0–5): домашний сервер + VPS
- Автоматическая генерация всех ключей и секретов
- Прогресс-бар установки с подсказками и retry per шаг
- Идемпотентность: повторный запуск безопасен (`.setup-state`)
- Установочные скрипты для Windows (`.bat`) и macOS (`.command`) с jsdelivr CDN fallback

**Туннели и протоколы**
- AmneziaWG (wg0, 10.177.1.0/24, порт 51820) — клиентский
- WireGuard (wg1, 10.177.3.0/24, порт 51821) — клиентский
- Tier-2 WireGuard туннель (wg-tier2, 10.177.2.0/30, порт 51822) — мониторинг и SSH к VPS
- Стек 1 — Cloudflare CDN (VLESS+WS через Cloudflare Workers): **работает** ✅
- Стек 2 — VLESS+XHTTP+REALITY (cdn.jsdelivr.net, порт 2083): **работает** ✅
- Стек 4 — Hysteria2 (QUIC + Salamander, UDP 443): **работает** ✅
- Адаптивный failover между стеками
- Ротация соединений (make-before-break, 30–60 мин)
- End-to-end тест пройден: AWG/WG клиент → домашний сервер → VPS → заблокированные сайты ✅

**Маршрутизация и фильтрация**
- Split tunneling Hybrid B+: AllowedIPs на клиенте (266 CIDR) + nftables fwmark на сервере
- nftables table `inet vpn`: blocked_static (36 946 правил), blocked_dynamic (timeout 24h)
- Kill switch: двойная защита — fwmark routing + nftables forward DROP
- Policy routing: table vpn (100) + table marked (200)
- dnsmasq: nftset= для blocked_dynamic, прогрев DNS-кэша, 39 доменов
- combined.cidr: 266 CIDR-записей, cron обновление 03:00 ежедневно
- Источники: antifilter.download, opencck.org, zapret-info

**Watchdog**
- Python async (aiohttp), systemd Type=notify, автозапуск tun при старте
- HTTP API на :8080 (bearer token, rate limiting, только Docker subnet)
- Мониторинг: ping, curl заблокированных сайтов, VPS heartbeat
- Адаптивный failover по стекам с детекцией деградации
- Cron failsafe: watchdog alive check каждые 5 мин

**Telegram-бот**
- Двухрежимный: admin (полный доступ) + клиенты (самообслуживание)
- Регистрация клиентов через invite-код с автоматическим получением конфига
- Команды: /status, /tunnel, /switch, /restart, /invite, /myconfig, /mydevices
- Конфиг-билдер: шаблоны .conf + AllowedIPs + QR-код
- SQLite (WAL mode) для хранения клиентов, устройств, invite-кодов
- Алерты в Telegram: туннель down, RTT, диск, внешний IP, heartbeat
- Автоматическая проверка обновлений раз в час с уведомлением в Telegram
- /graph: графики из Grafana прямо в чат (Grafana image renderer работает) ✅

**VPS**
- 3x-ui: инбаунды VLESS-XHTTP-jsdelivr (2083) + VLESS-XHTTP-microsoft (2087)
- Nginx: reverse proxy с mTLS для панелей
- Prometheus + Grafana: мониторинг, дашборды
- VPS healthcheck: cron 5 мин, алерты в Telegram
- Автоматическая настройка 3x-ui инбаундов через API

**Деплой и обслуживание**
- `deploy.sh`: snapshot → apply → smoke-тест → auto-rollback при провале
- `restore.sh`: полное восстановление из GPG-зашифрованного бэкапа
- Ежечасная проверка обновлений с уведомлением в Telegram
- Резервные копии: cron 04:00, GPG-шифрование, хранение на VPS + Telegram

### Известные проблемы и ограничения

- **Стек 3 (REALITY, microsoft.com, порт 2087)**: TCP-порт открыт на VPS, но ТСПУ блокирует на уровне TLS/XHTTP handshake — стек не работает через российских провайдеров
- **mTLS клиентские сертификаты**: CA и серверный сертификат создаются при установке; команда `/renew-cert` реализована, но скрипт `renew-mtls.sh` отсутствует — потребуется создать вручную
- **CGNAT**: не работает без реального (белого) IP или bridge mode на роутере
- **combined.cidr**: 266 CIDR-записей — покрывает основные заблокированные сервисы, редкие домены вне крупных AS могут отсутствовать до следующего обновления в 03:00

### Требования

- **Домашний сервер**: x86_64, 4+ GB RAM, 64+ GB SSD, Ubuntu Server 24.04 LTS
- **VPS**: KVM, 1+ vCPU, 1+ GB RAM, Ubuntu 24.04 LTS (рекомендуется: 2 vCPU, 2 GB RAM)
- **Роутер**: реальный (белый) IP, НЕ CGNAT; поддержка port forwarding
- **Telegram**: бот (token от @BotFather) + chat_id администратора
- **Опционально**: Cloudflare аккаунт (бесплатный) для CDN-стека

### Планируется в v0.2.0

- Стек 3: обход блокировки microsoft.com (2087) ТСПУ
- /graph команда (Grafana → Telegram PNG)
- mTLS клиентские сертификаты
- Документация по настройке клиентских устройств

---

*Формат: [Keep a Changelog](https://keepachangelog.com/)*

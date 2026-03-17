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
- Стек 2 — VLESS+XHTTP+REALITY (cdn.jsdelivr.net, порт 2083): **работает, трафик идёт через VPS**
- Ротация соединений (make-before-break, 30–60 мин)

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

- **Стек 3 (REALITY, microsoft.com, порт 2087)**: заблокирован ТСПУ в России — не работает
- **Стек 1 (Cloudflare CDN)**: cloudflared установлен, но VLESS+WS inbound не настроен — не работает
- **Стек 4 (Hysteria2)**: не настроен (standalone, отдельно от 3x-ui)
- **Failover**: работает переключение, но в текущей конфигурации доступен один рабочий стек (reality-grpc)
- **Сквозной тест клиента**: end-to-end тест (WG/AWG клиент → home → tun → VPS → сайт) не проводился
- **/graph команда**: Grafana Render API не настроен
- **mTLS**: CA создаётся, но клиентские сертификаты не выданы
- **CGNAT**: не работает без реального (белого) IP или bridge mode на роутере
- **combined.cidr**: 266 CIDR-записей, некоторые заблокированные сайты могут отсутствовать

### Требования

- **Домашний сервер**: x86_64, 4+ GB RAM, 64+ GB SSD, Ubuntu Server 24.04 LTS
- **VPS**: KVM, 1+ vCPU, 1+ GB RAM, Ubuntu 24.04 LTS (рекомендуется: 2 vCPU, 2 GB RAM)
- **Роутер**: реальный (белый) IP, НЕ CGNAT; поддержка port forwarding
- **Telegram**: бот (token от @BotFather) + chat_id администратора
- **Опционально**: Cloudflare аккаунт (бесплатный) для CDN-стека

### Планируется в v0.2.0

- Настройка Hysteria2 (стек 4)
- Настройка Cloudflare CDN (стек 1)
- Сквозное end-to-end тестирование всех стеков
- /graph команда (Grafana → Telegram PNG)
- Документация по настройке клиентских устройств

---

*Формат: [Keep a Changelog](https://keepachangelog.com/)*

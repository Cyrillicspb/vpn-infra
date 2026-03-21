# Changelog

Все значимые изменения проекта фиксируются здесь.
Формат: [SemVer](https://semver.org/), даты UTC.

---

## [v0.3.0.1] — 2026-03-22 — Исправления vpn-routes и autossh-vpn

### Исправления

- **vpn-policy-routing.sh**: `ip route add` → `ip route replace` — устраняет "RTNETLINK answers: File exists" при перезапуске сервиса или повторном запуске скрипта (идемпотентность).
- **autossh-vpn.service**: убран несуществующий пользователь `vpntunnel` и несуществующий ключ `/opt/vpn/.ssh/vpn-tunnel-key`. Сервис теперь запускается от `root`, использует `/root/.ssh/vpn_id_ed25519` и подключается как `sysadmin@VPS` — аналогично `autossh-tier2`.

---

## [v0.3.0.0] — 2026-03-21 — Bootstrap-инвайт, устойчивость установки

### Новое

- **Bootstrap-инвайт** (`/invite`): создаёт временные AWG + WG пиры заранее, генерирует конфиги и отправляет админу. Пользователь с заблокированным Telegram может подключиться по конфигу через любой мессенджер, после чего зарегистрироваться в боте.
- **Два сценария регистрации**: если пользователь подключился через bootstrap-конфиг (`last_handshake > 0`) — AWG-пир принимается как постоянный; если не подключался — temp-пиры удаляются и создаётся новый конфиг.
- **Cleanup loop**: каждый час удаляет истёкшие bootstrap-пиры (AWG + WG) и записи из БД.

### Исправления установки

- **AWG PGP-ключ** вшит в `install-home.sh` как heredoc — keyserver.ubuntu.com заблокирован в России.
- **zapret**: бинарники `nfqws-x86_64` / `nfqws-aarch64` (v72.12) в репо — GitHub заблокирован при установке.
- **Telegram tier1**: `api.telegram.org`, `web.telegram.org`, `telegram.org`, `t.me` — всегда в `blocked_static`, неудаляемые.
- **Smoke-тест таблицы 200**: проверяет `ip rule fwmark 0x1 lookup 200` вместо маршрута в таблице.
- **Telegram smoke-тест**: перенесён в конец фазы 4, ждёт до 60 сек пока watchdog + стек + DNS прогреются.
- **Установщики**: tar-архив + scp одного файла вместо `scp -r` (сотни `.git`-объектов).
- **GitHub Release**: `release.yml` публикует `vpn-infra.tar.gz` как fallback при недоступности GitHub.

---

## [v0.2.1] — 2026-03-19 — SSH Tier-2 туннель вместо WireGuard

### Исправления

- **Tier-2 туннель**: заменён `wg-quick@wg-tier2` (UDP 51822) на `autossh-tier2` (SSH tun, `autossh -w 0:0`, TCP 22/443).
  Причина: ISP блокировал UDP 51822 на инфраструктурном уровне — пакеты от домашнего сервера до VPS не доходили.
  Транспорт TCP 22/443 не блокируется. IP адреса туннеля (10.177.2.1↔10.177.2.2) не изменились — watchdog, dnsmasq, Prometheus не затронуты.
- **add-vps.sh**: аналогичная замена для второго VPS (`autossh-tier2-vps2`, tun1, 10.177.2.5↔10.177.2.6).
- **install-vps.sh / add-vps.sh**: убраны правила `udp dport 51822 accept` из nftables VPS.
- **VPS sshd_config**: шаг 45 добавляет `PermitTunnel yes` (нужен для `ssh -w`).

---

## [v0.2.0] — 2026-03-18 — UX бота, Xray xHTTP, zapret probe

### Новое

**Telegram-бот — администратор**
- Просмотр fail2ban jails для домашнего сервера и VPS прямо из бота (меню «Система»)
- Разбан IP одной кнопкой из Telegram без SSH
- `/renew-cert` и `/renew-ca` теперь работают через watchdog API (бот не требует доступа к хосту)

**Telegram-бот — клиент**
- Переработанное главное меню (вариант A): статус VPN в шапке, убраны дубли, сайты выделены в подменю
- Обновление конфига одного устройства кнопкой из детального вида
- Кнопка «Обновить все конфиги» в списке устройств

**DPI Bypass (zapret)**
- On-demand probe из бота: кнопка «🔄 Пересобрать пресет» в меню DPI запускает quick probe и шлёт результат в Telegram
- История смен пресетов: кнопка «📋 История» показывает последние 20 переключений с временными метками
- Логирование пресета при каждом старте nfqws в `preset_history.log`
- Watchdog API: `POST /zapret/probe` (quick/full), `GET /zapret/history`

**Xray — улучшение транспорта**
- REALITY стеки (microsoft.com, cdn.jsdelivr.net): добавлены `xPaddingBytes: 100-1000` и `mode: auto` — снижение fingerprint-детектируемости
- CDN стек: миграция с WebSocket на xHTTP (splithttp H2) — WebSocket устарел в Xray 26.x

### Исправления

- **deploy.sh**: `rsync home/xray/ xray/` заменён на `envsubst` — прямой rsync шаблонов с `${VAR}` приводил к падению xray-client (exit 23, «invalid password: ${XRAY_PUBLIC_KEY}»)
- **fail2ban VPS**: исправлен SSH-доступ через SOCKS5-прокси (`127.0.0.1:1081`), правильный ключ (`vpn_id_ed25519`), `sudo` для sysadmin, порт из `VPS_SSH_PORT` (env), а не из state (state хранил 443)
- **CF_WORKER_HOSTNAME → CF_CDN_HOSTNAME**: переименована переменная окружения по всему проекту
- **setup.sh**: `XRAY_SERVER` не писался в `.env` → `deploy.sh` не мог подставить адрес VPS через `envsubst`
- **xray-setup.sh**: CDN инбаунд на VPS создавался как WebSocket `/vpn` — исправлен на splithttp `/vpn-cdn` (совпадает с клиентом)

### Миграции (применяются автоматически при `deploy.sh`)

- `004`: `CF_WORKER_HOSTNAME` → `CF_CDN_HOSTNAME` в `.env`
- `005`: автозаполнение `XRAY_SERVER` из `VPS_IP` и паролей xHTTP из 3x-ui sqlite

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

**DPI Bypass — прямой обход без VPS (zapret/nfqws)**
- Перехват TCP 80/443 через nfqueue (Linux kernel), обход SNI-DPI без туннеля
- Техники: fakedsplit, fake+autottl, split2, multisplit (17 пресетов)
- Адаптивный подбор параметров: Thompson Sampling per-ISP, quick probe при старте
- Тест на реальных заблокированных хостах: discord.com, store.steampowered.com, youtube.com
- Работает параллельно с туннельными стеками; активируется через watchdog `/switch zapret`

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
- Алерты в Telegram: туннель down, RTT, диск, внешний IP, heartbeat (форматирование bold/code)
- Автоматическая проверка обновлений раз в час с уведомлением в Telegram
- /graph: графики из Grafana прямо в чат (Grafana image renderer работает) ✅
- /renew-cert: выпуск клиентского сертификата mTLS (.p12, импорт в браузер) ✅

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

### Исправления (hotfix)

- **Форматирование алертов watchdog**: сообщения бота отображались без форматирования (`*bold*` и `` `code` `` как обычный текст). Добавлена конвертация Markdown → HTML перед отправкой.

### Известные проблемы и ограничения

- **Стек 3 (REALITY, microsoft.com, порт 2087)**: TCP-порт открыт на VPS, но ТСПУ блокирует на уровне TLS/XHTTP handshake — стек не работает через российских провайдеров
- **CGNAT**: не работает без реального (белого) IP или bridge mode на роутере
- **combined.cidr**: 266 CIDR-записей — покрывает основные заблокированные сервисы, редкие домены вне крупных AS могут отсутствовать до следующего обновления в 03:00

### Требования

- **Домашний сервер**: x86_64, 4+ GB RAM, 64+ GB SSD, Ubuntu Server 24.04 LTS
- **VPS**: KVM, 1+ vCPU, 1+ GB RAM, Ubuntu 24.04 LTS (рекомендуется: 2 vCPU, 2 GB RAM)
- **Роутер**: реальный (белый) IP, НЕ CGNAT; поддержка port forwarding
- **Telegram**: бот (token от @BotFather) + chat_id администратора
- **Опционально**: Cloudflare аккаунт (бесплатный) для CDN-стека

---

*Формат: [Keep a Changelog](https://keepachangelog.com/)*

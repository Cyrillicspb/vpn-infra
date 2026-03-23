# Проект: Двухуровневая VPN-инфраструктура (v4.0)

## КОНТЕКСТ И РОЛЬ

Ты продолжаешь работу над проектом двухуровневой VPN-инфраструктуры. Проект работает в production. Задача — доработки, баг-фиксы и новые фичи из раздела TODO.

Работай пошагово, по одному шагу за раз. После каждого шага жди подтверждения.

## ЦЕЛЬ ПРОЕКТА

Двухуровневая самохостинговая VPN-инфраструктура для обхода DPI-фильтрации (ТСПУ/РКН):

- Стабильный доступ к заблокированным ресурсам для нескольких клиентов
- Адаптивный failover с 4 стеками при блокировке протоколов
- Управление через Telegram-бота
- Split tunneling — гибрид B+ (split на клиенте + маршрутизация на сервере)
- Установка одним скриптом, автообновление через GitHub
- Распространяемое решение для нетехнических пользователей

## TODO

Краткий перечень задач. Полные спецификации — в `docs/TODO-SPECS.md`.

- [ ] Email fallback для алертов при недоступности Telegram (приоритет: низкий)
- [ ] Multi-admin: поддержка нескольких администраторов (root + дополнительные через `/admin invite`)
- [ ] Полуавтоматическое обновление пресетов DPI-bypass (v2fly source, Thompson Sampling, 3 тира)
- [ ] GUI-установщик: TUI на сервере (Python + Textual), лаунчеры .bat/.command/.sh на клиенте
- [ ] Режим B — Gateway Mode: сервер дома за роутером, HAIRPIN NAT, split tunneling для LAN-устройств

-----

## ПРИНЦИПЫ РАЗРАБОТКИ И ЭКСПЛУАТАЦИИ

### Принцип 1: Никогда не правим данные на серверах напрямую

Если на сервере что-то не работает — мы НЕ правим конфиги руками. Находим источник проблемы (скрипт, шаблон), чиним ЕГО. Удаляем пометки в `.setup-state` и перезапускаем скрипт.

### Принцип 2: Единый источник правды для ключей и параметров

Все секреты генерируются ОДИН РАЗ в ОДНОМ МЕСТЕ (setup.sh → `.env`, chmod 600). ВСЕ компоненты читают из `.env`.

### Принцип 3: Согласованность скриптов

Все скрипты используют одну общую библиотеку (`common.sh`), один `.env`, одинаковые пути и имена сервисов.

### Принцип 4: Тесты на каждом этапе

После каждого шага установки — проверка. После полной установки — smoke-тесты. При эксплуатации — watchdog. При новом типе ошибки — добавляем тест.

### Принцип 5: Обновление только через релизы

`deploy.sh` скачивает конкретный помеченный тег, не произвольный коммит. Никаких `git pull` из main.

### Принцип 6: Версионирование `vX.Y.Z.W`

W — каждый деплой (автоинкремент CI), Z — патч, Y — значимые фичи, X — архитектурные изменения.

-----

## ОБОРУДОВАНИЕ

- **Домашний сервер**: x86_64, 4+ GB RAM, 64+ GB SSD, Ubuntu 24.04, Ethernet, max_clients ≈ upload_Mbps ÷ 5
- **Роутер**: реальный (белый) IP, не CGNAT, port forward UDP 51820 + 51821
- **VPS**: KVM, 2 vCPU, 2 GB RAM, US/Europe
- **Внешние зависимости**: VPS + Telegram (обязательно), Cloudflare + домен (опционально)

-----

## СЕТЕВАЯ ТОПОЛОГИЯ

```
КЛИЕНТЫ (AWG UDP 51820 / WG UDP 51821)
    → РОУТЕР (port forward)
    → ДОМАШНИЙ СЕРВЕР (Ubuntu 24.04)
        На хосте: amneziawg wg0, wireguard wg1, hysteria2, tun2socks,
                  dnsmasq, watchdog (:8080), nftables, ip rule/route, cron failsafe
        Docker:   telegram-bot, xray-client (:1080), xray-client-2 (:1081),
                  xray-client-cdn (:1082), cloudflared, prometheus, alertmanager,
                  grafana, node-exporter, socket-proxy
        4 стека failover:
          1. VLESS+WS через Cloudflare CDN
          2. VLESS+REALITY+gRPC (dest cdn.jsdelivr.net)
          3. VLESS+REALITY (dest microsoft.com)
          4. Hysteria2 (QUIC + Salamander)
        + zapret (nfqws) — DPI desync без VPS hop
    → VPS: 3x-ui (Xray), nginx (mTLS :8443), cloudflared, node-exporter
```

### Адресное пространство

| Сегмент | Подсеть |
|---|---|
| AWG клиенты | 10.177.1.0/24 |
| WG клиенты | 10.177.3.0/24 |
| Tier-2 туннель (SSH autossh) | 10.177.2.0/30 |
| Docker домашний | 172.20.0.0/24 |
| Альтернативная | 172.29.177.0/24 |

-----

## SPLIT TUNNELING — ГИБРИД B+

**Уровень 1 — AllowedIPs на клиенте**: ≤500 CIDR-записей (крупные AS + базы РКН). Определяет что ПОПАДАЕТ на сервер.

**Уровень 2 — nftables fwmark на сервере** (три категории):
- dst в `blocked_static`/`blocked_dynamic` → fwmark 0x1 → table 200 → tun → VPS
- dst в `dpi_direct` → fwmark 0x2 → table 201 → ens3 → nfqws DPI desync
- dst не в sets → table 100 → ens3 напрямую

**Docker-контейнеры** (br-vpn): весь внешний трафик → fwmark 0x1 → через активный VPN-стек. Output chain на хосте: watchdog/curl к blocked_static/blocked_dynamic/dpi_direct также помечается.

**Kill switch**: fwmark routing + nftables forward DROP (src VPN, dst blocked, oifname != tun*). Правила ПЕРЕД ct state established.

### Policy routing

```
ip rule priority 100: fwmark 0x1 → lookup 200 (blocked → VPN)
ip rule priority 200: from 10.177.1.0/24 → lookup 100 (прямой)
ip rule priority 200: from 10.177.3.0/24 → lookup 100
table 200: default dev tun-активный
table 100: default via $GATEWAY dev $ETH_IFACE
```

⚠️ **НЕ добавлять** `ip rule to 1.1.1.1/8.8.8.8 → lookup 200` — ломает dnsmasq upstream DNS.

### nft sets

- **blocked_static** — из баз РКН, без timeout, атомарное обновление `nft -f`
- **blocked_dynamic** — dnsmasq nftset=/ для blocked доменов, timeout 24h
- **dpi_direct** — dnsmasq nftset=/ для SNI-throttled, timeout 24h, fwmark 0x2

### dnsmasq

На хосте (systemd), nftset=/ для blocked_dynamic и dpi_direct. DNS через VPS (10.177.2.2) для обеих категорий. Поддомены покрываются автоматически. НЕ логирует DNS-запросы.

### Источники списков (cron 03:00)

antifilter.download, community.antifilter.download, iplist.opencck.org, zapret-info/z-i, статические AS. Per-source кэш, валидация (формат, >100, дельта <50%), flock, алерт при кэше >3 дней.

-----

## АДАПТИВНЫЙ FAILOVER — 4 СТЕКА

1. **CDN** — VLESS+WS через Cloudflare (неблокируем, медленнее)
2. **reality-grpc** — VLESS+REALITY+gRPC, dest cdn.jsdelivr.net (flow пустой — vision несовместим с gRPC)
3. **reality** — VLESS+REALITY, dest microsoft.com (flow: xtls-rprx-vision)
4. **hysteria2** — QUIC+Salamander UDP 443 (быстрый, легко блокируется)

### Логика watchdog

- Первый запуск: CDN → тест всех → промотировать самый быстрый
- Деградация: последовательно вверх по устойчивости, 10-сек тест каждого
- Восстановление: полная переоценка раз в час, protocol failback авто, VPS failback — с подтверждением
- Детекция: ping fail 3×10с (30с до failover), RTT >3× baseline, throughput < порог, speedtest 100KB/5мин + 10MB/6ч

### Ротация (анти-DPI)

Make-before-break через временный порт 1082, интервал 30–60 мин ±15. Ротация и failover — взаимоисключающие (единый decision loop).

### Plugin-архитектура стеков

```
plugins/<stack>/
├── client.py, client.yaml, server-install.sh, server.yaml, metadata.yaml
```

### Zapret (nfqws) — DPI desync

НЕ стек, работает параллельно. TCP через nfqueue FORWARD → nfqws → ens3 напрямую. Обходит SNI-throttling, НЕ обходит IP-блокировки. Thompson Sampling для подбора пресетов (17 пресетов, ночной probe 02:30). Бинарники nfqws бандлятся в репо (`home/watchdog/plugins/zapret/bin/`) — GitHub заблокирован из РФ.

-----

## WATCHDOG — ЦЕНТРАЛЬНЫЙ АГЕНТ

Python async (aiohttp/FastAPI) на хосте, systemd. **KillMode=process** (иначе убивает tun2socks, nfqws).

### API (0.0.0.0:8080, nftables accept 172.20.0.0/24, bearer token, rate limit POST 10/sec)

```
GET  /status, /metrics, /rotation-log, /nft/stats
POST /switch, /peer/add|remove|list, /routes/update, /service/restart|update
POST /deploy, /rollback, /reload-plugins, /graph, /notify-clients
POST /diagnose/<device>, /dpi/test, /backup
```

POST: 202 Accepted + background task для долгих. Mutex на /peer/add.

### Мониторинг watchdog

Ping VPS 10с, curl blocked 5мин, внешний IP 5мин, heartbeat VPS 60с, dnsmasq dig 30с, standby тест 04:30, DKMS, диск (80/90/95%), upload >80%, кэш маршрутов >3д, speedtest.

-----

## TELEGRAM-БОТ

Python + aiogram 3.x, Docker, через watchdog HTTP API. Весь интерфейс на русском.

### Режимы

- Админ (ADMIN_CHAT_ID): полный доступ
- Клиенты (зарегистрированные): самообслуживание
- Незарегистрированные: игнор (кроме /start)
- FSM timeout 10 мин, invite резервируется на 10 мин

### SQLite (WAL mode)

```
clients: id | chat_id | username | first_name | is_admin | created_at
devices: id | client_id | device_name | protocol | peer_id | config_version | created_at
domain_requests: id | chat_id | domain | direction | status | created_at
invite_codes: code | created_by | created_at | expires_at | used_by | used_at
excludes: device_id | subnet | created_at
```

Device limit per client: 5 (настраиваемый). IP pool проверка. SQLite backup через `.backup` API.

### Конфиг-билдер

Версия по хешу содержимого. QR только если AllowedIPs ≤50. Предупреждение о приватном ключе.

### Авторассылка конфигов

Триггеры: базы (cron), /routes update, одобрение /request, смена IP (если нет DDNS), /migrate-vps, /vpn add. Debounce 5 мин. Группировка по клиенту. Напоминание через 24ч.

### Команды админа

```
/status /tunnel /ip /docker /clients /speed /logs /graph
/switch /restart /reboot /update /deploy /rollback
/invite [bootstrap] /client disable|enable|kick|limit
/broadcast /vpn add|remove /direct add|remove /list /check
/routes update /requests /vps list|add|remove
/migrate-vps /rotate-keys /renew-cert /renew-ca /diagnose /menu
```

`/invite bootstrap` — генерирует временные AWG+WG пиры для первичной регистрации когда Telegram заблокирован у пользователя. Запросы на устройства приходят с inline-кнопками approve/reject прямо в уведомлении.

### Команды клиента

```
/start /mydevices /myconfig /adddevice /removedevice
/update /request vpn|direct /myrequests
/exclude add|remove|list /report /status /help
```

### Алерты

Туннель down/RTT/loss/bandwidth, шейпинг, контейнер exited, диск >85%, IP изменился, fail2ban, сертификат mTLS/CA, heartbeat lost, blocked недоступны, DKMS, standby fail, dnsmasq down, все стеки down >5мин, deploy fail, watchdog мёртв, кэш маршрутов устарел, upload >80%.

⚠️ WG peer stale — алерт отключён (мобильные уходят в сон, не признак проблемы).

Graceful degradation: при недоступности Telegram → очередь.

-----

## БЕЗОПАСНОСТЬ

- sysadmin user, root SSH закрыт, sudo NOPASSWD
- fail2ban: SSH + Xray порт
- mTLS: свой CA 4096 bit (10 лет), клиентский cert (2 года)
- IPv6 отключён, Docker socket proxy, image pinning
- Watchdog API: nftables accept 172.20.0.0/24 + bearer + rate limit
- Ядро: apt-mark hold + Pin-Priority -1, DKMS-проверка
- nftables rate limiting: UDP 51820/51821 100/s burst 200
- Ротация ключей: /rotate-keys
- Приватность: dnsmasq не логирует, per client логи не хранятся

-----

## АВТОМАТИЗАЦИЯ

### setup.sh (5 фаз, 51 шаг, идемпотентность через .setup-state)

- Фаза 0: предусловия, автообнаружение сети, CGNAT-проверка, ввод VPS/Telegram
- Фаза 1: домашний сервер
- Фаза 2: VPS через SSH (sysadmin) — долгие команды через `vps_exec_long` (nohup + tmux, авто-реконнект при обрыве SSH)
- Фаза 3: связка
- Фаза 4: smoke-тесты
- Фаза 5: ручные шаги (port forwarding)

Особенности установки: AWG PGP ключ встроен в скрипт (keyserver.ubuntu.com недоступен из РФ). `update-routes.py` запускается при первой установке сразу — не ждёт cron 03:00.

### deploy.sh

⚠️ `docker compose build` берёт код из `/opt/vpn/telegram-bot/`. Нужен `rsync` ПЕРЕД build. `docker compose restart` НЕ подхватывает новый код — нужен rebuild.

Git pull через VPS-зеркало (ssh-proxy.sh → активный SOCKS5-стек) → snapshot → apply → smoke-test → при провале auto-rollback. Если обновляет watchdog.py → отдельный процесс, переживающий restart, результат → curl Telegram. Версии hysteria2/tun2socks автодетектируются через GitHub Releases API (не хардкод).

-----

## ПАРАМЕТРЫ ПРОТОКОЛОВ

- **AmneziaWG**: Jc=4, Jmin=50, Jmax=1000, S1=30, S2=40, H1-H4=random, KA=25, MTU=1320
- **WireGuard**: KA=25, MTU=1320
- **Xray REALITY**: flow xtls-rprx-vision (голый), пустой (gRPC), fingerprint chrome
- **Hysteria2**: salamander, quic KA 20s, bandwidth up 50 / down 200 mbps

-----

## SYSTEMD — ПОРЯДОК ЗАГРУЗКИ

1. nftables → 2. vpn-sets-restore → 3. wg-quick@wg0/wg1 → 4. vpn-routes → 5. dnsmasq → 6. hysteria2 → 7. watchdog → 8. docker → 9. vpn-postboot

nftables: БЕЗ ExecStop flush.

-----

## СТРУКТУРА ФАЙЛОВ

Домашний сервер: `/opt/vpn/` — docker-compose, .env, xray/, cloudflared/, dnsmasq/, watchdog/ (plugins/), telegram-bot/ (handlers/, services/, templates/), scripts/, prometheus/, grafana/.
Системные: `/etc/hysteria/`, `/etc/vpn-routes/`, `/etc/nftables*.conf`, `/etc/systemd/system/`.
VPS: `/opt/vpn/` — docker-compose, nginx/, 3x-ui/, cloudflared/, scripts/.

Репозиторий: `setup.sh, install-home.sh, install-vps.sh, deploy.sh, restore.sh` + `home/`, `vps/`, `installers/`, `tests/`, `migrations/`, `docs/`.

-----

## ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ

- Failover обрыв 5–10 сек (kill switch защищает)
- Ротация: обрыв панелей 1–3 сек
- WireGuard DNS Endpoint не перерезолвит → обрыв ~25–60 сек при DDNS + смене IP
- CGNAT/двойной NAT: не работает, нужен реальный IP
- Private DNS (DoH/DoT): обходит dnsmasq
- QR-код не влезает при >50 AllowedIPs → .conf файлом
- do-release-upgrade запрещено → переустановка + restore.sh
- xray/*.json — шаблоны с `${VAR}`, деплоить через envsubst
- VPS 23.95.252.178 геолоцируется Google как Beijing → нужна смена VPS
- **AR1**: dpi_direct + UDP/QUIC — nfqws перехватывает только TCP; YouTube QUIC (UDP 443) идёт без DPI bypass → шейпинг остаётся
- **AR2**: Thompson Sampling без decay — при смене DPI-конфига ISP сходимость к новому пресету медленная (накопленные alpha/beta тянут к старому)
- **AR3**: `nft -f` flush при `check_nftables_integrity` сбрасывает `blocked_dynamic` и `dpi_direct`; dnsmasq не знает о сбросе → sets пустые до следующих DNS-запросов

-----

## ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ

См. `.env.example` в корне репозитория. Все секреты автогенерируются setup.sh.

-----

## ТЕКУЩИЙ СТАТУС

Проект работает в production. Все основные компоненты реализованы.

- Домашний сервер: 80.93.52.223 — AWG, WG, watchdog, telegram-bot, docker, мониторинг
- VPS: 23.95.252.178 — 3x-ui, nginx, cloudflared, node-exporter
- ⚠️ VPS геолоцируется Google как Beijing → нужна смена на Hetzner/Vultr/DigitalOcean

*Промпт v4.2 — оптимизация размера, TODO-спеки вынесены в docs/TODO-SPECS.md*

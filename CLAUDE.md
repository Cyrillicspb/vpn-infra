# Проект: Двухуровневая VPN-инфраструктура (v4.0)

# Переходный промпт — полное состояние проекта

-----

## КОНТЕКСТ И РОЛЬ

Ты продолжаешь работу над проектом двухуровневой VPN-инфраструктуры. Архитектурное проектирование ПОЛНОСТЬЮ ЗАВЕРШЕНО. Проведено 10 раундов виртуального тестирования (105 тестов), все найденные проблемы исправлены. Твоя задача — реализация.

Работай пошагово, по одному шагу за раз. После каждого шага жди подтверждения.

-----

## ЦЕЛЬ ПРОЕКТА

Двухуровневая самохостинговая VPN-инфраструктура для обхода DPI-фильтрации (ТСПУ/РКН):

- Стабильный доступ к заблокированным ресурсам для нескольких клиентов
- Адаптивный failover с 4 стеками при блокировке протоколов
- Управление через Telegram-бота
- Split tunneling — гибрид B+ (split на клиенте + маршрутизация на сервере)
- Установка одним скриптом, автообновление через GitHub
- Распространяемое решение для нетехнических пользователей

-----

## ОБОРУДОВАНИЕ

**Домашний сервер** (любой x86_64):
- Минимум: 4 GB RAM, 64 GB SSD, Ethernet
- Рекомендуется: 8+ GB RAM, 128+ GB SSD
- ОС: Ubuntu Server 24.04.2 LTS
- LAN IP: определяется автоматически
- Бутылочное горлышко: upload канала (формула: max_clients ≈ upload_Mbps ÷ 5 для HD)

**Роутер** (любой с port forwarding):
- **КРИТИЧНО: реальный (белый) IP, не CGNAT**
- Или DDNS при динамическом IP
- Port forward UDP 51820 + 51821

**VPS** (один или несколько):
- KVM, 2 vCPU, 2 GB RAM, 40 GB SSD
- Рекомендуемые локации: US, Europe

**Обязательные внешние зависимости**: только VPS и Telegram.
**Рекомендуемые**: Cloudflare аккаунт (бесплатный, для CDN-стека), домен (опционально).

-----

## СЕТЕВАЯ ТОПОЛОГИЯ

```
КЛИЕНТЫ (телефоны, ноутбуки, роутеры)
    │
    │ AmneziaWG UDP 51820 / WireGuard UDP 51821
    ▼
РОУТЕР (port forward)
    ▼
ДОМАШНИЙ СЕРВЕР — Ubuntu 24.04
    │
    │ === НА ХОСТЕ (systemd) ===
    ├── amneziawg-dkms (wg0: AWG 10.177.1.0/24)
    ├── wireguard (wg1: WG 10.177.3.0/24)
    ├── hysteria2 (Tier-2, systemd)
    ├── tun2socks (для fallback-стеков)
    ├── autossh-vpn (SSH fallback)
    ├── dnsmasq (DNS + nftset=/ для blocked_dynamic)
    ├── watchdog (центральный агент, async Python, HTTP API :8080)
    ├── nftables (NAT + fwmark routing + kill switch)
    ├── ip rule/route (fwmark 0x1→table 200, VPN-src→table 100)
    └── cron failsafe (проверка watchdog каждые 5 мин)
    │
    │ === В DOCKER ===
    ├── telegram-bot    (→ watchdog HTTP API)
    ├── xray-client     (SOCKS5 :1080, standby)
    ├── xray-client-2   (SOCKS5 :1081, standby, dest apple.com)
    ├── cloudflared     (CDN-стек, standby)
    ├── amnezia-wg-easy [опционально, read-only]
    ├── portainer       [опционально]
    ├── homepage        [опционально]
    ├── node-exporter
    └── socket-proxy
    │
    │ 4 стека (адаптивный выбор по устойчивости/скорости):
    │   1. VLESS+WebSocket через Cloudflare CDN (cloudflared)
    │   2. VLESS+REALITY+gRPC (dest cdn.jsdelivr.net, без flow vision)
    │   3. VLESS+REALITY (dest microsoft.com, flow xtls-rprx-vision)
    │   4. Hysteria2 (QUIC + Salamander)
    ▼
VPS (один или несколько)
    ├── [DOCKER] 3x-ui (network_mode: host)
    │     ├── Xray: TCP 443 REALITY + fallback → Nginx :8443
    │     ├── Xray: gRPC inbound (SNI cdn.jsdelivr.net)
    │     └── Hysteria2: UDP 443 Salamander
    ├── [DOCKER] nginx (mTLS + панели, :8443)
    ├── [DOCKER] cloudflared (tunnel для CDN-стека)
    ├── [DOCKER] grafana, prometheus, alertmanager, node-exporter
    ├── [HOST]   vps-healthcheck.sh (cron 5 мин → Telegram)
    └── [HOST]   git-зеркало (cron sync с GitHub)
```

### Адресное пространство

| Сегмент | Подсеть |
|---|---|
| AWG клиенты | 10.177.1.0/24 |
| WG клиенты | 10.177.3.0/24 |
| Tier-2 туннель | 10.177.2.0/30 |
| Docker домашний | 172.20.0.0/24 |
| Альтернативная (при конфликте с офисным /8) | 172.29.177.0/24 |

-----

## SPLIT TUNNELING — ГИБРИД B+

### Принцип

Два уровня split tunneling работают совместно:

**Уровень 1 — AllowedIPs на клиенте (широкий забор)**:
- Крупные AS-подсети CDN-провайдеров (Google, Meta, Cloudflare) — агрегированные
- Конкретные подсети заблокированных сервисов из крупных AS (AWS, Microsoft, Akamai)
- Конкретные CIDR из баз РКН
- DNS сервер 10.177.1.1/32 + резервный 1.1.1.1/32
- ≤500 записей (CIDR-агрегация через aggregate6/cidr-merger)
- Определяет какой трафик ПОПАДАЕТ на домашний сервер

**Уровень 2 — nftables fwmark + nft sets на сервере (точный split)**:
- Трафик от VPN-клиентов, попавший на сервер, маршрутизируется:
  - dst в nft set (заблокированный) → fwmark 0x1 → table 200 → tun (VPS)
  - dst НЕ в nft set → table 100 → default via gateway → eth0 (прямой интернет)
- dnsmasq nftset=/ при DNS-резолве добавляет IP в blocked_dynamic (timeout 24h)
- Базы РКН загружаются в blocked_static (без timeout)

**Kill switch (двойная защита)**:
- fwmark routing: заблокированный → tun, при падении tun → UNREACHABLE → drop
- nftables forward: src VPN, dst in blocked, oifname != tun* → DROP
- Незаблокированный трафик через eth0 продолжает работать при падении tun
- Kill switch правила ПЕРЕД ct state established для VPN-подсетей

### Policy routing

```
ip rule priority 100: fwmark 0x1 → lookup 200 (заблокированное → VPN)
ip rule priority 150: to 1.1.1.1 → lookup 200 (DNS через VPN)
ip rule priority 150: to 8.8.8.8 → lookup 200 (DNS через VPN)
ip rule priority 200: from 10.177.1.0/24 → lookup 100 (незаблокированное → прямой)
ip rule priority 200: from 10.177.3.0/24 → lookup 100
table 200: default dev tun-активный
table 100: default via $GATEWAY dev $ETH_IFACE
```

### nftables — два nft sets

- **blocked_static** — из баз РКН, без timeout, обновляется скриптом
- **blocked_dynamic** — из dnsmasq nftset=/, timeout 24h, self-cleaning

Обновление blocked_static: генерируется полный nft-скрипт → `nft -f` атомарно
(никакого flush+add — окно утечки!).

### nftables rate limiting

- UDP 51820/51821: limit rate 100/second burst 200 (защита от UDP flood)
- Watchdog API: POST endpoints — 10 req/sec

### dnsmasq

- На хосте (systemd unit, не Docker), nftset=/ для blocked_dynamic
- server=/domain/ для заблокированных доменов (резолв через VPS DNS)
- Поддомены покрываются автоматически (server=/youtube.com/ → *.youtube.com)
- НЕ логирует DNS-запросы в production (privacy)
- DNS домашнего сервера → 127.0.0.1 (свой dnsmasq)
- Прогрев DNS-кэша при старте (пререзолв популярных доменов)

### Восстановление nft sets после перезагрузки

- vpn-sets-restore.service (After=nftables): `nft -f /etc/nftables-blocked-static.conf`
- blocked_dynamic пустой → dnsmasq прогрев кэша заполнит

### Источники списков (cron 03:00)

- antifilter.download, community.antifilter.download
- iplist.opencck.org, github.com/zapret-info/z-i
- github.com/RockBlack-VPN (геоблок)
- Статический список AS-подсетей CDN (в репозитории)
- /etc/vpn-routes/manual-vpn.txt, manual-direct.txt
- Per-source кэш: при недоступности источника → предыдущая версия
- Алерт при возрасте кэша > 3 дней
- Валидация: формат IP/CIDR, размер >100, дельта <50%
- Конкурентность: flock /var/run/vpn-routes.lock

### Самообучение (оптимизация AllowedIPs)

Watchdog собирает статистику через conntrack раз в час: какие подсети из AllowedIPs стабильно идут через eth0 (незаблокированные). Рекомендация админу убрать подсеть ТОЛЬКО если:
- 100% трафика через eth0 (ни одного IP в nft set blocked)
- Подсеть присутствует в AllowedIPs как отдельная запись (не разбивать крупные AS)
- Нет пересечения с nft set blocked_static

AS-блоки — постоянный «широкий забор», самообучение для них не работает.
Эффективно только для мелких CIDR из баз РКН.

-----

## АДАПТИВНЫЙ FAILOVER — 4 СТЕКА

### Стеки (по устойчивости от максимальной)

1. **VLESS+WebSocket через Cloudflare CDN** (cloudflared)
   - Неблокируем без блокировки Cloudflare
   - Медленнее (дополнительный hop через CDN)
   - Требует бесплатный Cloudflare аккаунт
   - Архитектура: VPS cloudflared tunnel → Xray VLESS localhost → CDN → домашний сервер cloudflared access → Xray client → tun2socks

2. **VLESS+REALITY+gRPC** (dest cdn.jsdelivr.net)
   - Очень устойчив, маскировка под gRPC-сервис
   - flow: пустой (НЕ xtls-rprx-vision, vision несовместим с gRPC)

3. **VLESS+REALITY** (dest microsoft.com)
   - Устойчив, маскировка под HTTPS к Microsoft
   - flow: xtls-rprx-vision, fingerprint: chrome

4. **Hysteria2** (QUIC + Salamander)
   - Быстрый, но легко блокируется (QUIC-паттерн)
   - UDP 443

### Адаптивная логика watchdog

```
Первый запуск:
  → Начать с CDN (гарантированно работает)
  → Последовательно тестировать все стеки (throughput, 10 сек каждый)
  → Промотировать самый быстрый работающий на роль primary

Деградация primary:
  → Последовательный проход ВВЕРХ по устойчивости (не прыжок на CDN)
  → 10-сек тест каждого стека, worst case до CDN ~30 сек
  → Переключение на первый работающий

Восстановление:
  → Фоновая полная переоценка всех стеков раз в час
  → Если более быстрый стек заработал → промотировать
  → Protocol failback: автоматический
  → VPS failback: с подтверждением админа

Детекция деградации (три типа):
  → Ping fail 3 раза подряд (30 сек) → полная потеря
  → RTT > 3x от baseline (7 дней скользящее) → latency деградация
  → Throughput < порог от baseline → шейпинг
  → Speedtest: маленький (100KB каждые 5 мин) + большой (10MB раз в 6ч)
  → Сравнение tun vs eth0 throughput → исключение ложных срабатываний
  → Сравнение маленького и большого speedtest → детекция объёмного шейпинга
```

### Ротация соединений (анти-DPI)

- Make-before-break: новое соединение поднимается ДО закрытия старого
- Через временный порт (1082), после переключения route → закрыть старое
- Интервал: 30–60 мин (рандомный ±15 мин)
- Рандомизация keepalive timing
- При ротации: тест НОВОГО подключения (dest domain доступен?)
- Ротация и failover — взаимоисключающие решения (единый decision loop)

### Мульти-VPS

- Каждый VPS — «VPS endpoint» с внутренним failover 4 стеков
- Один активный tun per VPS наружу
- Балансировка: round-robin между VPS (weighted позже)
- VPS down → перераспределение на оставшиеся
- Тестирование: быстрый failover per VPS (10 сек) + фоновая полная переоценка раз в час
- /vps list, /vps add, /vps remove

### Plugin-архитектура стеков

```
plugins/hysteria2/
├── client.py        — управление на домашнем сервере
├── client.yaml      — конфиг клиента
├── server-install.sh — установка на VPS
├── server.yaml      — конфиг сервера
└── metadata.yaml    — имя, resilience, порты, зависимости
```

Watchdog hot reload: SIGHUP → пересканировать plugins без полного рестарта.

-----

## WATCHDOG — ЦЕНТРАЛЬНЫЙ АГЕНТ

**Python async (aiohttp/FastAPI)** на хосте, systemd unit.

### Защита от сбоя

- WatchdogSec=30 в systemd
- Restart=always, RestartSec=5, StartLimitBurst=5, StartLimitIntervalSec=300
- Consistency recovery при старте: проверить все сервисы, при неконсистентности → стоп всё, начать с PRIMARY
- Degraded mode: работает без туннелей при первой установке
- Cron failsafe: каждые 5 мин `systemctl is-active watchdog || curl telegram "WATCHDOG МЁРТВ"`
- SIGTERM handler: алерт «сервер выключается», сохранить состояние, без failover

### API (0.0.0.0:8080 + nftables INPUT accept только 172.20.0.0/24 + bearer token)

```
GET  /status, /metrics
POST /switch, /peer/add, /peer/remove, /peer/list
POST /routes/update, /service/restart, /service/update
POST /deploy, /rollback, /reload-plugins
POST /graph (→ Grafana Render API → PNG)
POST /notify-clients
POST /diagnose/<device>
```

- POST endpoints: rate limiting 10 req/sec
- Долгие операции (/routes/update, /deploy): 202 Accepted + background task
- mutex на /peer/add (race condition при одновременных добавлениях)

### Мониторинг

- Ping VPS через tun (каждые 10 сек)
- curl заблокированных сайтов через тun (каждые 5 мин)
- Внешний IP (каждые 5 мин) → при смене: DDNS обновление, рассылка конфигов только если DDNS не настроен
- Heartbeat → VPS (каждые 60 сек)
- dnsmasq healthcheck: dig @127.0.0.1 (каждые 30 сек), при падении → рестарт + алерт
- Проверка standby-туннелей (04:30 ежесуточно, test mode: без production side effects), тест НОВОГО подключения
- DKMS-проверка после обновления ядра
- Диск: автоочистка 80%, агрессивная 90% (prune all, удалить бэкапы), аварийная 95% (остановить некритичные)
- Upload utilization канала → алерт > 80%
- Возраст кэша маршрутов → алерт > 3 дней
- Speedtest: 100KB/5мин + 10MB/6ч (детекция объёмного шейпинга)

-----

## TELEGRAM-БОТ

Python + aiogram 3.x, Docker, через watchdog HTTP API.

### Двухрежимный

- Админ (ADMIN_CHAT_ID): полный доступ
- Клиенты (зарегистрированные): самообслуживание
- Незарегистрированные: игнор (кроме /start)
- Автоматическая регистрация админа при первом запуске (без invite-кода)
- /start проверяет chat_id в БД: если уже зарегистрирован → показать устройства, не запрашивать invite

### FSM

- Timeout 10 мин для любого состояния
- Любая команда из другого состояния → сброс FSM → выполнить команду
- Invite-код резервируется на 10 мин при вводе, разрезервируется при таймауте

### Весь интерфейс на русском

### Default handler

- Зарегистрированные: «Неизвестная команда. /help»
- Незарегистрированные: игнор

### SQLite (WAL mode)

```
clients: chat_id | device_name | protocol | peer_id | config_version | created_at
domain_requests: id | chat_id | domain | direction | status | created_at
invite_codes: code | created_by | created_at | expires_at | used_by | used_at
excludes: device_id | subnet | created_at
```

- IP pool проверка при создании пира (свободные IP в /24)
- Device limit per client: по умолчанию 5, настраиваемый /client limit
- backup.sh: `sqlite3 .backup` для консистентной копии (не cp WAL-файлов)

### Конфиг-билдер

- Шаблонизатор .conf: ключи + AllowedIPs из combined.cidr + Endpoint + DNS + MTU + AWG-параметры
- Версия по хешу содержимого (не инкрементальная)
- /update: не отправлять если конфиг не изменился
- QR-код только если AllowedIPs ≤ 50 записей (иначе не влезает)
- Предупреждение о приватном ключе при каждой отправке .conf

### Авторассылка конфигов

Триггеры:
1. Изменение баз (cron 03:00, diff)
2. /routes update
3. Одобрение /request vpn
4. Смена внешнего IP (только если DDNS НЕ настроен)
5. /migrate-vps
6. /vpn add (автоматически добавляет /24 подсеть IP домена в AllowedIPs)

Debounce 5 минут: множественные изменения → один финальный конфиг.
Группировка: все устройства одного клиента в одном сообщении.
Напоминание через 24ч если клиент не обновил конфиг.

### Команды админа

```
/status /tunnel /ip /docker /clients /speed
/logs <сервис> [кол-во строк]
/graph [tunnel|speed|clients|system] [период]
/switch <стек>
/restart <сервис>
/reboot (подтверждение)
/update (Docker images, подтверждение)
/deploy /rollback
/invite
/client disable|enable|kick|limit <имя> [значение]
/broadcast <сообщение>
/vpn add|remove <домен>
/direct add|remove <домен>
/list vpn|direct
/check <домен>
/routes update
/requests
/vps list|add|remove
/migrate-vps <IP> [--from-backup]
/rotate-keys
/renew-cert (клиентский mTLS)
/renew-ca (CA, редко)
/diagnose <устройство>
/menu
```

### Команды клиента

```
/start (регистрация: invite → имя → протокол AWG/WG)
/mydevices /myconfig <имя> /adddevice (модерация админа) /removedevice
/update
/request vpn|direct <домен> (модерация)
/myrequests
/exclude add|remove|list <подсеть>
/report <описание> (сообщение админу)
/status /help
```

### Алерты (Telegram)

- Туннель down > 1 мин / RTT > 500ms / loss > 5% / bandwidth < 5 Mbps
- Шейпинг обнаружен (throughput < порог от baseline)
- Объёмный шейпинг (расхождение маленького и большого speedtest)
- WG peer stale > 180s
- Контейнер exited/unhealthy
- Диск > 85%
- Внешний IP изменился
- fail2ban: IP забанен
- Сертификат mTLS/CA истекает (клиентский ≤14 дней, CA ≤30 дней)
- Heartbeat lost > 5 мин
- Заблокированные сайты недоступны через туннель
- DKMS модуль не собран
- Standby-туннель не прошёл проверку
- dnsmasq не отвечает
- Все стеки down > 5 мин (+ уведомление клиентам)
- REALITY dest domain: новое подключение fail
- Watchdog мёртв (cron failsafe)
- Кэш маршрутов устарел > 3 дней
- Upload utilization > 80%
- Deploy fail → auto-rollback

Graceful degradation: при недоступности Telegram API → очередь, отправка при восстановлении.

-----

## БЕЗОПАСНОСТЬ

- **sysadmin**: setup создаёт пользователя, root SSH закрыт, sudo NOPASSWD
- **fail2ban**: оба узла, SSH + Xray порт на VPS
- **Rate limiting VPS**: nftables на TCP/UDP 443
- **mTLS**: свой CA (4096 bit, TTL 10 лет), клиентский cert (TTL 2 года)
- **IPv6**: отключён на обоих узлах
- **Docker socket proxy**: бот и Portainer через proxy
- **Docker image pinning**: фиксированные версии, Dependabot/Renovate в GitHub
- **Watchdog API**: 0.0.0.0:8080 + nftables INPUT accept 172.20.0.0/24 + bearer token + rate limit POST
- **Секреты**: chmod 600, .gitignore, автогенерация setup-скриптом
- **Ядро**: apt-mark hold + Pin-Priority -1, DKMS-проверка
- **Автообновление ОС**: unattended-upgrades security only
- **Автоуправление портами**: watchdog обновляет nftables при переключении стека
- **Ротация ключей**: /rotate-keys
- **Приватность логов**: dnsmasq не логирует запросы, логи per client не хранятся
- **Приватность конфигов**: предупреждение при отправке .conf, рекомендация 2FA
- **/adddevice**: требует подтверждение админа
- **Валидация баз маршрутов**: формат, размер, дельта >50%
- **nftables атомарное обновление**: nft -f (не flush+add)

-----

## DDNS

- Поддержка: DuckDNS, No-IP, Cloudflare DDNS
- Endpoint в конфигах = DDNS-домен (не IP)
- При смене IP: обновить DNS-запись, НЕ рассылать конфиги (Endpoint не изменился)
- Без DDNS: при смене IP → рассылать конфиги (Endpoint = IP)
- Известное ограничение: WireGuard не перерезолвит DNS Endpoint; при смене IP → обрыв ~25–60 сек

-----

## СОСУЩЕСТВОВАНИЕ С ОФИСНЫМ VPN

- Setup.sh обнаруживает активные WG-интерфейсы автоматически
- Бот при /start и /exclude: управление исключениями per device
- Наша подсеть 10.177.x.x не пересекается с типичными офисными (10.0.0.0/24, 192.168.x.x)
- При конфликте (офисный VPN /8 покрывает 10.177.x.x) → алерт + альтернативная подсеть 172.29.177.0/24
- /exclude add|remove|list per device (таблица excludes в SQLite)

-----

## БЭКАПЫ

- Cron 04:00, GPG симметричное шифрование
- Состав: ключи, конфиги, .env, SQLite (.backup API), nftables
- Исключено: Grafana/Prometheus data (dashboards из git)
- Гарантия: ≤50 MB (Telegram лимит)
- Назначение: VPS (30 дней ротация) + Telegram
- Snapshot перед deploy (для rollback)
- restore.sh = install-home.sh + overwrite конфигами из бэкапа + пересоздание venv
- /migrate-vps --from-backup: восстановление ключей на новый VPS

-----

## МОНИТОРИНГ

- **Pull**: Prometheus на VPS ← домашний сервер через туннель (node-exporter, watchdog)
- **Push**: Watchdog → Telegram напрямую
- **Heartbeat**: watchdog → VPS 60 сек, потеря > 5 мин → алерт
- **VPS healthcheck**: cron 5 мин, проверка Docker + curl сервисов → Telegram
- **Post-boot**: vpn-postboot.service → проверка + отчёт в Telegram
- **Grafana → Telegram**: /graph → watchdog → Grafana Render API → PNG
- Prometheus через Nginx reverse proxy на VPS (стабильнее чем прямой scrape)

-----

## ЛОГИРОВАНИЕ

- Docker: json-file, max-size 10m, max-file 3
- Хостовые: /var/log/vpn-*.log, logrotate daily, rotate 14, compress
- journald: SystemMaxUse=500M
- dnsmasq: НЕ логировать запросы
- Timezone: одинаковый на обоих серверах, логи UTC, отображение местное
- Бот: /logs <сервис> для просмотра, выгрузка файлом

-----

## АВТОМАТИЗАЦИЯ

### setup.sh — главный мастер

- Фаза 0: предусловия (VPS? Telegram? CGNAT? DDNS? Cloudflare? домен?)
  - Автообнаружение: сеть, IP, gateway, MAC, CGNAT, двойной NAT, Wi-Fi чипсет
  - CGNAT-сообщение: три причины (CGNAT, двойной NAT, bridge mode)
  - Пользователь вводит руками: VPS IP, VPS пароль, Telegram bot token, chat_id
  - Автогенерация ВСЕХ секретов (ключи, пароли, UUID)
  - VPS bootstrap: если SSH:22 заблокирован → инструкция через веб-консоль VPS
- Фаза 1: домашний сервер
- Фаза 2: VPS через SSH (sysadmin, не root)
- Фаза 3: связка
- Фаза 4: smoke-тесты (DNS, split, туннель, watchdog API, бот, curl blocked sites, kill switch)
- Фаза 5: ручные шаги (port forwarding, mTLS)
- Идемпотентность: /opt/vpn/.setup-state, каждый шаг проверяет «уже сделано?» (включая генерацию ключей, cloudflared tunnel create)
- Прогресс-бар «Шаг N/51», retry per step
- При ошибке: что не так + как исправить + продолжить

### deploy.sh — обновление

- Git pull из VPS-зеркала (не напрямую из GitHub — может быть заблокирован)
- Snapshot перед обновлением
- Apply → автоматический smoke-test → при провале → auto-rollback + алерт
- Миграция конфигов между версиями (migrations/)
- Обновление VPS через SSH, per-VPS deploy status + retry
- Если обновляет watchdog.py → deploy.sh как отдельный процесс, переживающий restart watchdog, результат → прямой curl Telegram
- Бот: «Доступно v1.3→v1.4 [Обновить] [Пропустить] [Подробнее]»
- Проверка обновлений каждый час (VPS-зеркало)
- Security-обновления: ⚠️

### VPS git-зеркало

- VPS: cron git fetch из GitHub → локальная копия
- Домашний сервер: git pull через SSH-туннель к VPS
- Решает проблему блокировки GitHub из России

### Установочные файлы

- .bat (Windows) / .command (macOS) — SSH к серверу + запуск setup.sh
- Без PyInstaller (SmartScreen, антивирус)

-----

## ПАРАМЕТРЫ ПРОТОКОЛОВ

### AmneziaWG
```
Jc=4, Jmin=50, Jmax=1000, S1=30, S2=40
H1/H2/H3/H4 = random uint32
PersistentKeepalive=25, MTU=1320
```

### WireGuard
```
PersistentKeepalive=25, MTU=1320
```

### Xray REALITY
```
flow: xtls-rprx-vision (голый), пустой (gRPC)
fingerprint: chrome
dest: microsoft.com (голый), cdn.jsdelivr.net (gRPC)
```

### Hysteria2
```
obfs: salamander, quic keepAlive 20s
bandwidth: up 50, down 200 mbps
```

-----

## СИСТЕМD — ПОРЯДОК ЗАГРУЗКИ

1. nftables.service (правила + пустые sets)
2. vpn-sets-restore.service (заполнить blocked_static)
3. wg-quick@wg0, wg-quick@wg1
4. vpn-routes.service (ip rule/route)
5. dnsmasq.service (+ прогрев DNS-кэша)
6. hysteria2.service
7. watchdog.service (async Python, venv)
8. docker.service (все контейнеры restart: always)
9. vpn-postboot.service (проверка + отчёт Telegram)

nftables: БЕЗ ExecStop flush (правила в ядре, исчезнут при shutdown).

-----

## СТРУКТУРА ФАЙЛОВ — ДОМАШНИЙ СЕРВЕР

```
/opt/vpn/
├── docker-compose.yml
├── .env (chmod 600)
├── .env.example
├── .setup-state
├── .deploy-snapshot/
├── version
├── amnezia-wg-easy/ [опционально]
├── xray/ (config-reality.json, config-grpc.json)
├── cloudflared/ (config.yml)
├── dnsmasq/
│   ├── dnsmasq.conf
│   └── dnsmasq.d/
│       ├── vpn-domains.conf (server=/ генерируется)
│       └── vpn-force.conf (server=/ генерируется)
├── homepage/ [опционально]
├── socket-proxy/
├── watchdog/
│   ├── watchdog.py
│   ├── requirements.txt (пины версий)
│   ├── venv/
│   ├── plugins/
│   │   ├── hysteria2/
│   │   ├── reality/
│   │   ├── reality-grpc/
│   │   └── cloudflare-cdn/
│   └── watchdog.service
├── telegram-bot/
│   ├── bot.py, config.py, database.py
│   ├── handlers/ (admin, client, requests, alerts)
│   ├── services/ (watchdog_client, config_builder, autodist, system)
│   ├── templates/ (шаблоны .conf)
│   ├── Dockerfile, docker-compose.yml, .env
│   └── data/vpn_bot.db
├── scripts/
│   ├── update-routes.py
│   ├── backup.sh
│   ├── restore.sh
│   ├── disk-cleanup.sh
│   ├── postboot-check.sh
│   └── dns-warmup.sh (прогрев dnsmasq)
└── backups/

/etc/hysteria/config.yaml
/etc/vpn-routes/ (manual-vpn.txt, manual-direct.txt, combined.cidr, per-source кэши)
/etc/nftables.conf (правила)
/etc/nftables-blocked-static.conf (элементы set, генерируется)
/etc/sysctl.d/ (99-disable-ipv6.conf, 99-bbr.conf)
/etc/docker/daemon.json
/etc/systemd/system/ (vpn-routes, vpn-sets-restore, hysteria2,
    tun2socks@, autossh-vpn, watchdog, dnsmasq, vpn-postboot)
/etc/apt/preferences.d/pin-kernel (Pin-Priority -1)
/usr/local/bin/ (hysteria, tun2socks)
/etc/cron.d/vpn-watchdog-failsafe
```

## СТРУКТУРА ФАЙЛОВ — VPS

```
/opt/vpn/
├── docker-compose.yml, .env
├── nginx/ (conf.d/, mtls/, ssl/)
├── 3x-ui/db/
├── cloudflared/ (config.yml, tunnel credentials)
├── prometheus/, alertmanager/, grafana/
├── hugo-site/ [опционально]
├── scripts/vps-healthcheck.sh
├── vpn-repo.git/ (зеркало GitHub)
└── backups/

/etc/sysctl.d/, /etc/ssh/sshd_config (Port 22 + 443)
```

## GITHUB-РЕПОЗИТОРИЙ

```
vpn-infra/
├── setup.sh, install-home.sh, install-vps.sh
├── deploy.sh, restore.sh
├── home/ (docker-compose, telegram-bot/, watchdog/, dnsmasq/, systemd/, scripts/)
├── vps/ (docker-compose, nginx/, prometheus/, cloudflared/, hugo-site/)
├── installers/ (macos/, windows/)
├── tests/ (smoke-тесты)
├── migrations/
├── .env.example, README.md
└── docs/
    ├── INSTALL.md, HARDWARE.md, ARCHITECTURE.md
    ├── COMMANDS.md, FAQ.md, TROUBLESHOOTING.md
    ├── SECURITY.md, PRIVACY.md
    ├── UPDATE.md, DISASTER-RECOVERY.md
    └── REQUIREMENTS.md (характеристики, провайдеры, формула upload, UPS)
    Всё на русском, Mermaid-диаграммы.
```

-----

## ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ

- **Failover обрыв 5–10 сек**: kill switch защищает, но кратковременный обрыв заблокированных сайтов
- **Ротация: обрыв панелей**: make-before-break ≈ 1–3 сек, панели переподключаются
- **Telegram недоступен**: алерты в очередь, аварийный доступ через SSH
- **WireGuard DNS Endpoint**: не перерезолвит; при DDNS + смене IP → обрыв ~25–60 сек
- **DNS-кэш клиента**: после /vpn add → до 5 мин на протухание кэша
- **CGNAT/Двойной NAT**: проект не работает, нужен реальный IP или bridge mode
- **Private DNS (DoH/DoT)**: обходит dnsmasq, документировать отключение
- **Upload канала**: бутылочное горлышко, формула max_clients ≈ upload÷5
- **Самообучение**: только мелкие CIDR, AS-блоки — постоянные
- **QR-код**: не влезает при >50 AllowedIPs, отправлять .conf файлом
- **VPN за границей**: неоптимально, рекомендация отключить
- **Новая блокировка вне крупных AS**: до 24ч через базы или моментально через /vpn add
- **do-release-upgrade**: запрещено, обновление ОС через переустановку + restore.sh
- **NAT timeout**: если VPN отваливается периодически → увеличить UDP NAT timeout на роутере

-----

## ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (.env.example)

### Домашний сервер
```bash
# Сеть (автоопределение)
HOME_SERVER_IP=
GATEWAY_IP=
NET_INTERFACE=
HOME_SUBNET=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_CHAT_ID=

# WireGuard
WG_HOST=            # внешний IP или DDNS
WG_AWG_PORT=51820
WG_WG_PORT=51821
WG_MTU=1320

# VPS (основной)
VPS_IP=
VPS_SSH_PORT=443    # или 22
VPS_TUNNEL_IP=10.177.2.2

# Hysteria2
HYSTERIA2_SERVER=
HYSTERIA2_AUTH=
HYSTERIA2_OBFS_PASSWORD=

# Xray (REALITY)
XRAY_SERVER=
XRAY_UUID=
XRAY_PUBLIC_KEY=
XRAY_SOCKS_PORT=1080

# Xray (gRPC)
XRAY_GRPC_UUID=
XRAY_GRPC_PUBLIC_KEY=
XRAY_GRPC_SOCKS_PORT=1081

# Cloudflare CDN
CF_TUNNEL_TOKEN=

# Watchdog
WATCHDOG_API_TOKEN=

# DDNS (опционально)
DDNS_PROVIDER=
DDNS_DOMAIN=
DDNS_TOKEN=

# Домен (опционально)
DOMAIN=
CF_API_TOKEN=

# Backup
BACKUP_GPG_PASSPHRASE=
BACKUP_VPS_HOST=
BACKUP_VPS_USER=

# Опции
INSTALL_PORTAINER=false
INSTALL_HOMEPAGE=false
INSTALL_WG_EASY=false
BANDWIDTH_LIMIT=
DEVICE_LIMIT_PER_CLIENT=5
```

### VPS
```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_CHAT_ID=
XRAY_PANEL_PASSWORD=
GRAFANA_PASSWORD=
MTLS_CA_PASSWORD=
SSH_ADDITIONAL_PORT=443
DOMAIN=
CF_API_TOKEN=
```

-----

## ТЕКУЩИЙ СТАТУС

Архитектурное проектирование полностью завершено. 10 раундов виртуального тестирования (105 тестов). Все найденные проблемы зафиксированы и решены.

**Начинать с**: Шаг 1 — установка Ubuntu Server 24.04 LTS.

После каждого шага ждать подтверждения.

-----

*Промпт v4.0 — гибрид B+, адаптивный failover 4 стека, plugin-архитектура,
nftables native sets + dnsmasq nftset=/, мульти-VPS, CDN через cloudflared,
самообучение AllowedIPs, make-before-break ротация, детекция шейпинга,
CIDR-агрегация ≤500 записей, автоустановка/автообновление/авторассылка,
105 тестов пройдено. 51 шаг.*

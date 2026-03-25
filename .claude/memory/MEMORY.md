# VPN Infrastructure — Память проекта

## Репозиторий
- GitHub: https://github.com/Cyrillicspb/vpn-infra
- Ветка: master (main — для PR)
- Локально: C:\Users\kiril\Documents\Google Диск\Мой диск\VibeCoding\vpn-infra

## Доступ к серверам
- **Домашний сервер**: root@80.93.52.223 (Санкт-Петербург, Россия)
  - SSH ключ генерировать через `ssh-keygen -t ed25519 -f /tmp/claude_vpn_key -N ""`
  - Публичный ключ надо добавить в `~/.ssh/authorized_keys` вручную (спросить пользователя)
  - VPS доступен через туннель: `ssh -i /root/.ssh/vpn_id_ed25519 sysadmin@10.177.2.2`
  - SCP на VPS: через `scp -i /root/.ssh/vpn_id_ed25519 file sysadmin@10.177.2.2:/tmp/`
- **VPS**: 23.95.252.178, sysadmin@10.177.2.2 (через туннель), sudo для root команд
  - Прямой SSH (port 22) не доступен извне России — только через WireGuard туннель

## Статус развёртывания (актуально на 2026-03-12)

### Что работает
- **Watchdog**: запущен стабильно (systemd, Type=notify), ~275s uptime без крашей
- **Стек reality-grpc (XHTTP, cdn.jsdelivr.net, порт 2083)**: ✅ РАБОТАЕТ
  - Трафик идёт через VPS 23.95.252.178
  - tun-grpc интерфейс поднят вручную, table marked маршрут активен
  - xray-client-2 (SOCKS5 :1081) → VPS:2083 splithttp+REALITY
- **Tier-2 SSH туннель**: autossh-tier2 (tun0), 10.177.2.1 (home) ↔ 10.177.2.2 (VPS)
- **3x-ui на VPS**: инбаунды VLESS-XHTTP-microsoft (2087) + VLESS-XHTTP-jsdelivr (2083)

### Что не работает / не настроено
- **Стек reality (XHTTP, microsoft.com, порт 2087)**: ❌ ТСПУ блокирует
- **Стек cloudflare-cdn**: ❓ cloudflared healthy, но VLESS+WS inbound не настроен
- **Hysteria2**: ❌ не настроен (standalone, 3x-ui не поддерживает Hysteria2)
- **combined.cidr**: отсутствует (`/etc/vpn-routes/combined.cidr не найден`)

### Текущее состояние на серверах (задеплоено 2026-03-12)
- **Watchdog**: стабильно работает, active_stack=reality-grpc, автостарт tun при перезапуске
- **tun-grpc**: поднят watchdog автоматически при старте, маршрут table marked → tun-grpc ✅
- **Трафик через VPS**: curl через tun-grpc → 23.95.252.178 ✅
- **combined.cidr**: 266 CIDR-записей, обновлён 2026-03-12, cron 03:00 ежедневно ✅
- **nftables blocked_static**: 36 946 правил активны ✅
- **dnsmasq**: 39 доменов, nftset blocked_dynamic работает ✅

## Архитектура (краткое)
- **Домашний сервер**: Ubuntu 24.04, AmneziaWG (wg0) + WireGuard (wg1)
- **VPS**: 3x-ui (Xray), cloudflared, Prometheus/Grafana
- **4 стека** (по убыванию устойчивости): CDN → XHTTP-jsdelivr → XHTTP-microsoft → Hysteria2
- **Split tunneling Hybrid B+**: AllowedIPs на клиенте (≤500 CIDR) + nftables fwmark на сервере
- **nft table**: `inet vpn` (НЕ `inet filter`)
- **Policy routing**: table "marked" (200) = fwmark 0x1 → tun, table "vpn" (100) = WG clients → gateway

## Ключевые сетевые параметры
- AWG клиенты: 10.177.1.0/24 (wg0), DNS: 10.177.1.1
- WG клиенты: 10.177.3.0/24 (wg1), DNS: 10.177.3.1
- Tier-2 туннель: 10.177.2.0/30 (home=.1, VPS=.2, интерфейс tun0, сервис autossh-tier2)
- Docker домашний: 172.20.0.0/24 (env var DOCKER_SUBNET)
- AWG порт: 51820, WG порт: 51821
- Watchdog API: 0.0.0.0:8080 (bearer token: в /opt/vpn/.env WATCHDOG_API_TOKEN)
- Routing tables: "vpn"=100 (WG→internet), "marked"=200 (fwmark 0x1→VPN tun)

## XHTTP стеки (Xray 26.x, заменили gRPC и vision)

### VPS инбаунды (3x-ui)
| Имя | Порт | SNI | UUID (env) | Password (env) |
|-----|------|-----|-----------|----------------|
| VLESS-XHTTP-microsoft | 2087 | microsoft.com | XRAY_UUID | XHTTP_MS_PASSWORD |
| VLESS-XHTTP-jsdelivr | 2083 | cdn.jsdelivr.net | XRAY_GRPC_UUID | XHTTP_CDN_PASSWORD |

### Клиентские конфиги
- `config-reality.json` → SOCKS5 :1080 → VPS:2087 splithttp REALITY microsoft.com
- `config-grpc.json` → SOCKS5 :1081 → VPS:2083 splithttp REALITY cdn.jsdelivr.net

### Tun интерфейсы (плагины)
- `tun-reality` (≤15 chars ✓) — плагин reality
- `tun-grpc` (8 chars ✓) — плагин reality-grpc (было tun-reality-grpc=16 chars, ТАК НЕЛЬЗЯ!)
- Tmp: `tun-reality-t` и `tun-grpc-tmp`

## Исправленные баги
- **Watchdog crash loop**: `_notify_systemd(b"WATCHDOG=1")` перенесён в НАЧАЛО итерации monitoring_loop
- **Watchdog startup**: `last_large_speedtest = time.time()` (было 0.0 → запускало 6ч-тест при старте)
- **Plugin tun name слишком длинное**: tun-reality-grpc (16) → tun-grpc (8)
- **make-before-break bug**: `_switch_stack` использовал `plugin.start(temp_port="1082")` + маршрут к `tun-{stack}` (несуществующий). Исправлено: `plugin.start()` без temp_port + маршрут берётся из `plugin.meta["tun_name"]`
- **Watchdog ping loop**: был внутри monitoring_loop (блокировался curl 30s). Исправлено: отдельный `asyncio.create_task(_watchdog_ping_loop())` каждые 10s
- **Watchdog startup**: не поднимал tun при старте. Исправлено: on_startup вызывает plugin.start() если tun не существует
- **Xray 26.x password**: XHTTP+REALITY требует `password` в splithttpSettings (иначе "empty password" error)
- **Port 2096 conflict**: 3x-ui subscription service занимает 2096 → переехали на 2083

## Ключевые файлы

### Корень репозитория
- `setup.sh` — мастер установки (51 шаг, фазы 0-5)
- `install-home.sh`, `install-vps.sh`, `deploy.sh`, `restore.sh`

### home/
- `docker-compose.yml` — xray-client/xray-client-2 на `teddysun/xray:latest`
- `xray/config-reality.json` — XHTTP шаблон (microsoft.com, порт 2087)
- `xray/config-grpc.json` — XHTTP шаблон (cdn.jsdelivr.net, порт 2083)
- `watchdog/watchdog.py` — FastAPI async, WATCHDOG=1 ping в начале итерации
- `watchdog/plugins/reality/` — tun_name: tun-reality, SOCKS :1080
- `watchdog/plugins/reality-grpc/` — tun_name: tun-grpc, SOCKS :1081

### vps/
- `docker-compose.yml` — 3x-ui (host), nginx:8443, cloudflared, prometheus, grafana

## Что нужно сделать в следующей сессии
1. **Сквозной тест**: подключить WireGuard/AWG клиент → home → tun-grpc → VPS → заблокированный сайт
   - Проверить что nftables fwmark работает (blocked_static → table marked → tun-grpc)
   - Проверить DNS через dnsmasq (nftset blocked_dynamic)
2. **Telegram-bot**: проверить работу бота (combined.cidr готов, watchdog работает)
   - Зарегистрироваться как клиент, получить конфиг, подключиться
3. **cloudflare-cdn стек**: настроить VLESS+WS inbound на VPS (localhost:8080) + cloudflared tunnel
4. **Hysteria2**: настроить standalone сервис (не через 3x-ui)
5. **Стек reality (порт 2087)**: ❌ заблокирован ТСПУ — пока не трогать

## Подробнее
Детали архитектуры: memory/architecture.md

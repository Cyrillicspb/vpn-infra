# Архитектура

## Назначение

Система решает три задачи:

- даёт клиентам один стабильный ingress на home-server;
- уводит blocked/VPN-трафик через VPS;
- оставляет обычный трафик direct, если policy не требует обратного.

Главный принцип: клиенты знают только home ingress. Home-server остаётся control plane, watchdog принимает runtime-решения, VPS выступает execution backend для blocked/VPN lane.

## Основные узлы

### Home-server

На home-server живут:

- `wg0` и `wg1` для клиентских подключений;
- `dnsmasq` и `nftables` для split tunneling;
- `watchdog` как decision engine и HTTP API;
- Telegram-бот;
- клиентские outbound-стэки к VPS;
- `deploy.sh`, `restore.sh`, smoke и post-install проверки.

### VPS

На VPS живут:

- публичные inbound-стэки;
- вспомогательные reverse-proxy и admin components;
- git mirror и parity state для deploy;
- backup/recovery артефакты.

## Control Plane

Источник истины для состояния системы:

- `/opt/vpn/.env` для секретов и инсталляционных параметров;
- `/opt/vpn/.deploy-state/` для deploy/rollback состояния;
- watchdog API для runtime-операций;
- Telegram-бот как operator surface.

Watchdog и бот не должны определять успех deploy по stdout скриптов. Для этого используется только deploy-state contract.

## Client Endpoint Contract

- Клиентский `Endpoint` для WG/AWG всегда указывает на home ingress.
- `WG_HOST` относится только к home ingress.
- `HOME_DDNS_DOMAIN` относится только к home ingress DDNS.
- `VPS_IP` и любые VPS-hostname не должны использоваться как клиентский endpoint.
- Если в будущем появится отдельный hostname для операционного доступа к VPS, он должен жить отдельно от `WG_HOST`.

## Traffic Model

Система делит трафик на три логических lane:

- `direct`: обычный трафик идёт в интернет напрямую;
- `vpn`: blocked/static/dynamic трафик идёт через VPS lane;
- `latency_sensitive_direct`: отдельный direct-first слой для сервисов, которые должны обходить broad blocked CIDR.

Приоритет правил важен:

1. manual overrides;
2. `latency_sensitive_direct`;
3. blocked static/dynamic policy;
4. остальной трафик.

## Routing Data Sources

Runtime routing собирается из нескольких источников:

- статические базы blocked domains/IP ranges;
- `manual-vpn.txt`;
- `manual-direct.txt`;
- runtime latency catalog;
- runtime learned latency set;
- generated dnsmasq/nftables artifacts.

Bounded self-learning ограничен только известными service families из latency catalog. Он не должен свободно переводить произвольные домены между lane.

## External Stacks

Home-server использует набор outbound-стэков до VPS. Watchdog выбирает активный стек и может переключать его без изменения клиентских конфигов.

Документация не фиксирует один конкретный preferred stack как вечную истину. Актуальное состояние нужно смотреть через:

- `/status`;
- `/tunnel`;
- watchdog `/status`.

## Deploy And Recovery

### Deploy

`deploy.sh` обновляет home и VPS как один release:

- fetch target release;
- create snapshot;
- apply home;
- apply VPS;
- verify через health gate;
- commit или rollback.

### Rollback

`deploy.sh --rollback` возвращает систему к последнему подтверждённому snapshot release.

### Disaster recovery

`restore.sh` используется только для backup/DR и migration flows. Это не release rollback.

## Verification Baseline

Поддерживаемый operational baseline после install/deploy:

- `bash /opt/vpn/scripts/post-install-check.sh` зелёный;
- `bash /opt/vpn/tests/run-smoke-tests.sh` зелёный;
- `bash /opt/vpn/deploy.sh --status` показывает committed release без `pending`;
- deploy-state на home и VPS совпадает.

## Что не является текущим контрактом

В текущую архитектуру не входят как active commitments:

- GUI installer как основной install path;
- router-zapret / Keenetic backend;
- отдельный `vps-only` deploy;
- определение здоровья системы по логам вместо state/API.

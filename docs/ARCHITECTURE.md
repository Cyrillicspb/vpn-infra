# Архитектура

## Назначение

Система решает три задачи:

- даёт клиентам один стабильный ingress на home-server;
- уводит blocked/VPN-трафик через VPS;
- оставляет обычный трафик direct, если policy не требует обратного.

Главный принцип: клиенты знают только home ingress. Home-server остаётся control plane, VPS выступают execution backend для blocked/VPN lane.

При наличии нескольких VPS система больше не должна опираться на неявную логику внутри watchdog. Для этого вводится отдельный слой `Decision Maker`: он принимает authoritative routing/backend-решения, а нижележащие подсистемы только исполняют их и подтверждают результат.

## Основные узлы

### Home-server

На home-server живут:

- `wg0` и `wg1` для клиентских подключений;
- `dnsmasq` и `nftables` для split tunneling;
- `Decision Maker` как policy/backend brain;
- `watchdog` как health, self-heal и execution engine;
- Telegram-бот;
- клиентские outbound-стэки к VPS;
- `deploy.sh`, `restore.sh`, smoke и post-install проверки.

### VPS

На VPS живут:

- публичные inbound-стэки;
- вспомогательные reverse-proxy и admin components;
- git mirror и parity state для deploy;
- backup/recovery артефакты.

При multi-VPS каждый backend считается отдельным execution node с собственным health/runtime состоянием.

## Runtime topology

Базовая логика остаётся прежней:

- клиенты всегда подключаются к `home-server`;
- `home-server` остаётся ingress и control plane;
- VPS выступают execution backend для `blocked` / `vpn` lane;
- обычный direct-трафик не должен уходить на backend без policy-reason.

Типовые сегменты адресов:

- `10.177.1.0/24` — AWG клиенты;
- `10.177.3.0/24` — WG клиенты;
- `172.20.0.0/24` — home Docker;
- дополнительные runtime/bridge сегменты допускаются, но не должны менять client-facing ingress model.

## Control Plane

Источник истины для состояния системы:

- `/opt/vpn/.env` для секретов и инсталляционных параметров;
- `/opt/vpn/.deploy-state/` для deploy/rollback состояния;
- Decision Maker API для routing/backend-решений;
- watchdog API для health/runtime-операций;
- Telegram-бот как operator surface.

Watchdog и бот не должны определять успех deploy по stdout скриптов. Для этого используется только deploy-state contract.

## Layer Model

### 1. Identity Layer

Определяет источник трафика до того, как система начинает выбирать маршрут.

- `homeserver mode`: только `vpn_client`;
- `gateway mode`: `vpn_client`, `lan_client`, `unknown`.

Нормализованный identity object должен содержать:

- `identity_type`;
- `identity_id`;
- `source_ip`;
- `mode`.

`LAN client identity` допускается только в `gateway mode`. В обычном `homeserver mode` LAN-идентификация не должна появляться как псевдо-клиентская модель.

### 2. Decision Maker

`Decision Maker` становится главным brain-слоем системы.

Он получает:

- source identity;
- destination context (`domain/ip/cidr/service`);
- global policy;
- client or LAN preferences;
- backend inventory;
- backend health snapshot;
- current assignments/leasing state.

Он возвращает:

- `route_mode`;
- `route_class`;
- `effective_backend_id`;
- `desired_backend_path`;
- `decision_source`;
- `fallback_reason`;
- `explanation`.

Ключевое правило: `Decision Maker` принимает решение, но не применяет его к dataplane сам.

### 3. Execution Layer

Execution Layer применяет решение `Decision Maker`:

- перерендеривает backend-dependent runtime configs;
- переключает active backend или assignment;
- перезапускает нужные сервисы;
- обновляет routing/runtime state;
- верифицирует, что dataplane реально пришёл в требуемое состояние.

Сюда входят:

- tier-2;
- outbound transport clients (`xray`, `hysteria2`, `tuic`, `trojan` и т.д.);
- routing glue на `home`.

### 4. Monitoring Layer

Monitoring не должен быть одним общим “зелёный/красный” флагом. Он делится как минимум на:

- `home health`;
- `backend health`;
- `stack health`;
- `routing health`.

Этот слой поставляет факты в `Decision Maker` и `watchdog`, но не принимает policy-решения сам.

## Responsibility Split

### Decision Maker owns

- backend inventory model;
- policy resolution;
- precedence rules;
- backend selection;
- stickiness/lease logic;
- decision explanations;
- effective assignment state.

### Watchdog owns

- health collection;
- self-heal/failover execution;
- stack lifecycle;
- applying decisions to runtime;
- alerting;
- runtime verification after apply.

### Bot owns

- operator UX;
- menu surface;
- CRUD для preferences и backend controls;
- diagnostics presentation.

Бот не должен принимать routing-решения самостоятельно.

### Deploy owns

- rollout кода и конфигов;
- consistency checks;
- backend-aware release apply;
- rollback orchestration.

Deploy не должен становиться policy engine.

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

При multi-VPS этого уже недостаточно. После traffic verdict добавляется ещё один decision step:

1. определить `route_mode`;
2. определить `route_class`;
3. через `Decision Maker` выбрать `effective backend`;
4. передать решение в Execution Layer.

Серверный split tunneling по-прежнему опирается на `dnsmasq`, `nftables` и policy-routing:

- `blocked_static` / `blocked_dynamic` идут в `vpn` lane;
- `latency_sensitive_direct` остаётся direct-first исключением;
- остальной трафик идёт direct.

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

Если backend больше одного, система должна мыслить не одним `VPS_IP`, а backend pool.

Текущая фаза foundation:

- один `active backend` для всего `vpn`/`blocked` lane;
- single-backend backward compatibility;
- active backend может быть switched/drained/auto-selected.
- runtime status уже нормализован как:
  - `desired_backend_path`
  - `applied_backend_path`
  - `backend_path_status`
    - `desired_matches_applied`
    - `applied_matches_active`
    - `reconciled`
    - `verified`

Текущее безопасное локальное состояние контракта:

- `Decision Maker` уже владеет canonical read-side shaping для:
  - `/decision/status`
  - `/decision/backends`
  - `/decision/assignments`
  - `/decision/backend-paths`
- `hysteria2 backend_path` уже нормализован как набор отдельных shapes:
  - target
  - runtime record
  - verify record
  - path entry
  - path status summary
- `watchdog` на этом уровне только собирает runtime facts и передаёт их в `Decision Maker`.
    - `applied_matches_active`
    - `reconciled`
    - `verified`

Целевая фаза:

- `Decision Maker` выбирает backend для конкретного route-class;
- дальше появляются leases/stickiness;
- затем source-specific preferences;
- затем, только в `gateway mode`, LAN-specific preferences.

Это policy-aware backend selector, а не client-visible L4 load balancer.

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

При multi-VPS deploy должен оставаться cluster-aware, но не должен сразу требовать одинаково строгий success для всех backend’ов. Рабочая модель:

- есть `home`;
- есть `primary backend`;
- есть optional additional backends;
- primary backend обязателен для committed release;
- остальные backend’ы могут быть degraded, но должны быть явно отражены в status/alerts.

### Rollback

`deploy.sh --rollback` возвращает систему к последнему подтверждённому snapshot release.

Rollback остаётся release-механизмом, а не policy-механизмом:

- rollback не восстанавливает пользовательские preferences “по логам”;
- rollback возвращает code/config/runtime contract;
- после rollback `Decision Maker` и `watchdog` должны уметь безопасно пересчитать effective assignments из authoritative state.

### Disaster recovery

`restore.sh` используется только для backup/DR и migration flows. Это не release rollback.

## Verification Baseline

Поддерживаемый operational baseline после install/deploy:

- `bash /opt/vpn/scripts/post-install-check.sh` зелёный;
- `bash /opt/vpn/tests/run-smoke-tests.sh` зелёный;
- `bash /opt/vpn/deploy.sh --status` показывает committed release без `pending`;
- deploy-state на home и VPS совпадает.

Для multi-VPS к этому добавляется:

- backend pool виден в status;
- active backend и switch reason объяснимы;
- backend health не расходится с effective runtime state;
- self-heal не уводит required VPN traffic в silent direct fallback.

## Health, Self-Heal, Monitoring

### Health

Health model делится на четыре зоны:

- `home health`;
- `backend health`;
- `stack health`;
- `routing health`.

### Self-Heal

Self-heal должен работать только в рамках явных правил:

- restart/reload runtime services;
- смена active stack;
- смена active backend;
- controlled failover при unhealthy backend;
- без silent direct fallback для traffic, который policy требует вести через VPN lane.

Self-heal не должен сам менять долгосрочные policy preferences.

### Monitoring

Monitoring должен уметь ответить:

- какой backend сейчас effective;
- почему выбран именно он;
- это manual override, auto-choice или fallback;
- healthy ли backend, stack и routing path;
- применено ли решение в runtime реально.

Иначе multi-VPS система становится необъяснимой оператору.

## Decision Maker API Contract

Даже если первая реализация живёт как модуль в watchdog process, интерфейс должен проектироваться как API.

Read-side:

- `resolve_route(context)`
- `explain_route(context)`
- `list_backends()`
- `get_backend_health()`
- `get_assignments()`

Write-side:

- `set_backend_state(drain/undrain/weight)`
- `clear_assignment(route_class|all)`
- `force_backend(...)`
- `auto_select_backend(...)`

Execution-facing:

- `get_desired_runtime_state()`
- `ack_applied_state(...)`

Это нужно для того, чтобы bot, diagnostics, self-heal и dataplane смотрели в один и тот же authoritative decision layer.

## Что не является текущим контрактом

В текущую архитектуру не входят как active commitments:

- консольный install path как равноправный primary UX;
- router-zapret / Keenetic backend;
- отдельный `vps-only` deploy;
- определение здоровья системы по логам вместо state/API;
- client-visible multi-VPS ingress balancing;
- смешение LAN identity с обычными VPN clients вне `gateway mode`.

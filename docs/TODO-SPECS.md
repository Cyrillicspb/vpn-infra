# TODO — Подробные спецификации

Этот документ больше не является плоским списком “всего подряд”.
Он разделён на:
- короткий актуальный backlog;
- детальные спецификации, которые либо ещё актуальны, либо оставлены как архив решений.

Краткий операционный backlog держим компактным. Всё, что уже реализовано, отменено или отложено без даты, не должно конкурировать с живыми задачами.

## Актуальный backlog

На 4 апреля 2026 года в живом backlog остаются только эти направления:

- [ ] Разделить `HOME_DDNS_DOMAIN` / `WG_HOST` и отдельный VPS hostname, чтобы ingress для клиентов нельзя было случайно направить на VPS.
- [ ] Расширить functional scenarios routing/smoke под банки, маркетплейсы, Bitrix24 и телекомы.
- [~] Multi-VPS / Decision Maker Phase 2: вынести backend selection и route explanation в отдельный `Decision Maker` contract.
- [~] Multi-VPS / Decision Maker Phase 3: `vpn_client` backend preferences (`service/domain/cidr`) поверх `Decision Maker`.
- [~] Multi-VPS / Decision Maker Phase 4: `LAN client identity` и LAN backend preferences только для `gateway mode`.

## Уже закрыто

- [x] Deploy/rollback state machine, rollback contract и health gate приведены в рабочее состояние.
- [x] Smoke и post-install checks очищены от ложных срабатываний и приведены к зелёному baseline.
- [x] Bounded self-learning и runtime latency catalog уже реализованы и используются в текущем routing pipeline.
- [x] Multi-admin уже реализован и не является активной задачей.
- [x] `systemd` timer для `update-latency-catalog.py` добавлен.
- [x] Operator surface для `latency-learned` / `latency-candidates` добавлен в Telegram-бот.
- [x] Alert на пустой или устаревший runtime latency catalog добавлен в watchdog.
- [x] Multi-VPS Phase 1 foundation: backend pool, active backend, health/drain, menu-complete operator surface.

## Архив / не в активной разработке

Эти разделы ниже сохраняются как reference-спеки, но сейчас не считаются текущим backlog:

- Email fallback для алертов.
- Полуавтоматическое обновление DPI-presets.
- GUI-установщик.
- Gateway Mode и Router-Zapret / Keenetic expansion.
- Полный export/import для чистой переустановки.
- WiFi как резервный канал.

-----

## ACTIVE — Multi-VPS / Decision Maker Architecture

**Приоритет: высокий**

### Цель

Перестать разносить routing/backend-логику между watchdog, bot, scripts и runtime эвристиками.

Нужен отдельный authoritative слой `Decision Maker`, который:

- принимает routing/backend-решения;
- учитывает health, policy и preferences;
- объясняет, почему решение именно такое;
- согласован с self-heal, monitoring, deploy и rollback.

### Что уже сделано

- [x] `backend pool` введён как runtime сущность.
- [x] Есть `active backend` foundation для всего VPN lane.
- [x] Есть backend health/drain/manual/auto switch.
- [x] Bot menu уже знает backend-oriented operations.
- [x] Выделен `decision_maker.py` как отдельный модуль.
- [x] Появился canonical `/decision/*` API для read/explain/reassign/choose/apply.
- [x] Bot `/check` и backend actions уже используют canonical Decision Maker flow.

### PHASE 2 — DECISION MAKER CONTRACT

#### 1. Выделить Decision Maker как отдельную подсистему

- [ ] Явно вынести `resolve_route(...)`
- [x] Явно вынести `explain_route(...)`
- [x] Явно вынести backend selection API
- [~] Не смешивать Decision Maker с runtime apply

#### 2. Разделить ownership

- [ ] `Decision Maker` владеет policy/backend decision state
- [ ] `watchdog` владеет health/self-heal/runtime apply
- [ ] `bot` владеет UX/CRUD/diagnostics presentation
- [ ] `deploy` не становится policy engine

#### 3. Согласовать с health/self-heal/monitoring

- [ ] backend health используется как input, а не как сам decision layer
- [ ] self-heal применяет решения, а не изобретает policy
- [ ] monitoring объясняет effective backend и switch reason
- [ ] no silent direct fallback для VPN-required traffic

**Текущее состояние Phase 2:**

- explanation path уже вынесен;
- `resolve_route(...)` уже появился для domain decision path;
- choose/apply flow уже разделён;
- bot уже смотрит в canonical Decision Maker API;
- remaining gap: более явное отделение decision state от watchdog runtime state и перенос preference precedence в новый resolver.

### PHASE 3 — VPN CLIENT PREFERENCES

- [~] `vpn_client` preferences (`service/domain/cidr`)
- [~] precedence rules поверх global policy
- [x] `Decision Maker` explanation path для `/check`
- [x] menu-complete bot surface для preferences

**Текущее состояние Phase 3:**

- добавлена таблица `client_backend_prefs` в bot DB;
- `Decision Maker resolve_route(...)` уже учитывает `vpn_client` preferences по `service/domain/cidr`;
- `/check <domain> [chat_id]` уже показывает `matched_preference` и fallback;
- precedence уже частично формализован: matching preference не ломает `direct`/`latency_sensitive_direct` verdict и явно показывает `ignored_by_policy`;
- в меню есть entry point `Client backend prefs`, plus menu-path для add/remove preferences;
- remaining gap: развить precedence поверх global policy и добавить более богатый UX фильтрации/редактирования, если это понадобится.

### PHASE 4 — GATEWAY-ONLY LAN IDENTITY

- [x] `LAN client identity` только в `gateway mode`
- [x] отдельный store для LAN prefs
- [x] не смешивать LAN clients с Telegram-managed VPN clients
- [~] gateway-only menu and diagnostics

**Текущее состояние Phase 4:**

- добавлен отдельный gateway-only store:
  - `gateway-lan-clients.json`
  - `gateway-lan-prefs.json`
- добавлены watchdog API:
  - `/gateway/lan-clients`
  - `/gateway/lan-prefs`
  - `/gateway/lan-client/upsert|remove`
  - `/gateway/lan-pref/add|remove`
- bot получил gateway-only surface:
  - `Gateway` section в меню
  - `/lan_clients`
  - `/lan_client ...`
  - `/lan_backend_pref ...`
- вне `gateway mode` эти действия отвечают `not applicable`;
- `Decision Maker resolve_route(...)` уже умеет принимать `lan_client` identity и `LAN backend preferences`;
- `/check <domain> <source_ip>` в `gateway mode` теперь показывает `lan_client` identity и effective backend decision;
- remaining gap: сделать отдельный richer UX для явного выбора `lan_client` из меню, а не только через команды и diagnostics.

### PHASE 5 — ROUTE-CLASS LEASES

- [~] перейти от одного active backend к lease per `route_class`
- [~] sticky assignments с TTL
- [ ] controlled rebalance
- [x] explanation/fallback для lease decisions

**Текущее состояние Phase 5:**

- lease state уже живёт в `backend_assignments` и имеет TTL;
- `Decision Maker` и bot diagnostics уже показывают lease-driven `route_class`;
- execution layer сейчас честно помечен как `single_active_backend`;
- пока dataplane не умеет реальный per-class backend execution, assignment choice принудительно согласован с active backend;
- добавлен controlled reconciliation path для lease state после смены active backend;
- добавлен dataplane foundation для `hysteria2`: per-backend rendered configs и `backend_paths` diagnostics с `desired/applied/rendered/verified`;
- `backend apply` для `hysteria2` теперь проходит через `verify` и делает rollback на предыдущий backend при failed probe;
- required runtime checks теперь отделяют optional residue (`extra-stacks`) от core default execution path;
- remaining gap: controlled rebalance и настоящий per-class execution path ниже decision layer.

### PHASE 6 — MULTI-BACKEND DEPLOY / ROLLBACK

- [ ] backend-aware deploy verification
- [ ] policy/runtime reconciliation after rollback
- [ ] explicit degraded/additional-backend semantics

### Definition of Done

- [ ] routing decision идёт через один authoritative layer
- [ ] bot, diagnostics и runtime используют один decision contract
- [ ] health/self-heal/monitoring не расходятся по ownership
- [ ] deploy/rollback знают про multi-backend без смешения с policy

-----

## ACTIVE — Разделить home-ingress DDNS и VPS hostname

**Приоритет: высокий**

Сейчас в установке и `.env` есть только один `DDNS_DOMAIN`, и исторически его слишком легко трактовать двусмысленно:
- как домен для клиентского `WG/AWG Endpoint`;
- как домен/alias для VPS или миграции между VPS.

Это приводит к архитектурной ошибке: клиентский `Endpoint` должен всегда указывать на home ingress, тогда как VPS-адресация и миграция должны жить отдельно.

**Что уже зафиксировано:**
- `WG_HOST` и клиентский `Endpoint` относятся только к home ingress;
- `ROUTER_EXTERNAL_IP` в `gateway` mode тоже относится только к home ingress;
- `VPS_IP` остаётся отдельным source of truth для внешних стеков, SSH, backup и операционных задач;
- DDNS updater должен публиковать WAN IP домашнего роутера, а не VPS egress IP.

**Что нужно сделать дальше:**
- Добавить в install flow явное разделение двух сущностей:
  - `HOME_DDNS_DOMAIN` / `WG_HOST` для клиентского ingress;
  - отдельный `VPS_HOSTNAME` или `VPS_MIGRATION_HOSTNAME` только для операционных сценариев, если он вообще нужен.
- Пересмотреть `.env`, TUI installer и `setup.sh`, чтобы второй hostname не мог случайно переопределить `WG_HOST`.
- Явно описать в docs, какой hostname используется:
  - для генерации AWG/WG client config;
  - для VPS migration / operator access;
  - для backup/restore и mirror.
- Если отдельный VPS hostname реально вводится:
  - не использовать его в `config_builder.py`;
  - не использовать его в hairpin logic;
  - не использовать его в `router_external_ips`.

**Критерий готовности:**
- installer задаёт правильные вопросы без двусмысленности;
- клиентский `Endpoint` нельзя случайно направить на VPS;
- отдельный VPS hostname, если нужен, не влияет на AWG/WG client config generation.

-----

## ACTIVE — CIDR Aggregation & Routing Intelligence

**Приоритет: высокий**

### Цель

Повысить точность `combined.cidr`, уменьшить over-inclusion, сохранить стабильность и адаптировать routing под разные типы клиентов.

### PHASE 1 — SAFETY BASELINE

#### 1. Ограничение агрегации

- [ ] Запретить автоматическую агрегацию `/8`
- [ ] Запретить автоматическую агрегацию `/7`
- [ ] Разрешать `/9` и `/10` только через whitelist
- [ ] Ввести глобальный параметр `MAX_AGGREGATION_PREFIX=/12`

#### 2. Expansion ratio check

- [ ] Добавить расчёт `expansion_ratio = merged_size / original_size`
- [ ] Ввести пороги: `<=4` → OK
- [ ] Ввести пороги: `<=8` → допустимо
- [ ] Ввести пороги: `>8` → запрещено

#### 3. Базовая нормализация списка

- [ ] Найти и удалить явно избыточные CIDR
- [ ] Разбить слишком широкие диапазоны
- [ ] Проверить отсутствие `/8` в итоговом списке

### PHASE 2 — CLASS-BASED MODEL

#### 4. Ввести классы маршрутов

- [ ] `CLASS_A (critical)`
- [ ] `CLASS_B (observed)`
- [ ] `CLASS_C (candidate)`
- [ ] `CLASS_D (excluded)`

#### 5. Источники данных

- [ ] `CLASS_A`: `vpn-force.conf`
- [ ] `CLASS_A`: ручной список доменов
- [ ] `CLASS_B`: DNS logs
- [ ] `CLASS_B`: реальные подключения
- [ ] `CLASS_C`: единичные наблюдения
- [ ] `CLASS_D`: control-plane IP
- [ ] `CLASS_D`: LAN
- [ ] `CLASS_D`: management

#### 6. Разделить генерацию

- [ ] Генерация CIDR по классам отдельно
- [ ] Разные правила агрегации для каждого класса

### PHASE 3 — TRAFFIC OBSERVATION

#### 7. Сбор наблюдений

- [ ] Логировать DNS → IP
- [ ] Логировать частоту появления
- [ ] Логировать время жизни
- [ ] Хранить `first_seen`
- [ ] Хранить `last_seen`
- [ ] Хранить `hit_count`

#### 8. Confidence scoring

- [ ] Ввести score по частоте
- [ ] Ввести score по повторяемости
- [ ] Ввести score по количеству клиентов
- [ ] Ввести threshold для promotion
- [ ] Ввести threshold для demotion

#### 9. Candidate pipeline

- [ ] Candidate-список отдельно от production
- [ ] Candidate не влияет напрямую на `combined.cidr`

### PHASE 4 — PROMOTION / DEMOTION

#### 10. Promotion logic

- [ ] Перевод `candidate → observed` только при достижении threshold
- [ ] Перевод `observed → stable` только при долгосрочной стабильности

#### 11. Demotion logic

- [ ] Ввести TTL для downgrade
- [ ] Не удалять сразу: сначала переводить в `stale`

### PHASE 5 — DEVICE PROFILES

#### 12. Ввести профили клиентов

- [ ] `mobile_legacy`
- [ ] `mobile_modern`
- [ ] `desktop`
- [ ] `power`

#### 13. Ограничения профилей

- [ ] `mobile_legacy`: минимальный список
- [ ] `mobile_legacy`: больше агрегации
- [ ] `mobile_modern`: баланс
- [ ] `desktop`: больше точности

#### 14. Генерация per-profile

- [ ] Один `base list`
- [ ] Разные финальные `AllowedIPs`

### PHASE 6 — RUNTIME VALIDATION

#### 15. Проверка over-inclusion

- [ ] Измерять процент трафика, который лишне идёт через VPN
- [ ] Порог `<5%` → OK
- [ ] Порог `>15%` → требует оптимизации

#### 16. Проверка coverage

- [ ] Проверять, что нужные домены реально обходятся
- [ ] Тестировать blocked domains
- [ ] Тестировать API endpoints

### PHASE 7 — HEALTH INTEGRATION

#### 17. Улучшить health-check

- [ ] Убрать ложные warnings для unused `dpi_direct`
- [ ] Убрать ложные warnings для `nfqws inactive`, если стек не используется
- [ ] Добавить `CIDR quality score`
- [ ] Добавить `over-inclusion score`

### PHASE 8 — OPTIONAL

#### 18. ASN-aware aggregation

- [ ] Проверять ASN consistency при merge
- [ ] Не агрегировать разные ASN

#### 19. Smart merge strategy

- [ ] Ограничить merge только внутри однородных сетей

#### 20. Simulation mode

- [ ] Генерировать `current vs optimized CIDR`
- [ ] Сравнивать размер
- [ ] Сравнивать покрытие
- [ ] Сравнивать лишний трафик

### Definition of Done

- [ ] Нет `/8` и `/7`
- [ ] Агрегация ограничена
- [ ] Список воспроизводим
- [ ] Нет массового лишнего трафика
- [ ] Клиенты стабильно работают
- [ ] Поддерживаются разные device profiles

### Критические замечания

#### Не делать сразу

- [x] Не внедрять полный auto self-learning
- [x] Вместо этого внедрить bounded self-learning:
  - только внутри известных service families из latency catalog
  - только после повторяемых blocked-path симптомов
  - без свободного автодобавления произвольных доменов
- [ ] Не внедрять агрессивный auto-merge
- [ ] Не внедрять полную per-user сегрегацию

#### Делать поэтапно

- [x] Сначала ограничения агрегации
- [x] Потом классы
- [ ] Потом профили
- [x] Потом наблюдения

#### Реализовано для latency-sensitive routing

- [x] `blocked_static` больше не допускает сверхширокие `/7-/8` и аналогично опасные broad CIDR
- [x] `latency_sensitive_direct` имеет direct-first precedence над `blocked_static` и `blocked_dynamic`
- [x] `.ru` / `.рф` получают direct-first не только через DNS, но и через отдельный nft set
- [x] Добавлен fallback catalog российских сервисов и их bootstrap/auth/CDN зависимостей
- [x] Добавлен runtime catalog: `/etc/vpn-routes/latency-catalog.json`
- [x] Добавлен runtime overlay: `/etc/vpn-routes/latency-catalog-overlay.json`
- [x] Добавлен runtime learned-set: `/etc/vpn-routes/latency-learned.txt`
- [x] Добавлен runtime candidates-store: `/etc/vpn-routes/latency-candidates.json`
- [x] Добавлен updater: `update-latency-catalog.py`
- [x] `/check` теперь показывает service attribution и source tags
- [x] Self-learning ограничен catalog-мэтчем и не перебивает `manual-vpn`

#### Следующие шаги

- [ ] Добавить systemd timer для `update-latency-catalog.py`
- [ ] Расширить functional scenarios под банки, маркетплейсы, Bitrix24, телекомы
- [ ] Добавить операторские команды для просмотра learned/candidates из Telegram-бота
- [ ] Добавить алерт если runtime catalog слишком старый или пустой

-----

## ARCHIVE — Email fallback для алертов при недоступности Telegram

Этот раздел сохранён как идея. В текущий backlog не входит.

**Приоритет: низкий**

Когда Telegram API недоступен (режим белых списков), watchdog отправляет алерты через email.
- SMTP: smtp.yandex.ru / smtp.mail.ru (российские, в белом списке)
- Реализация: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL` в `.env`; watchdog при N неудачных попытках Telegram переключается на email
- Обратное переключение: при восстановлении Telegram API — снова через Telegram

-----

## PARKED — Полуавтоматическое обновление пресетов DPI-bypass

Раздел оставлен как reference-идея. Сейчас не является активной задачей.

- **Источник**: v2fly/domain-list-community — только как источник доменов для уже известных сервисов. НЕ автодискавери новых сервисов из v2fly (их там ~400).
- **Набор сервисов фиксированный**: youtube, instagram, twitter, tiktok, spotify, steam. Новый сервис добавляется только через код (V2FLY_MAPPING) или вручную через `/dpi add`. Cron обновляет домены, не сервисы.
- **Три слоя**: tier1=core (hardcoded, неудаляемые), tier2=v2fly (обновляемые), tier3=locked (пользователь через бот).
- **Хранение**: `/etc/vpn/dpi-presets.json` (cron пишет) + `home/dpi/presets-default.json` (fallback в репо). `DPI_SERVICE_PRESETS` в watchdog загружается из файла, не захардкожен.
- **Фильтрация v2fly**: исключать api./auth./login./accounts. домены; дедупликация (родитель покрывает поддомены в dnsmasq); cap 15 доменов на сервис.
- **Расписание**: systemd timer, воскресенье 03:30, после обновления маршрутов.
- **Watchdog**: новый endpoint `POST /dpi/presets/reload` для hot-reload без рестарта; алерт если presets не обновлялись >30 дней.
- **Самообучение**: conntrack-счётчик хитов по доменам из dpi_direct; домены с нулевым трафиком за 30 дней → предложение удалить (алерт админу).

-----

## ARCHIVE — GUI-установщик

Отложено без срока. Текущий install/deploy path остаётся shell-first.

**Архитектура: TUI на сервере, лаунчер на клиенте**
- TUI (Python + Textual) запускается на сервере — там уже есть Python (нужен для watchdog)
- Клиент — только SSH-терминал (встроен в Windows 10+, macOS, Linux). Никаких зависимостей на клиенте.
- Лаунчер (`.bat` / `.command` / `.sh`) делает одно: SSH-ключ + scp installer.py + `ssh -t server python3 installer.py`
- Все экраны, детект сети, тест VPS, запуск `setup.sh` — внутри TUI на сервере через subprocess

**Лаунчеры — единый flow, красивый вывод:**
- Windows: `.bat` с ANSI-цветами (VTP, cmd Windows 10+)
- macOS: `.command` (bash + ANSI)
- Linux: `.sh` (bash + ANSI, тот же скрипт что macOS)
- Единственный вопрос на клиенте: IP сервера
- Пошаговый прогресс: `✓ SSH доступен`, `→ Копирование ключа...`, `✓ Готово`
- Пароль запрашивается один раз (для SSH-ключа), после — только ключ

**TUI-установщик (Textual, 10 экранов):**
- Layout: header (шаг N/8) + content + footer (Назад / Помощь / Далее)
- Экраны: Приветствие → Подключение → Автодетект сети + Режим → VPS → Telegram → Опции → Review → Установка → Завершение
- Экран "Автодетект сети": LAN IP, интерфейс, CGNAT; кнопки `[A] Сервер на хостинге` / `[B] Сервер дома за роутером`
- Экран "Telegram": поле TOKEN + ID; подпись: root-администратор, неудаляем, `/admin invite` для других
- Inline-валидация, [Далее] заблокирован до прохождения
- [Проверить подключение] на экранах сервера и VPS
- [? Помощь] — side-panel с контекстной инструкцией
- Экран установки: прогресс-бар + консоль, `##PROGRESS:N:51:описание`
- State persistence: `~/.vpn-installer-state.json`

**Структура файлов:**
```
installers/
├── gui/
│   ├── installer.py       ← Textual App
│   ├── screens/           ← по файлу на экран
│   ├── components/        ← ValidatedInput, ProgressPane, ConsolePane
│   └── state.py           ← InstallerState + JSON persist
└── bootstrap.sh
```

-----

## ARCHIVE — Режим B — Gateway Mode

Спека сохранена как reference для возможного отдельного проекта. В текущий roadmap не входит.

Сервер физически дома за роутером. Роутер имеет белый IP + port forward. Сервер становится шлюзом для всей домашней сети.

```
РОУТЕР (80.93.52.223, port forward UDP 51820/51821 → 192.168.1.100)
  │ LAN 192.168.1.0/24
  ├── ДОМАШНИЙ СЕРВЕР (192.168.1.100) ← шлюз
  ├── Smart TV     (gateway=192.168.1.100) ← прозрачный VPN
  ├── Консоль      (gateway=192.168.1.100) ← прозрачный VPN
  └── ПК           (gateway=192.168.1.100) ← прозрачный VPN

Мобильные AWG/WG клиенты — endpoint = IP роутера
  Дома (WiFi): HAIRPIN redirect → WireGuard ✅
  Вне дома:    интернет → роутер → port forward → сервер ✅
```

### 1. HAIRPIN NAT
```
prerouting_nat (type nat hook prerouting priority dstnat):
iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr $ROUTER_EXTERNAL_IP
    udp dport 51820 redirect to :51820
iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr $ROUTER_EXTERNAL_IP
    udp dport 51821 redirect to :51821
```
Динамический IP роутера: nft set `router_external_ips`, watchdog обновляет.

### 2. Split tunneling для LAN-устройств (nftables)
```
chain prerouting (mangle):
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @dpi_direct    meta mark set 0x2 accept
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @blocked_static  meta mark set 0x1 accept
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @blocked_dynamic meta mark set 0x1 accept

chain forward:
  ip saddr $LAN_SUBNET ip daddr @blocked_static  oifname != "tun*" drop  # Kill switch LAN
  ip saddr $LAN_SUBNET ip daddr @blocked_dynamic oifname != "tun*" drop
  iifname $LAN_IFACE ip saddr $LAN_SUBNET accept
  oifname $LAN_IFACE ip daddr $LAN_SUBNET accept

chain postrouting:
  ip saddr $LAN_SUBNET oifname "tun*" masquerade  # Только для tun, не eth0
```

### 3. Policy routing для LAN
- `ip rule add priority 200 from $LAN_SUBNET lookup 100`
- table 100: `default via 192.168.1.1 dev $LAN_IFACE`

### 4. dnsmasq — слушать на LAN
- `interface=$LAN_IFACE` в dnsmasq.conf
- Smart TV → DNS → dnsmasq → VPS DNS → IP в blocked_dynamic → fwmark → tun

### 5. Failsafe
- tun упал: незаблокированное ✅, заблокированное → kill switch DROP ✅
- nftables упал: forward drop → watchdog мониторит 30 сек → перезагрузка
- Сервер недоступен: LAN теряет шлюз (PoF) → документировать ИБП

### 6. Изменения в setup.sh
- Фаза 0: `[A] Сервер на хостинге  [B] Сервер дома за роутером`
- .env: `SERVER_MODE=gateway`, `LAN_IFACE`, `LAN_SUBNET`, `ROUTER_EXTERNAL_IP`
- nftables-gateway.conf.j2 (отдельный шаблон)
- Фаза 5: инструкция по port forward + DHCP gateway/DNS

### 7. Watchdog дополнения
- Мониторинг nftables 30 сек → перезагрузка при пустом ruleset
- /status: кол-во LAN-клиентов из conntrack
- nft set router_external_ips обновлять при смене IP

### 8. Gateway-only меню
- `LAN Clients`
  Список IP, hostname, MAC, last seen, активные conntrack-сессии, какой стек сейчас обслуживает трафик.
- `Gateway Status`
  `SERVER_MODE`, `LAN_IFACE`, `LAN_SUBNET`, `HOME_SERVER_IP`, `ROUTER_EXTERNAL_IP`, активный стек, dnsmasq на LAN, hairpin NAT, kill switch.
- `DHCP/DNS Check`
  Какие клиенты реально используют `192.168.1.201` как DNS и какие ещё ходят мимо.
- `LAN Routes`
  Счётчики по `blocked_static`, `blocked_dynamic`, `dpi_direct`, текущие `ip rule` и `table 100/200`.
- `Top LAN Talkers`
  Какие IP сейчас больше всего гонят трафик через gateway.
- `Router Reachability`
  Проверка `router_external_ips`, hairpin NAT для `51820/51821`, доступности AWG/WG из LAN.
- `Bypass/Exclude`
  Быстро исключить конкретный LAN IP из gateway-режима.

### 9. Что НЕ меняется
Плагины стеков, failover, конфиг-билдер, бот, базы РКН, AllowedIPs. Конфиги клиентов: endpoint = ROUTER_EXTERNAL_IP, HAIRPIN решает.

-----

## ARCHIVE — Router-Zapret Extension For Behind-Router Mode

Спека сохранена как reference. Сейчас не является активным направлением.

## Summary

Добавить в архитектуру третий управляемый исполнительный контур: `router-zapret` на Keenetic, управляемый исключительно с `home-server` по SSH.
`home-server` остаётся единственным control plane и source of truth; VPS и Keenetic становятся execution backends.
Решение вводится только для `server_mode=B` (`home-server` за роутером), с rollout по умолчанию: установка и регистрация выполняются автоматически, но traffic policy остаётся выключенной, пока админ не включит её явно.

Ключевая модель после внедрения:

- `home-server` управляет состоянием системы
- `watchdog` принимает решения и применяет desired state
- VPS обслуживает blocked/VPN traffic
- Keenetic обслуживает DPI bypass на edge для YouTube и других `dpi_direct` сервисов
- Telegram-бот остаётся штатной operator surface поверх watchdog

## Architecture Changes

### 1. Новый execution backend: router-zapret

Добавить в систему понятие управляемого router backend для `zapret`, отдельное от локального `nfqws` на `home-server`.

Поведение:

- в `mode=A` локальный `zapret` на `home-server` остаётся штатным
- в `mode=B` система поддерживает два варианта DPI backend:
  - `local-home-zapret`
  - `router-zapret`
- для Keenetic backend считается внешним execution target, а не частью source tree на роутере

В `watchdog` зафиксировать отдельную сущность состояния:

- `dpi_backend_mode`: `local | router`
- `router_backend`: `none | keenetic`
- `router_backend_status`: `discovered | reachable | installed | configured | enabled | degraded`
- `router_backend_last_apply`, `router_backend_last_error`
- `router_backend_policy_mode`: `disabled | device_allowlist | full_lan`

### 2. Control plane: home-server -> SSH -> Keenetic

Основной и единственный поддерживаемый канал управления:

- SSH к Keenetic с `home-server`
- файловые операции через `scp/sftp`
- выполнение команд через `ssh`

Нужно ввести отдельный router access profile, аналогичный текущему VPS access:

- IP/hostname роутера в LAN
- SSH port
- username
- путь к ключу/механизм bootstrap ключа
- флаг доступности SSH shell и Entware/OPKG

Хранение параметров:

- `.env` как source of truth для router connection metadata
- при необходимости отдельный шаблон SSH config рядом с `home/ssh/vps.conf.template`, но для роутера
- никакие router credentials не должны жить в repo-tracked файлах

### 3. Keenetic-specific adapter

Вместо “общего router install” сразу проектировать `KeeneticAdapter` как первый backend adapter.

Зона ответственности adapter:

- проверка SSH-доступа
- проверка модели Keenetic/совместимости
- проверка наличия Entware/OPKG
- установка `nfqws-keenetic`
- запись managed config
- применение/рестарт пакета
- чтение текущего статуса
- безопасное выключение policy без удаления пакета

Важно:

- adapter не должен пытаться полностью управлять KeeneticOS
- он управляет только собственным managed footprint:
  - Entware package
  - managed config files пакета
  - managed списки устройств/подсетей
  - managed enable/disable hooks

### 4. Ownership model для конфигурации

`home-server` должен быть source of truth для `router-zapret` config.

Нужно ввести managed artifacts на стороне `home-server`:

- rendered router package config
- rendered policy lists for allowlist/full LAN
- router host metadata
- last applied checksum/version

На Keenetic нужно писать только файлы, которыми владеет система:

- основной managed config пакета
- generated allowlist/auto.list/user.list при необходимости
- marker/version file для drift detection

Правило:

- ручные правки на Keenetic либо запрещены, либо считаются drift и перезаписываются очередным apply

## Implementation Plan

### 1. Installer and configuration model

Расширить `install/setup` flow для `mode=B` новыми параметрами роутера:

- `ROUTER_TYPE=keenetic`
- `ROUTER_LAN_IP`
- `ROUTER_SSH_PORT`
- `ROUTER_SSH_USER`
- `ROUTER_ZAPRET_BACKEND=keenetic`
- `ROUTER_ZAPRET_DEFAULT_POLICY=disabled`

В `setup.sh` и `install-home.sh` добавить фазу:

- discover router access
- validate SSH
- detect Keenetic / Entware / OPKG
- install router integration components on home-server
- при явном флаге enable выполнить package install на Keenetic
- по умолчанию завершать установкой backend без включения traffic policy

Bootstrap path:

- если SSH ключ уже работает, использовать его
- если нет, installer должен уметь один раз скопировать ключ при наличии пароля
- если bootstrap невозможен, behind-router mode считается incomplete и должен быть виден в health

### 2. Home-server router orchestration module

Добавить на `home-server` новый orchestration слой, по аналогии с VPS operations:

- SSH command wrapper
- file push/pull
- status/readback helpers
- dry inspection helpers
- apply transaction with rollback-to-disabled

Логически это отдельный subsystem, не размазанный по bot handler’ам.

Минимальные операции:

- `router_detect`
- `router_install_package`
- `router_render_config`
- `router_apply_config`
- `router_enable_policy`
- `router_disable_policy`
- `router_collect_status`
- `router_uninstall` только как явная админская операция

### 3. Watchdog integration

`watchdog` становится owner desired state для `router-zapret`.

Новые обязанности:

- читать router backend config из `.env` и/или state
- держать health checks для router backend
- различать local zapret и router-zapret
- при `mode=B + router backend installed` не считать локальный `nfqws` обязательным для YouTube path
- expose status в API и `state.json`

Новые health scenarios:

- router SSH reachable
- router package installed
- managed config checksum matches
- policy mode matches desired state
- router backend enabled/disabled as expected
- router backend drift detected
- router backend degraded but system overall still operational

### 4. Bot/operator surface

Управление идёт через `watchdog + Telegram-бот`, без отдельного UI.

Нужно добавить в admin surface:

- статус router backend
- install/apply/recheck команды
- enable/disable policy
- выбрать policy mode:
  - `disabled`
  - `device_allowlist`
  - `full_lan`
- показать managed targets в allowlist

На первом этапе достаточно admin-only операций; client-facing flows не нужны.

### 5. Policy model on Keenetic

Зафиксировать три режима policy на роутере:

- `disabled`
  - пакет установлен, но не влияет на трафик
- `device_allowlist`
  - `router-zapret` применяется только к явно выбранным устройствам/сетям
- `full_lan`
  - применяется ко всему LAN

Default после install: `disabled`.

В первой версии allowlist должен поддерживать как минимум:

- IPv4 адреса устройств
- возможно подсети
- без обязательной MAC-based логики в v1, если это усложняет implementation

### 6. Rollout sequence

1. Infrastructure only:
   - env/config model
   - SSH orchestration
   - Keenetic detection
   - package install/apply
   - status/health
2. Operator controls:
   - watchdog endpoints/state
   - bot commands/status
3. Policy enablement:
   - `disabled`
   - `device_allowlist`
   - `full_lan`
4. Documentation + recovery flows

## Tests and Acceptance

### Automated checks

- shell syntax checks для `setup.sh`, `install-home.sh`, router helper scripts
- unit tests на config rendering и state transitions
- tests на idempotent re-apply
- tests на drift detection
- tests на mode split
- router unreachable:
  - system degrades gracefully
  - VPS path and home-server continue operating
- router package already installed manually:
  - installer detects and adopts only if compatible, иначе сообщает incompatible state
- repeated deploy/recovery:
  - router backend state is preserved and re-applied from home-server

### Acceptance criteria

- `home-server` может автоматически установить и настроить `nfqws-keenetic` по SSH
- после установки `router-zapret` не включается автоматически на трафик
- оператор может включить/выключить `router-zapret` из bot/watchdog
- состояние роутера видно в health/status
- behind-router architecture формально описывает Keenetic как execution backend, а не как отдельный control plane

## Assumptions and Defaults

- Первый поддерживаемый роутер: только Keenetic
- Основной канал управления: только SSH
- Основная operator surface: watchdog + Telegram-бот
- Default rollout mode: disabled until enabled
- Source of truth: home-server
- Keenetic не рассматривается как место для хранения бизнес-логики системы; только managed execution target
- v1 не требует поддержки нескольких vendor adapters; abstraction проектируется сразу, но реализуется только `KeeneticAdapter`

-----

## IMPLEMENTED — Multi-admin

Раздел сохранён как запись принятого решения. Активной задачей не является.

**Модель прав:**
- **Root admin** = `TELEGRAM_ADMIN_CHAT_ID` из `.env`. Хранится только в `.env`, не в БД. Неудаляем.
- **Дополнительные admins** = `is_admin=1` в таблице `clients`. Те же права, кроме `/admin add|remove` (только root).

**`_is_admin()` в handlers/admin.py:**
```python
def _is_admin(uid: int) -> bool:
    return str(uid) == str(config.admin_chat_id) or db.is_admin(uid)

def _is_root(uid: int) -> bool:
    return str(uid) == str(config.admin_chat_id)
```
Все 59 мест с проверкой прав используют `_is_admin()`. Управление adminами — `_is_root()`.

**Миграция БД:** `admin_added_by TEXT` в clients, `grants_admin INTEGER DEFAULT 0` в invite_codes.

**Команды:**
```
/admin list                  — все админы
/admin invite                — admin-invite (только root)
/admin remove <username>     — снять права (только root)
```

**Watchdog:** `state.admin_chat_ids` — список всех для алертов. `POST /admin-notify`, `POST /admin-notify/reload`.

-----

## PARKED — Экспорт/импорт данных для чистой переустановки

Часть recovery/export уже закрыта текущими `backup.sh` / `restore.sh`.
Полный zero-loss reinstall через `setup.sh --from-export` пока не в активной разработке.

**Приоритет: средний**

### Анализ существующего механизма

`backup.sh` + `restore.sh` уже реализуют большую часть нужного функционала. Это **расширение**, а не новая система.

**Что backup.sh уже сохраняет:**
- `/etc/wireguard/` — WireGuard/AWG ключи и серверные конфиги (включая пиры)
- `/opt/vpn/.env` — все секреты (UUID, токены, пароли, REALITY ключи, Hysteria2)
- `vpn_bot.db` — SQLite БД (clients, devices, invite_codes, excludes, domain_requests)
- nftables конфиги, hysteria config, xray конфиги, dnsmasq конфиги
- `/etc/vpn-routes/manual-*.txt` — ручные маршруты
- watchdog plugins

**Что НЕ сохраняется (gap):**
| Данные | Где живут | Критичность |
|--------|-----------|-------------|
| mTLS CA ключ + сертификат | VPS `/opt/vpn/nginx/mtls/ca.key`, `ca.crt` | Высокая — без CA нельзя выпустить новый клиентский cert |
| watchdog `state.json` | `/opt/vpn/watchdog/state.json` | Средняя — baseline RTT, активный стек |
| DPI presets | `/etc/vpn/dpi-presets.json` | Низкая — Thompson Sampling состояние |
| Cloudflared tunnel cert | `~/.cloudflared/*.json` | Средняя — нужен если используется CDN стек |

**setup.sh:** нет флага `--from-export` — при чистой установке шаг `step07_generate_secrets` всегда генерирует новые ключи.

---

### Архитектурное решение

Три расширения существующих скриптов + один новый эндпоинт:

1. `backup.sh --full-export` — расширенный бэкап с mTLS и state
2. `restore.sh` — обработка новых данных + IP-детекция
3. `setup.sh --from-export <file>` — пропуск генерации ключей
4. watchdog `POST /backup/export` + бот `/backup export`

---

### Шаг 1 — backup.sh: флаг --full-export

**Файл:** `home/scripts/backup.sh` | **Сложность:** Low

**Изменения:**
```bash
# Новый флаг
--full-export   # включает mTLS (SSH к VPS), DPI presets; имя файла vpn-export-*

# Дополнения к стандартному составу (всегда, без флага):
/opt/vpn/watchdog/state.json          → watchdog-state.json

# Только при --full-export:
VPS:/opt/vpn/nginx/mtls/ca.key        → mtls/ca.key       (scp через SSH_KEY)
VPS:/opt/vpn/nginx/mtls/ca.crt        → mtls/ca.crt
/etc/vpn/dpi-presets.json             → dpi-presets.json  (если существует)
~/.cloudflared/                       → cloudflared/       (если существует)

# Обновление metadata.json:
{
  "export_type":    "full-export" | "backup",
  "vpn_version":    "...",
  "client_count":   N,           # из SQLite: SELECT COUNT(*) FROM clients
  "has_mtls":       true/false,
  "home_server_ip": "...",       # EXTERNAL_IP из .env — для IP-детекции при импорте
}
```

**Graceful degradation:** если VPS недоступен по SSH → создать экспорт без mTLS + предупреждение в stdout и Telegram. Не fatal.

**Имя файла:** `vpn-export-${TIMESTAMP}.tar.gz.gpg` (для `--full-export`), `vpn-backup-*` остаётся для обычного бэкапа.

---

### Шаг 2 — restore.sh: новые компоненты + IP-детекция

**Файл:** `restore.sh` | **Сложность:** Medium

**Добавить в `restore_configs()`:**
```bash
# 1. watchdog state
if [[ -f "$src/watchdog-state.json" ]]; then
    cp "$src/watchdog-state.json" /opt/vpn/watchdog/state.json
fi

# 2. mTLS CA — восстановить на VPS через SSH
if [[ -d "$src/mtls" && -f "$src/mtls/ca.key" ]]; then
    # VPS_IP берётся из .env (уже восстановлен)
    ssh -i $SSH_KEY sysadmin@$VPS_IP "mkdir -p /opt/vpn/nginx/mtls"
    scp -i $SSH_KEY "$src/mtls/ca.key" "$src/mtls/ca.crt" \
        "sysadmin@${VPS_IP}:/opt/vpn/nginx/mtls/"
    ssh -i $SSH_KEY sysadmin@$VPS_IP "chmod 600 /opt/vpn/nginx/mtls/ca.key && \
        docker restart nginx 2>/dev/null || true"
fi

# 3. DPI presets
if [[ -f "$src/dpi-presets.json" ]]; then
    mkdir -p /etc/vpn
    cp "$src/dpi-presets.json" /etc/vpn/dpi-presets.json
fi

# 4. cloudflared tunnel credentials
if [[ -d "$src/cloudflared" ]]; then
    mkdir -p ~/.cloudflared
    cp -r "$src/cloudflared/." ~/.cloudflared/
fi
```

**Добавить после `restart_services()`:**
```bash
# IP-детекция: если IP сервера изменился — разослать обновлённые конфиги
detect_ip_change() {
    local meta="$RESTORE_TMP/metadata.json"
    [[ -f "$meta" ]] || return 0

    local backup_ip current_ip
    backup_ip=$(python3 -c "import json; \
        d=json.load(open('$meta')); print(d.get('home_server_ip',''))" 2>/dev/null || echo "")
    current_ip=$(curl -sf --max-time 5 https://icanhazip.com 2>/dev/null || echo "")

    if [[ -n "$backup_ip" && -n "$current_ip" && "$backup_ip" != "$current_ip" ]]; then
        log_warn "IP изменился: $backup_ip → $current_ip"
        # Обновить EXTERNAL_IP в .env
        sed -i "s/^EXTERNAL_IP=.*/EXTERNAL_IP=${current_ip}/" "$ENV_FILE"
        # После запуска watchdog — запустить рассылку конфигов
        TRIGGER_NOTIFY_CLIENTS=true
    fi
}
```

**Новый флаг `--check-export <file>`:** pre-flight валидация без применения — проверить наличие обязательных файлов (.env, wireguard/, vpn_bot.db), вывести состав и предупреждения.

---

### Шаг 3 — setup.sh: флаг --from-export

**Файл:** `setup.sh` | **Сложность:** Medium-High

**Парсинг в начале main:**
```bash
IMPORT_MODE=false
IMPORT_FILE=""
for arg in "$@"; do
    case "$arg" in
        --from-export) IMPORT_MODE=true; shift; IMPORT_FILE="${1:-}"; shift ;;
    esac
done
```

**Перед фазой 0 (если IMPORT_MODE):**
```bash
# Расшифровать и распаковать экспорт
IMPORT_DIR="$(mktemp -d)"
# Decrypt + extract → $IMPORT_DIR
# Загрузить .env из экспорта → /opt/vpn/.env
# Сохранить путь к распакованному архиву
```

**В step05 (collect_inputs) — если IMPORT_MODE:**
- Пропустить интерактивный ввод VPS IP, Telegram токена и т.д. — всё берётся из `.env`
- Показать загруженные значения пользователю для подтверждения

**В step07 (generate_secrets) — если IMPORT_MODE:**
```bash
if $IMPORT_MODE; then
    step_skip "step07_generate_secrets"  # ключи уже в .env из экспорта
    step_done "step07_generate_secrets"
    return 0
fi
```

WireGuard конфиги из экспорта копируются в `/etc/wireguard/` на шаге установки AmneziaWG (step12) — аналогично через `if $IMPORT_MODE → cp из $IMPORT_DIR/wireguard/`.

**После завершения всех фаз:**
```bash
if $IMPORT_MODE; then
    log_info "Восстановление данных из экспорта..."
    # Восстановить SQLite, mTLS, routes, watchdog state, DPI presets
    bash restore.sh --restore-data-only "$IMPORT_FILE"
    # IP-детекция и рассылка конфигов при необходимости
fi
```

**Совместимость версий:** сравнить `vpn_version` из metadata экспорта с текущей. Если отличается → запустить `migrations/apply.sh` автоматически.

**Итоговый UX:**
```bash
# Переустановка на новый сервер с сохранением всех пользователей:
sudo bash setup.sh --from-export vpn-export-20260324_120000.tar.gz.gpg
```

---

### Шаг 4 — Telegram-бот + watchdog: /backup export

**Файлы:** `home/telegram-bot/handlers/admin.py`, `home/watchdog/watchdog.py` | **Сложность:** Low

**Watchdog — новый эндпоинт:**
```
POST /backup/export      → запускает backup.sh --full-export в фоне
                           по завершении отправляет файл через TelegramQueue
```

**Бот — новая подкоманда:**
```
/backup                  — обычный бэкап (существующий)
/backup export           — полный экспорт: WG-ключи + mTLS + state + DPI
                           "Создаётся полный экспорт (~30–60 сек)..."
                           → файл vpn-export-*.tar.gz.gpg в чат
```

---

### Зависимости между шагами

```
Шаг 1 (backup.sh)  →  независим, делать первым
Шаг 2 (restore.sh) →  после Шага 1 (понимает новую структуру архива)
Шаг 3 (setup.sh)   →  после Шагов 1 + 2
Шаг 4 (бот)        →  после Шага 1 (вызывает --full-export)
```

Шаги 1 и 4 можно сделать параллельно. Шаги 2 и 3 — после Шага 1.

---

### Риски и митигации

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| VPS недоступен при экспорте → нет mTLS CA | Средняя | Graceful degradation: экспорт без mTLS + предупреждение. При импорте → автоматически перевыпустить CA через `/renew-ca` |
| setup.sh идемпотентность с IMPORT_MODE | Средняя | При `--from-export` стереть шаги step05/step07 из `.setup-state` перед запуском |
| Cloudflared tunnel token ≠ credentials файл | Низкая | Проверить `~/.cloudflared/` при экспорте; если нет → только token из .env достаточен для пересоздания |
| Версионная несовместимость схемы БД | Низкая | `migrations/apply.sh` уже существует, запускать автоматически при IMPORT_MODE |
| Файл экспорта > 50 МБ (Telegram лимит) | Низкая | Отправить только ссылку для скачивания с VPS; в Telegram — только уведомление |

### Что НЕ меняется
- Обычный бэкап (`backup.sh` без флага) — без изменений, не ломаем существующий cron
- `home/scripts/restore.sh` — только алиас на корневой, остаётся
- Формат GPG-шифрования — тот же AES256
- Ротация 30 дней — та же политика

-----

## ARCHIVE — WiFi как резервный канал (опционально)

Идея сохранена, но в текущий backlog не входит.

**Приоритет: Low**

При установке на Mac mini (или любой сервер с WiFi):
- Установить Broadcom драйвер (bcmwl-kernel-source) если нужно
- Спросить SSID + пароль
- Netplan: Ethernet metric 100 (основной), WiFi metric 600 (резервный)
- При падении Ethernet — автоматический failover на WiFi
- Watchdog алерт: "Ethernet недоступен, работаю через WiFi (деградация)"
- При восстановлении Ethernet — автоматический возврат

Также: автозапуск при подаче питания (Mac: nvram AutoBoot=%03, PC: предупреждение о BIOS).

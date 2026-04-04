# Команды Telegram-бота

Документ описывает только текущий поддерживаемый bot surface.
Если команда существует в коде, но не доведена до production-contract, это отмечено отдельно.

## Роли

- Администратор: `TELEGRAM_ADMIN_CHAT_ID` и дополнительные admins из БД.
- Клиент: зарегистрированный пользователь.
- Незарегистрированный пользователь: только `/start`.

## Команды администратора

### Статус и здоровье

- `/status` — общий статус системы, включая deploy-state.
- `/health` — сводка health checks.
- `/functional` — functional health и сценарии.
- `/tunnel` — состояние туннелей и активного стека.
- `/ip` — direct IP и VPN/VPS egress IP.
- `/docker` — статус контейнеров.
- `/speed` — быстрый throughput snapshot.

### Логи и графики

- `/logs <service> [N]` — логи сервиса.
- `/graph [panel] [period]` — график через Grafana render.

Поддерживаемые `panel`: `system`, `tunnel`, `speed`, `clients`.
Типовые `period`: `1h`, `6h`, `24h`, `7d`.

### Операции со стэками и сервисами

- `/assess` — тест доступных стэков.
- `/switch <stack>` — переключить активный стек.

Поддерживаемые `stack`: `cloudflare-cdn`, `vless-reality-vision`, `hysteria2`, `reality-xhttp`, `tuic`, `trojan`.

`reality-xhttp`, `tuic`, `trojan` сейчас считаются experimental/manual stacks: они доступны для ручных тестов и ручного `/switch`, но не участвуют в automatic standby/reassessment path.
- `/restart <service>` — перезапустить сервис или контейнер.
- `/upgrade` — обновить Docker-образы.
- `/reboot` — перезагрузить home-server.

### Deploy и rollback

- `/deploy` — запустить release deploy.
- `/rollback` — откатить к последнему подтверждённому snapshot.

Прогресс и итог deploy/rollback нужно смотреть через `/status`, а не по тексту запуска.

### Клиенты и рассылки

- `/invite` — создать invite.
- `/clients` — список клиентов.
- `/client disable <name>`
- `/client enable <name>`
- `/client kick <name>`
- `/client limit <name> <n>`
- `/broadcast <text>` — сообщение всем клиентам.
- `/requests` — входящие запросы клиентов.
- `/admin list`
- `/admin invite`
- `/admin remove <username|id>`

### Routing policy

- `/vpn add <domain>`
- `/vpn remove <domain>`
- `/direct add <domain>`
- `/direct remove <domain>`
- `/list vpn`
- `/list direct`
- `/check <domain> [chat_id]`
- `/backend_pref add <chat_id> <service|domain|cidr> <value> <backend-id>`
- `/backend_pref list [chat_id]`
- `/backend_pref remove <id>`
- `/latency learned|candidates|all`
- `/routes update`

`/check` показывает verdict, source tags, service attribution, current Decision Maker explanation и active `hysteria2` backend path state (`desired/applied/rendered/verified`) для effective backend.
Поддерживаемые формы:
- `/check <domain>`
- `/check <domain> <chat_id>`
- `/check <domain> <source_ip>` в `gateway mode`
- `/check <domain> <chat_id> <source_ip>` для явной диагностики

Если передан `chat_id`, Decision Maker дополнительно учитывает `vpn_client` backend preferences для этого клиента.
Если передан `source_ip` в `gateway mode`, Decision Maker может резолвить `lan_client` identity и учитывать `LAN backend preferences`:
- `identity_type`
- `identity_id`
- `route_class`
- `decision_source`
- `effective_backend_id`
- `matched_preference`
- `preference_status`
- `preference_reason`
- `fallback_reason`
`/latency all` показывает runtime catalog status, `learned` и `candidates`.

### VPS и recovery-операции

- `/backends`
- `/backend add <ip> [ssh_port]`
- `/backend remove <ip>`
- `/backend drain <backend-id>`
- `/backend undrain <backend-id>`
- `/balancer`
- `/rebalance [route_class|all]`
- `/decision status` via menu/API canonical read path
- `/decision backend-paths` via diagnostics/API canonical dataplane foundation path
- `/decision assignments` via diagnostics/API canonical read path
- `/decision choose-backend` via API canonical choose path
- `/decision apply-backend` via API canonical apply path
  При `hysteria2` current slice apply теперь проходит через runtime verify и при failed probe откатывается на предыдущий backend. Diagnostics показывают `desired vs applied` и `verify reason`.
- `/decision reassign [route_class|all]` via menu/API canonical decision path
- `/decision reconcile-assignments [backend-id]` via API canonical reconciliation path
- `/balancer switch <backend-id>` via menu/API apply path
- `/balancer auto-select` via menu/API apply path
- `/vps list`
- `/vps add <ip> [ssh_port]`
- `/vps remove <ip>`

### Gateway-only

Эти команды применимы только при `SERVER_MODE=gateway`.

- `/lan_clients`
- `/lan_client add <name> <src_ip>`
- `/lan_client remove <id>`
- `/lan_backend_pref add <lan_client_id> <service|domain|cidr> <value> <backend-id>`
- `/lan_backend_pref list`
- `/lan_backend_pref remove <id>`
- `/migrate_vps <ip> [--from-backup]`
- `/migrate-vps <ip> [--from-backup]`
- `/renew_cert`
- `/renew-cert`
- `/renew_ca`
- `/renew-ca`
- `/diagnose <device>`

### Experimental / partial commands

- `/dpi` — experimental DPI bypass surface.
- `/rotate_keys` и `/rotate-keys` — зарезервированы, но пока не реализованы как безопасный production path.

## Команды клиента

- `/start` — регистрация по invite-коду или вход в меню.
- `/mydevices` — список устройств.
- `/myconfig` — получить конфиг.
- `/adddevice` — добавить устройство.
- `/removedevice` — удалить устройство.
- `/update` — обновить конфиги устройств.
- `/request` — запросить policy change по домену.
- `/myrequests` — статус собственных запросов.
- `/exclude` — исключения из split tunneling.
- `/route` — принудительные маршруты через сервер.
- `/report` — отправить сообщение администратору.
- `/status` — клиентский статус VPN.
- `/help` — краткая помощь.
- `/menu` — показать меню.

## Поддерживаемые смысловые сценарии

### Deploy

- `/deploy`
- затем `/status`

### Rollback

- `/rollback`
- затем `/status`

### Routing

- `/check example.com`
- `/check example.com 123456789`
- `/backend_pref add 123456789 service openai backend-us-1`
- `/backend_pref list 123456789`
- `/vpn add example.com`
- `/direct add example.com`
- `/routes update`

### Maintenance после deploy

Через SSH:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
sudo python3 /opt/vpn/scripts/update-routes.py --force
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

Ожидаемый результат:

- post-install check без критических ошибок;
- route rebuild без traceback;
- smoke полностью зелёный;
- `Pending: none`;
- `Last attempt: success / commit`.

## Чего здесь намеренно нет

Из документа убраны:

- старые обещания про команды, которых больше нет в production-contract;
- описание alert-матрицы как гарантированного API;
- старые версии deploy UX, где успех определялся по тексту запуска;
- `/vps ...` сейчас legacy alias на backend pool; отдельный `vps-only` update path не является текущим контрактом.
- `/backends` и `/balancer` остаются bot-facing surface, но canonical read/reassign API теперь идёт через `/decision/*`.

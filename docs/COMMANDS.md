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
- `/check <domain>`
- `/latency learned|candidates|all`
- `/routes update`

`/check` показывает verdict, source tags и service attribution, если домен сопоставлен с latency catalog.
`/latency all` показывает runtime catalog status, `learned` и `candidates`.

### VPS и recovery-операции

- `/vps list`
- `/vps add <ip> [ssh_port]`
- `/vps remove <ip>`
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
- неактуальные команды для отдельного `vps-only` update path.

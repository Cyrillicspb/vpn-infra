# Обновление и recovery

## Поддерживаемые пути

### Основной путь

- бот: `/deploy`
- или SSH: `sudo bash /opt/vpn/deploy.sh`

### Проверка без применения

```bash
sudo bash /opt/vpn/deploy.sh --check
```

### Статус

```bash
sudo bash /opt/vpn/deploy.sh --status
```

### Rollback release

- бот: `/rollback`
- или SSH: `sudo bash /opt/vpn/deploy.sh --rollback`

### Disaster recovery

```bash
sudo bash /opt/vpn/restore.sh --full-restore <backup.tar.gz.gpg>
```

## Deploy contract

`deploy.sh` обновляет home и VPS как один release:

1. preflight;
2. fetch target release;
3. snapshot текущего release;
4. apply на home;
5. apply на VPS;
6. verify через health gate;
7. commit или rollback.

Health gate опирается на:

- watchdog/API health;
- smoke suite;
- parity между home и VPS deploy-state.

Режим deploy: `fail fast strict`.

## Source of truth и mirror semantics

- latest release tag из `origin` является единственным источником target release.
- `vps-mirror` больше не используется как source-of-truth для выбора release.
- `vps-mirror` используется только как parity gate для backend rollout.
- Термин `mirror parity` используется как operator-facing статус синхронизации `vps-mirror` относительно `origin`.
- Перед strict parity gate `deploy.sh` пытается автоматически догнать `vps-mirror` до exact target release tag и default branch из `origin`.
- Если `origin` недоступен, `vps-mirror` недоступен, `vps-mirror` stale относительно `origin`, либо у mirror нет matching release tag, deploy останавливается на preflight без apply.
- Для `origin=https://github.com/...` `deploy.sh` может использовать active SOCKS fallback (`/var/run/vpn-active-socks-port`) для `git fetch/ls-remote`, если direct DNS/HTTPS до GitHub с home-host недоступны.
- `sudo bash /opt/vpn/deploy.sh --status` и `--check` должны явно показывать:
  - `Target source`
  - `Origin sha`
  - `Mirror sha`
  - `Mirror parity`
  - `Repo head`

## GitHub Release contract

- merge в `master` не должен сам по себе создавать новый release commit в `master`;
- release создаётся отдельным tag-first workflow по выбранному `ref`;
- authoritative version для release берётся из git tag, а не из autobump-коммита в ветке;
- installer и release assets должны собираться из exact tagged state;
- `Rebuild Release Assets` допускается только для уже существующего tag и должен валидировать tag/commit metadata release manifest перед перезаливкой assets.

## Preflight blockers

Deploy обязан остановиться до snapshot/apply при любом blocker:

- dirty tracked tree;
- отсутствует `tests/run-smoke-tests.sh`;
- backend inventory пуст;
- `.env` отсутствует или unreadable;
- отсутствует toolchain: `git`, `rsync`, `sqlite3`, `docker`, `python3`, `curl`, `docker compose`;
- `origin` не fetch'ится или не содержит release tags;
- `vps-mirror` stale/unreachable/not-configured/missing-ref;
- `current.json` не парсится;
- primary backend недоступен по SSH.

## Источник истины для результата

Результат deploy/rollback определяется по:

- `/opt/vpn/.deploy-state/current.json`
- `/opt/vpn/.deploy-state/pending.json`
- `/opt/vpn/.deploy-state/last-attempt.json`

Не по тексту логов и не по факту старта команды.

State contract описан в [docs/DEPLOY-STATE.md](/home/kirill/vpn-infra/docs/DEPLOY-STATE.md).

## Rollback и restore различаются

### Rollback

Используется для проблемы в последнем release:

```bash
sudo bash /opt/vpn/deploy.sh --rollback
```

Rollback возвращает систему к последнему подтверждённому deploy snapshot.

### Restore

Используется для DR, миграции или полного восстановления из backup:

```bash
sudo bash /opt/vpn/restore.sh --full-restore <backup>
```

Это не тот же самый механизм.

## Docker image update

Обновление Docker-образов не равно deploy кода.

- бот: `/upgrade`
- или ручной `docker compose pull/up` там, где это действительно нужно

Использовать `/deploy` для изменения кода и generated configs.

Во время `/deploy` сервисы разделены на два класса:

- `build-local`:
  - `telegram-bot`
  - такие сервисы не участвуют в общем `docker compose pull`; вместо этого для них выполняется локальная сборка и post-apply verify
- `pull-remote`:
  - registry-backed сервисы, которые входят в allowlist `docker compose pull`

Это убирает класс ошибок вида `docker.io/library/vpn-telegram-bot:latest not found` при нормальном deploy.

## Route data refresh

Если нужен route rebuild без deploy:

```bash
sudo python3 /opt/vpn/scripts/update-routes.py --force
```

Если нужно отдельно пересобрать latency catalog:

```bash
sudo python3 /opt/vpn/scripts/update-latency-catalog.py
sudo python3 /opt/vpn/scripts/update-routes.py
sudo systemctl restart watchdog
```

Runtime catalog теперь также обновляется по `systemd timer`.
Если watchdog считает runtime catalog пустым или устаревшим, это попадает в health/alerts и видно через bot surface `/latency all`.

## Maintenance после обновления

После крупных infra-изменений прогонять:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
sudo python3 /opt/vpn/scripts/update-routes.py --force
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

Ожидаемый committed baseline:

- post-install без критических ошибок;
- smoke полностью зелёный;
- `Pending: none`;
- `Last attempt: success / commit`.

## Что больше не является update contract

Из документа убраны старые paths:

- отдельный `vps-only` deploy;
- смешивание release rollback и backup restore;
- утверждение, что Docker image update автоматически обновляет код проекта;
- описание success/failure deploy только через Telegram-тексты.

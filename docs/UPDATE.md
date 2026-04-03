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

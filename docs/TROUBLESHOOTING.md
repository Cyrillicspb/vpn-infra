# Troubleshooting

## Базовая диагностика

Начинать почти всегда нужно с этого:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh --verbose
sudo bash /opt/vpn/deploy.sh --status
```

Через бота:

- `/status`
- `/health`
- `/functional`
- `/tunnel`
- `/check <domain>`

## Deploy завис или сломался

### Проверить state

```bash
sudo bash /opt/vpn/deploy.sh --status
ls -la /opt/vpn/.deploy-state
```

Смотреть нужно на:

- `current.json`
- `pending.json`
- `last-attempt.json`

### Если release не committed

```bash
sudo bash /opt/vpn/deploy.sh --rollback
```

Если сломан именно release path, rollback предпочтительнее, чем restore из backup.

## Заблокированный сайт не работает

1. Проверить policy:

```bash
/check example.com
```

2. Убедиться, что устройство не использует внешний DNS / DoH.
3. Проверить, не попал ли домен в `manual-direct`.
4. Проверить, не тестируется ли сторонний CDN/bootstrap hostname вместо самого сервиса.
5. Если проблема в route data, выполнить:

```bash
sudo python3 /opt/vpn/scripts/update-routes.py --force
```

## Сервис открывается с “не той” геолокацией

Причина обычно одна из двух:

- direct egress и VPS egress находятся в разных странах;
- домен сервиса или его служебный CDN ушёл не в тот lane.

Проверять через `/check` нужно и основной домен, и служебные hostnames из waterfall.

## Бот не отвечает

Проверить:

```bash
sudo docker ps
sudo docker logs telegram-bot --tail 100
systemctl status watchdog
```

Если watchdog недоступен, bot чаще всего тоже будет частично неработоспособен.

## Smoke зелёный, но клиент всё равно жалуется

Чаще всего проблема на стороне клиента:

- старый конфиг;
- не тот tunnel DNS;
- включён Secure DNS / Private DNS / DoH;
- локальная сеть клиента пересекается с policy routes.

Нужно:

- переслать свежий конфиг;
- перепроверить client-side DNS settings;
- использовать `/diagnose <device>`.

## Нужен полный recovery из backup

```bash
sudo bash /opt/vpn/restore.sh --full-restore <backup.tar.gz.gpg>
```

Это DR path. Не использовать его вместо обычного release rollback, если сломан только последний deploy.

## Нужно очистить VPS и поднять заново

Используйте `dev/reset-vps.sh`, затем повторите установку VPS path.

Типовой запуск:

```bash
ssh sysadmin@<VPS_IP> "sudo bash /opt/vpn/dev/reset-vps.sh"
```

После этого:

```bash
cd /opt/vpn
sudo bash install-vps.sh
```

## Проблема после ручной правки на сервере

Если правился generated runtime-файл, сначала нужно решить, откуда он генерируется, и чинить source, а не runtime copy.

Иначе проблема вернётся на следующем:

- `setup.sh`;
- `deploy.sh`;
- `update-routes.py`;
- `restore.sh`.

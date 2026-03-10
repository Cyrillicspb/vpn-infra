# Восстановление после сбоя

## Сценарий 1: Домашний сервер вышел из строя

1. Установите Ubuntu 24.04 на новый сервер
2. Скачайте последний бэкап (из Telegram или VPS)
3. Запустите:
   ```bash
   curl -fsSL .../setup.sh | bash
   # Выберите "восстановление из бэкапа"
   bash restore.sh backup.tar.gz.gpg
   ```
4. Перенастройте port forwarding на роутере

## Сценарий 2: VPS вышел из строя

1. Арендуйте новый VPS
2. Запустите:
   ```
   /migrate-vps <новый_IP>
   ```
   или:
   ```
   /migrate-vps <новый_IP> --from-backup
   ```
3. Watchdog автоматически переключится на новый VPS

## Сценарий 3: Watchdog мёртв

Cron failsafe отправит алерт в Telegram каждые 5 минут.

Восстановление вручную:
```bash
systemctl restart watchdog
journalctl -u watchdog -n 50  # Диагностика
```

## Сценарий 4: nftables сбросились (перезагрузка)

vpn-sets-restore.service автоматически восстанавливает blocked_static.
Если не сработало:
```bash
nft -f /etc/nftables.conf
nft -f /etc/nftables-blocked-static.conf
```

## Сценарий 5: dnsmasq не запускается

```bash
# Порт 53 занят
ss -lpn 'sport = :53'
systemctl disable --now systemd-resolved
systemctl start dnsmasq
```

## Аварийный доступ без VPN

Если Telegram недоступен и VPN не работает:
```bash
ssh sysadmin@<домашний_IP>
systemctl status watchdog dnsmasq
/opt/vpn/watchdog/venv/bin/python -c "import requests; print(requests.get('http://localhost:8080/status').json())"
```

## Бэкап и ротация

| Хранилище | Ротация |
|-----------|---------|
| VPS | 30 дней |
| Telegram | Вечно (в истории чата) |
| Локально | Нет (очищаются при очистке диска) |

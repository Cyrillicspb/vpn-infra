# Устранение неполадок

## VPN не работает после установки

1. Проверьте port forwarding на роутере:
   - UDP 51820 и 51821 должны вести на IP домашнего сервера

2. Проверьте белый IP:
   ```bash
   curl https://api.ipify.org
   ```
   Должен совпадать с IP в конфиге клиента.

3. Проверьте статус сервисов:
   ```bash
   systemctl status watchdog dnsmasq wg-quick@wg0
   ```

4. Проверьте nftables:
   ```bash
   nft list ruleset
   ```

## Заблокированные сайты не открываются

1. Проверьте туннель (ping VPS через tun):
   ```bash
   ping 10.177.2.2
   ```

2. Проверьте nft set:
   ```bash
   nft list set inet filter blocked_static | head -20
   ```

3. Проверьте dnsmasq:
   ```bash
   dig @127.0.0.1 youtube.com
   ```

4. Запустите `/status` в боте — должен показать активный стек.

## Watchdog не запускается

```bash
journalctl -u watchdog -n 50
```

Частые причины:
- Python venv не создан: `python3 -m venv /opt/vpn/watchdog/venv && /opt/vpn/watchdog/venv/bin/pip install -r requirements.txt`
- Нет .env файла: `cp /opt/vpn/.env.example /opt/vpn/.env` и заполните

## dnsmasq не запускается

Порт 53 занят systemd-resolved:
```bash
systemctl disable --now systemd-resolved
systemctl start dnsmasq
```

## AmneziaWG DKMS не собирается

```bash
dkms status
# Если нет amneziawg:
apt-get install -y amneziawg-dkms
dkms build amneziawg/<version>
```

## Docker контейнеры не запускаются

```bash
docker compose logs telegram-bot
docker compose logs xray-client
```

## Алерты в Telegram не приходят

Проверьте BOT_TOKEN и ADMIN_CHAT_ID в .env:
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```

## Высокий ping через VPN

- Hysteria2 → самый быстрый, но блокируется первым
- Watchdog автоматически выберет лучший стек
- `/switch hysteria2` — переключить вручную

## Потеря пакетов через VPN

1. Проверьте upload канала: `/speed` в боте
2. Проверьте загрузку сервера: `/status`
3. Уменьшите MTU: WG_MTU=1280 в .env

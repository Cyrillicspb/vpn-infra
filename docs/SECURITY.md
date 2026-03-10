# Безопасность

## Модель угроз

Проект защищает от:
- **DPI-фильтрации (ТСПУ/РКН):** шифрование + маскировка трафика
- **Утечки трафика при падении VPN:** kill switch
- **Утечки DNS:** split DNS, dnsmasq не использует публичный DNS для заблокированных доменов
- **Несанкционированного доступа к серверу:** SSH только по ключам, fail2ban, mTLS
- **Компрометации одного компонента:** изоляция Docker, секреты в env-файлах

Проект **не защищает** от:
- Целевой слежки государственными органами
- Компрометации физического оборудования
- Утечки приватного ключа WireGuard клиента

---

## Аутентификация и доступ

### SSH (домашний сервер и VPS)

- Root login **отключён** (`PermitRootLogin no`)
- Аутентификация только по ключам (`PasswordAuthentication no`)
- Пользователь `sysadmin` с `sudo NOPASSWD` для сервисных операций
- SSH-ключ для автоматического VPS-доступа: `/root/.ssh/vpn_id_ed25519`

### mTLS (панели управления)

Все административные интерфейсы (Grafana, Prometheus, 3x-ui) доступны только с клиентским сертификатом:

| Компонент | CA | Сертификат | TTL |
|-----------|-----|------------|-----|
| CA | Свой, 4096 bit RSA | — | 10 лет |
| Клиентский cert | Наш CA | P12 для браузера | 2 года |
| Серверный cert | Let's Encrypt / self-signed | — | 90 дней / 1 год |

```bash
# Обновить клиентский сертификат:
/renew-cert

# Обновить CA (делать очень редко):
/renew-ca
```

Watchdog предупредит за 14 дней (клиентский) и 30 дней (CA) до истечения.

### Watchdog API

- Слушает на `0.0.0.0:8080`
- nftables INPUT accept только `172.20.0.0/24` (Docker bridge)
- Bearer token (`WATCHDOG_API_TOKEN`) для авторизации
- Rate limit: 10 req/sec на все POST (slowapi)

### Telegram-бот

- Admin: только `TELEGRAM_ADMIN_CHAT_ID` из `.env`
- Клиенты: invite-код (одноразовый, TTL 24ч) → регистрация в БД
- Незарегистрированные: молчание (полное игнорирование)
- `/adddevice` для клиентов: требует одобрение администратора

---

## Секреты и ключи

### Хранение

| Секрет | Расположение | Права |
|--------|-------------|-------|
| Все секреты | `/opt/vpn/.env` | 600 (root only) |
| WireGuard ключи | `/etc/wireguard/*.key` | 600 |
| SSH ключ VPS | `/root/.ssh/vpn_id_ed25519` | 600 |
| mTLS CA ключ | `/etc/nginx/mtls/ca.key` | 600 |
| Клиентский cert | `/etc/nginx/mtls/client.p12` | 644 |

### Автогенерация при установке

setup.sh генерирует **все** секреты автоматически. Ничего не нужно придумывать вручную:

- WireGuard ключи (AWG + WG): `wg genkey | tee ... | wg pubkey`
- AmneziaWG параметры H1/H2/H3/H4: `openssl rand -hex 4` → uint32
- REALITY x25519 ключевая пара: `docker run teddysun/xray x25519`
- UUID для Xray: `uuidgen`
- Watchdog API token: `openssl rand -hex 32`
- GPG passphrase для бэкапов: `openssl rand -base64 32`
- Hysteria2 auth + obfs password: `openssl rand -base64 24`

### Git: что НИКОГДА не попадает в репозиторий

`.gitignore` исключает:
```
.env, *.key, *.pem, *.crt, *.p12, *.pfx, *.gpg
```

### Ротация ключей

```
/rotate-keys    — сгенерировать новые WireGuard ключи, разослать конфиги клиентам
```

После ротации: все клиенты получат новые конфиги. Нужно будет обновить WireGuard на всех устройствах.

---

## Сетевая изоляция

### nftables (домашний сервер)

```
INPUT: принимает только established + WG порты 51820/51821 + SSH + Docker 172.20.0.0/24
FORWARD: policy drop — весь проброс запрещён по умолчанию
POSTROUTING: только NAT для VPN-подсетей (10.177.1.0/24, 10.177.3.0/24)
```

Kill switch — критически важно:
```
# Kill switch stоит ПЕРЕД ct established (иначе не работает для VPN-трафика):
iifname { "wg0", "wg1" } ip daddr @blocked_static  oifname != "tun*" drop
iifname { "wg0", "wg1" } ip daddr @blocked_dynamic oifname != "tun*" drop
ct state established,related accept   # ПОСЛЕ kill switch
```

### nftables (VPS)

Rate limiting на входящие подключения к Xray:
```
tcp dport 443 limit rate 500/second burst 1000 accept
tcp dport 443 drop
udp dport 443 limit rate 300/second burst 500 accept
udp dport 443 drop
```

### Docker socket proxy

Telegram-бот не имеет прямого доступа к Docker daemon. Доступ через `socket-proxy:0.1.2` с ограниченным набором разрешённых команд (только чтение + restart).

### IPv6 отключён

На обоих серверах:
```bash
/etc/sysctl.d/99-disable-ipv6.conf:
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
```

Это предотвращает IPv6-утечки (VPN работает только с IPv4).

---

## Защита от атак

### fail2ban

| Сервис | Порог | Бан |
|--------|-------|-----|
| SSH | 5 попыток за 10 мин | 1 час |
| SSH (повторный) | — | Постоянный |
| Xray (VPS) | 20 попыток за 5 мин | 24 часа |

Watchdog уведомляет при каждом бане.

### Rate limiting

- WireGuard UDP: 100 пакетов/сек, burst 200 (защита от UDP flood)
- Watchdog API: 10 req/sec на POST endpoints (slowapi)
- VPS nftables: 500/300 соединений/сек на TCP/UDP 443

### Обновление ядра

```bash
# Ядро закреплено (не обновляется автоматически):
/etc/apt/preferences.d/pin-kernel:
Package: linux-image-*
Pin: release *
Pin-Priority: -1
```

Это предотвращает автоматическое обновление ядра которое сломает DKMS-модуль AWG.

`unattended-upgrades` настроен только на security-обновления пакетов (не ядра).

### Ротация соединений (анти-DPI)

Watchdog автоматически меняет соединение каждые 30–60 минут (рандомный интервал ±15 мин). Это затрудняет корреляционный анализ трафика по времени.

---

## Бэкапы и шифрование

Бэкапы содержат чувствительные данные (ключи, конфиги, БД). Защита:

```bash
# GPG симметричное шифрование:
echo "$BACKUP_GPG_PASSPHRASE" | gpg --batch \
  --passphrase-fd 0 \           # пароль через pipe, не в cmdline
  --symmetric \
  --cipher-algo AES256 \
  --s2k-digest-algo SHA512 \
  --s2k-count 65011712 \        # ~1 сек KDF на современном CPU
  --output backup.tar.gz.gpg \
  backup.tar.gz
```

Passphrase передаётся через `--passphrase-fd 0` (не виден в `ps aux`).

---

## Приватность конфигов

При отправке конфига клиенту бот **всегда** добавляет предупреждение:

> ⚠️ Этот файл содержит ваш приватный ключ. Не пересылайте его другим людям.
> Рекомендуется использовать двухфакторную аутентификацию Telegram.

---

## Чеклист безопасности

| Пункт | Статус | Команда |
|-------|--------|---------|
| SSH root отключён | setup.sh | — |
| SSH только по ключам | setup.sh | `ssh-keygen` |
| fail2ban активен | setup.sh | `systemctl status fail2ban` |
| mTLS панели | setup.sh | `/renew-cert` |
| Клиентские ключи уникальны | bot | `wg show` |
| Бэкап зашифрован GPG | backup.sh | `ls /opt/vpn/backups/*.gpg` |
| IPv6 отключён | setup.sh | `ip -6 addr show` |
| Kill switch работает | smoke test | `test_kill_switch.sh` |
| Rate limiting активен | setup.sh | `nft list table inet vpn` |
| Ядро закреплено | setup.sh | `apt-cache policy linux-image-$(uname -r)` |
| Сертификат не истёк | watchdog | `/status` |

# Безопасность

## Аутентификация

- SSH: только по ключам, root login закрыт
- Watchdog API: Bearer token + nftables (только 172.20.0.0/24)
- Grafana/Prometheus: mTLS + nginx reverse proxy
- Docker socket: только через socket-proxy с минимальными правами

## Сетевая защита

- **fail2ban**: на обоих серверах (SSH, Xray)
- **Rate limiting**: UDP 51820/51821 — 100 пакетов/сек
- **IPv6 отключён** на обоих серверах
- **nftables**: минимальные правила, policy drop

## Ключи и секреты

- Все секреты в .env (chmod 600)
- .env в .gitignore — НИКОГДА не коммитить
- Генерируются автоматически setup.sh
- Ротация: `/rotate-keys`

## Обновления

- Автообновления безопасности: unattended-upgrades (только security)
- Ядро заморожено (apt-mark hold + Pin-Priority -1)
- DKMS проверка после обновления ядра

## Docker

- Образы с фиксированными версиями (no :latest)
- Docker socket только через socket-proxy
- Dependabot/Renovate для обновления версий

## mTLS

- Собственный CA (4096 bit, TTL 10 лет)
- Клиентские сертификаты (TTL 2 года)
- Алерты за 14 дней до истечения клиентского, 30 дней для CA

## Приватность

- dnsmasq: НЕ логирует DNS-запросы
- Логи: per-client не хранятся
- Конфиги: предупреждение при отправке, рекомендация 2FA

## Ротация ключей

```
/rotate-keys
```

Генерирует новые WireGuard ключи и рассылает конфиги клиентам.

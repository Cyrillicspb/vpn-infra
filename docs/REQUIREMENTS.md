# Требования

## Home-server

- Ubuntu 24.04 LTS
- x86_64
- Ethernet
- минимум 4 GB RAM
- желательно 64+ GB SSD
- белый IP на роутере и port forwarding к серверу

Рекомендуемый baseline:

- 4 CPU cores
- 8 GB RAM
- 100+ GB SSD

## VPS

- Ubuntu 22.04 или 24.04 LTS
- KVM
- минимум 1 vCPU / 2 GB RAM
- 20+ GB SSD
- SSH-доступ
- размещение вне РФ

## Роутер

- белый WAN IP;
- UDP port forwarding;
- DHCP reservation для home-server;
- желательно DDNS, если внешний IP динамический.

## Сеть

- домашний upload критичен сильнее download;
- для нескольких клиентов нужен стабильный upload, а не только высокий download;
- direct и VPS egress могут попадать в разные геозоны, и это нужно учитывать в route policy.

## Внешние сервисы

Обязательные:

- VPS;
- Telegram.

Опциональные:

- Cloudflare/CDN;
- DDNS;
- собственный домен.

## Software baseline

Устанавливаются и поддерживаются скриптами репозитория:

- Docker / Docker Compose;
- WireGuard / AmneziaWG;
- dnsmasq;
- nftables;
- watchdog;
- Telegram-бот;
- smoke и post-install checks.

## Практический критерий пригодности

Оборудование и окружение считаются пригодными, если после установки выполняется такой baseline:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh
```

и оба прохода заканчиваются без критических ошибок.

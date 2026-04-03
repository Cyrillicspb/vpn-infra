# FAQ

## Нужен ли белый IP дома?

Да. Для прямого ingress на home-server нужен белый IP на домашнем роутере и проброс UDP-портов к серверу.

## Используют ли клиенты VPS как endpoint?

Нет. Клиенты всегда подключаются к home ingress. VPS участвует как backend для blocked/VPN lane, но не как клиентский endpoint.

## Можно ли жить без Cloudflare/CDN?

Да. CDN path опционален. Система не должна зависеть от него как от обязательного install/deploy контракта.

## Что делать, если Telegram-бот недоступен?

Использовать SSH:

```bash
sudo bash /opt/vpn/deploy.sh --status
sudo bash /opt/vpn/deploy.sh
sudo bash /opt/vpn/deploy.sh --rollback
```

## Чем rollback отличается от restore?

- `deploy.sh --rollback` — откат последнего release к snapshot.
- `restore.sh --full-restore <backup>` — disaster recovery из архивного backup.

Это разные механизмы.

## Почему сайт может открываться не так, как ожидается, даже при working tunnel?

Чаще всего причины такие:

- устройство использует внешний DNS / DoH вместо dnsmasq;
- домен ушёл в manual-direct/manual-vpn override;
- для сервиса не хватает catalog coverage;
- проверяется не сам домен сервиса, а сторонний CDN/bootstrap hostname.

Начинать нужно с `/check <domain>`.

## Можно ли редактировать runtime-конфиги вручную на сервере?

Не рекомендуется. Исправлять нужно генератор, шаблон или source-файл в репозитории. Ручные правки runtime-конфигов быстро расходятся с deploy/install logic.

## Какой минимальный набор проверок после изменений?

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

## Поддерживается ли zero-loss reinstall через export/import?

Не как текущий production-contract. Бэкапы и `restore.sh` работают, но полный `setup.sh --from-export` остаётся backlog/parked темой.

# Безопасность

## Что защищает система

- утечки blocked-трафика мимо VPN lane;
- утечки DNS при корректном использовании dnsmasq path;
- несанкционированный доступ к admin surface;
- случайные частичные deploy/recovery состояния.

## Что она не обещает

- анонимность от целевого государственного противника;
- защиту при компрометации домашнего сервера или телефона клиента;
- безопасность, если клиент раздаёт свой конфиг третьим лицам;
- корректную работу при внешнем DNS / DoH вне вашей policy.

## Секреты

Главный secret store:

- `/opt/vpn/.env`

Требование:

- не коммитить секреты;
- не дублировать секреты по репозиторию;
- не редактировать вручную repo-tracked шаблоны ради подстановки секретов.

## Админский доступ

Поддерживаемые admin surfaces:

- SSH;
- watchdog API с bearer token;
- Telegram-бот.

Практическое правило:

- опасные операции должны идти либо через watchdog API, либо через root-owned shell scripts;
- bot не должен иметь прямой безограниченный доступ к Docker socket или arbitrary shell path.

## Deploy safety

Безопасный deploy основан на таких правилах:

- один deploy за раз;
- snapshot перед apply;
- health gate перед commit;
- rollback при unsafe state;
- итог определяется по `/opt/vpn/.deploy-state/`, а не по логам.

## Backup и recovery

- backup содержит чувствительные данные;
- backup нужно хранить только в зашифрованном виде;
- `restore.sh` используется для DR, не для release rollback.

## Privacy baseline

Система не должна хранить:

- полную историю DNS запросов;
- полную историю посещённых сайтов;
- содержимое трафика клиентов.

Допустимый runtime-state:

- service-level routing metadata;
- bounded latency candidates/learned state;
- служебные health и deploy-state файлы.

## Риски, о которых важно помнить

- Клиент с включённым Private DNS / Secure DNS / DoH может обходить dnsmasq policy.
- Неправильный `manual-direct` может сломать geo-sensitive сервисы.
- Прямой и VPS egress могут находиться в разных странах, и это влияет на route expectations.
- Ручные правки runtime-конфигов часто ломают идемпотентность deploy/install.

## Минимальный security checklist

Проверять после крупных изменений:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

Дополнительно:

- проверить права на `/opt/vpn/.env`;
- проверить, что deploy-state на home и VPS согласован;
- убедиться, что client-facing configs по-прежнему указывают на home ingress, а не на VPS.

# Установка

## Поддерживаемый install path

Основной путь установки:

```bash
curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/install.sh | sudo bash
```

`install.sh` подготавливает `/opt/vpn` и запускает `setup.sh`.

## Что нужно заранее

### Home-server

- Ubuntu 24.04 LTS;
- x86_64;
- Ethernet;
- фиксированный LAN IP;
- белый IP на роутере;
- проброс UDP-портов к home-server.

### VPS

- Ubuntu 22.04 или 24.04 LTS;
- KVM;
- SSH-доступ;
- расположение вне РФ.

### Данные для инсталлятора

- `VPS_IP`;
- SSH port и учётные данные для первоначального доступа;
- Telegram bot token;
- Telegram admin chat id;
- опциональные CDN/DDNS параметры, если они реально используются.

## Что делает установка

`setup.sh`:

- собирает ввод и пишет `/opt/vpn/.env`;
- ставит home-server runtime;
- ставит VPS runtime;
- поднимает watchdog, bot и routing;
- выполняет post-install phase и базовые проверки.

## Идемпотентность

Установка хранит прогресс в `/opt/vpn/.setup-state`.
Если процесс прервался, повторный запуск `setup.sh` должен продолжить работу, а не начинать всё с нуля.

## Обязательный ручной шаг

На роутере нужно пробросить к home-server:

- UDP `51820`;
- UDP `51821`.

Без этого клиентский ingress работать не будет.

## Что проверить сразу после установки

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

Через бота:

- `/status`
- `/health`
- `/tunnel`

## Если установка сломалась

### Повторный запуск

Обычно достаточно:

```bash
cd /opt/vpn
sudo bash setup.sh
```

### Home-only / VPS-only rerun

Если нужно локально повторить только часть пайплайна:

```bash
sudo bash /opt/vpn/install-home.sh
sudo bash /opt/vpn/install-vps.sh
```

### Жёсткая диагностика

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh --verbose
```

### VPS reset

Если VPS нужно очистить и поднять заново, используйте [docs/TROUBLESHOOTING.md](/home/kirill/vpn-infra/docs/TROUBLESHOOTING.md).

## Что не считать install contract

Документация больше не обещает как обязательный контракт:

- GUI installer как основной путь;
- отдельные исторические фазы с фиксированными номерами шагов;
- обязательный Cloudflare/CDN path;
- release rollback через `restore.sh`.

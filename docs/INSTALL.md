# Установка

## Поддерживаемый install path

Основной путь установки:

```bash
curl -fsSL https://github.com/Cyrillicspb/vpn-infra/releases/latest/download/install.sh | sudo bash
```

Для конкретного release tag:

```bash
curl -fsSL https://github.com/Cyrillicspb/vpn-infra/releases/download/vX.Y.Z/install.sh | sudo bash
```

`install.sh` подготавливает `/opt/vpn`, скачивает обязательные release bundles именно из GitHub Release assets и запускает `setup.sh`.

## Модель установки

- основной режим установки: `TUI` через `setup.sh`;
- консольный режим: только fallback, если TUI не может быть запущен из локального release bundle;
- clean install должен стартовать только из полного release bundle, а не из `master`, raw-файлов или ad-hoc архива.

`setup.sh` сначала пытается поднять Textual-интерфейс из локального bundled wheel set. Если локальный TUI недоступен, установка может продолжиться в консольном режиме, но это fallback path, а не основной UX.

Поддерживаемый install contract:

- latest installer разрешён только как `GitHub Releases latest`, а не как moving `master`;
- для воспроизводимой установки использовать конкретный tag release;
- installer не опирается на `git clone` из default branch и не должен тянуть код из raw `master`.

## Bundle-first contract

Обязательные release assets:

- `vpn-infra.tar.gz`
- `system-packages-*`
- `docker-images-*`
- `installer-gui-wheels.tar.gz`
- `watchdog-wheels.tar.gz`
- `telegram-bot-wheels.tar.gz`

Для обязательных install-time зависимостей включён strict bundle-first режим:

- если обязательный release asset отсутствует, установка завершается ошибкой сразу;
- скрытый fallback в Docker Hub / PyPI / GitHub binary downloads для обязательных компонентов не считается поддерживаемым путём;
- raw `master` не считается поддерживаемым install source.

### Что считается обязательным bundled содержимым

- `vpn-infra.tar.gz` с полным репозиторием, включая `setup.sh`, `common.sh`, TUI и bundled runtime binaries;
- `installer-gui-wheels.tar.gz` для запуска TUI без PyPI fallback;
- `watchdog-wheels.tar.gz` и `telegram-bot-wheels.tar.gz`;
- `docker-images-*` и `system-packages-*`;
- install-critical transport binaries внутри `vpn-infra.tar.gz`, например `tools/hysteria2-*`, `tools/tun2socks-*`, bundled `nfqws` для bootstrap и runtime.

### Что допускается тянуть из сети

Только минимальные bootstrap prerequisites, без которых installer вообще не стартует на чистой Ubuntu:

- `python3` / `python3-pip` при их отсутствии;
- `tmux` как UX-защита от обрыва SSH;
- базовые системные пакеты из Ubuntu repository, если они относятся к documented bootstrap-minimal.

Даже в этом режиме install-critical runtime binaries и TUI dependencies должны приходить из release bundle, а не из `latest`.

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
- DuckDNS hostname и token, если нужен DDNS для home ingress;
- Cloudflare CDN параметры, только если вы отдельно включаете CDN-стек.

## DDNS contract

- installer поддерживает DDNS только через `DuckDNS`;
- `Cloudflare CDN` и `DDNS` не смешиваются: Cloudflare здесь отдельный CDN-стек, а не DDNS provider;
- при включённом DDNS installer запрашивает только `DuckDNS hostname` и `DuckDNS token`;
- в `.env` для совместимости сохраняется `DDNS_PROVIDER=duckdns`.

## Что делает установка

`setup.sh`:

- собирает ввод и пишет `/opt/vpn/.env`;
- запускает TUI как основной install UX;
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

- raw `master`, `git clone` и ad-hoc архивы как install source;
- сетевые `latest` fallback'и для install-critical binaries и TUI dependencies;
- консольный режим как равноправный основной UX;
- отдельные исторические фазы с фиксированными номерами шагов;
- обязательный Cloudflare/CDN path;
- release rollback через `restore.sh`.

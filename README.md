# vpn-infra

Самохостинговая двухуровневая VPN-инфраструктура:

- клиенты подключаются к home-server;
- blocked/VPN lane уходит через VPS;
- обычный трафик остаётся direct по policy;
- управление и диагностика идут через watchdog и Telegram-бота.

## Что сейчас является реальным контрактом

- install path: `install.sh` → `setup.sh`
- release path: `deploy.sh`
- release rollback: `deploy.sh --rollback`
- disaster recovery: `restore.sh --full-restore <backup>`
- verification baseline:
  - `post-install-check.sh`
  - `tests/run-smoke-tests.sh`
  - `deploy.sh --status`

## Быстрый старт

На home-server:

```bash
curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/install.sh | sudo bash
```

После установки:

```bash
sudo bash /opt/vpn/scripts/post-install-check.sh
cd /opt/vpn && bash tests/run-smoke-tests.sh
sudo bash /opt/vpn/deploy.sh --status
```

## Документация

- [Установка](docs/INSTALL.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Команды бота](docs/COMMANDS.md)
- [Обновление и recovery](docs/UPDATE.md)
- [Deploy state contract](docs/DEPLOY-STATE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Требования](docs/REQUIREMENTS.md)
- [Безопасность](docs/SECURITY.md)
- [FAQ](docs/FAQ.md)
- [TODO / backlog](docs/TODO-SPECS.md)

## Что убрано из README намеренно

README больше не пытается быть полной спецификацией системы. Из него убраны:

- исторические пошаговые install-фазы с фиксированными номерами;
- старые схемы со старыми optional-компонентами как будто они обязательны;
- ссылки на несуществующие документы;
- обещания, которые больше не являются production-contract.

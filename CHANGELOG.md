# Changelog

Все значимые изменения проекта фиксируются здесь.
Формат: [SemVer](https://semver.org/), даты UTC.

---

## [v1.1.0] — 2026-03-17

### Добавлено
- Автоматическая проверка обновлений раз в час с уведомлением в Telegram
- Кнопки [Обновить] / [Пропустить] в уведомлении об обновлении
- История изменений (CHANGELOG.md) и нумерация версий (SemVer)

### Исправлено
- Логи Docker-контейнеров через socket-proxy API (не требуется docker CLI внутри контейнера)
- Автоматическая пересборка образов telegram-bot и xray при изменении исходников

---

## [v1.0.0] — 2026-03-12

### Добавлено
- Двухуровневая VPN-инфраструктура: AmneziaWG + WireGuard клиенты
- Адаптивный failover: 4 стека (CDN, XHTTP-jsdelivr, XHTTP-microsoft, Hysteria2)
- Split tunneling Hybrid B+: AllowedIPs на клиенте + nftables fwmark на сервере
- Telegram-бот: управление клиентами, конфигами, мониторинг
- Watchdog: центральный агент, HTTP API, автозапуск tun при старте
- dnsmasq: nftset blocked_dynamic, прогрев DNS-кэша
- nftables: blocked_static (36k записей), kill switch, rate limiting
- combined.cidr: 266 CIDR-записей, cron обновление 03:00
- deploy.sh: снапшот, миграции, smoke-тесты, auto-rollback
- Мониторинг: Prometheus + Grafana на VPS, алерты в Telegram

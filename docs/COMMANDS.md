# Команды бота

Весь интерфейс на русском языке.

## Команды администратора

| Команда | Описание |
|---------|----------|
| `/status` | Статус системы (стек, IP, uptime) |
| `/tunnel` | Статус туннеля и WG peers |
| `/ip` | Внешний IP |
| `/docker` | Статус Docker контейнеров |
| `/clients` | Список зарегистрированных клиентов |
| `/speed` | Speedtest через туннель |
| `/logs <сервис> [N]` | Последние N строк логов |
| `/graph [тип] [период]` | График из Grafana |
| `/switch <стек>` | Переключить VPN стек вручную |
| `/restart <сервис>` | Перезапустить сервис |
| `/reboot` | Перезагрузка сервера (с подтверждением) |
| `/update` | Обновить Docker образы |
| `/deploy` | Обновить из git |
| `/rollback` | Откатить обновление |
| `/invite` | Создать код приглашения |
| `/client disable\|enable\|kick\|limit <имя>` | Управление клиентом |
| `/broadcast <текст>` | Рассылка всем клиентам |
| `/vpn add\|remove <домен>` | Управление VPN-маршрутами |
| `/direct add\|remove <домен>` | Управление прямыми маршрутами |
| `/list vpn\|direct` | Список маршрутов |
| `/check <домен>` | Проверить куда идёт домен |
| `/routes update` | Обновить базы РКН |
| `/requests` | Запросы клиентов на модерации |
| `/vps list\|add\|remove` | Управление VPS |
| `/migrate-vps <IP>` | Миграция на новый VPS |
| `/rotate-keys` | Ротация ключей |
| `/diagnose <устройство>` | Диагностика устройства |
| `/menu` | Главное меню |

## Команды клиента

| Команда | Описание |
|---------|----------|
| `/start` | Регистрация или главная |
| `/mydevices` | Мои устройства |
| `/myconfig [имя]` | Получить конфигурацию |
| `/adddevice` | Добавить устройство |
| `/removedevice` | Удалить устройство |
| `/update` | Получить обновлённый конфиг |
| `/request vpn\|direct <домен>` | Запросить маршрут |
| `/myrequests` | Мои запросы |
| `/exclude add\|remove\|list <подсеть>` | Исключения подсетей |
| `/report <текст>` | Сообщение администратору |
| `/status` | Статус VPN |
| `/help` | Справка |

## Форматы

### /switch
```
/switch hysteria2
/switch reality
/switch reality-grpc
/switch cloudflare-cdn
```

### /logs
```
/logs watchdog 50
/logs dnsmasq
/logs telegram-bot 100
```

### /vpn
```
/vpn add example.com
/vpn remove example.com
```

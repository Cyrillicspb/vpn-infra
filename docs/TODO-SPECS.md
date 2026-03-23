# TODO — Подробные спецификации

Этот документ содержит детальные технические спецификации для запланированных фич.
Краткий перечень задач — в `CLAUDE.md` → TODO.

-----

## Email fallback для алертов при недоступности Telegram

**Приоритет: низкий**

Когда Telegram API недоступен (режим белых списков), watchdog отправляет алерты через email.
- SMTP: smtp.yandex.ru / smtp.mail.ru (российские, в белом списке)
- Реализация: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL` в `.env`; watchdog при N неудачных попытках Telegram переключается на email
- Обратное переключение: при восстановлении Telegram API — снова через Telegram

-----

## Полуавтоматическое обновление пресетов DPI-bypass

- **Источник**: v2fly/domain-list-community — только как источник доменов для уже известных сервисов. НЕ автодискавери новых сервисов из v2fly (их там ~400).
- **Набор сервисов фиксированный**: youtube, instagram, twitter, tiktok, spotify, steam. Новый сервис добавляется только через код (V2FLY_MAPPING) или вручную через `/dpi add`. Cron обновляет домены, не сервисы.
- **Три слоя**: tier1=core (hardcoded, неудаляемые), tier2=v2fly (обновляемые), tier3=locked (пользователь через бот).
- **Хранение**: `/etc/vpn/dpi-presets.json` (cron пишет) + `home/dpi/presets-default.json` (fallback в репо). `DPI_SERVICE_PRESETS` в watchdog загружается из файла, не захардкожен.
- **Фильтрация v2fly**: исключать api./auth./login./accounts. домены; дедупликация (родитель покрывает поддомены в dnsmasq); cap 15 доменов на сервис.
- **Расписание**: systemd timer, воскресенье 03:30, после обновления маршрутов.
- **Watchdog**: новый endpoint `POST /dpi/presets/reload` для hot-reload без рестарта; алерт если presets не обновлялись >30 дней.
- **Самообучение**: conntrack-счётчик хитов по доменам из dpi_direct; домены с нулевым трафиком за 30 дней → предложение удалить (алерт админу).

-----

## GUI-установщик

**Архитектура: TUI на сервере, лаунчер на клиенте**
- TUI (Python + Textual) запускается на сервере — там уже есть Python (нужен для watchdog)
- Клиент — только SSH-терминал (встроен в Windows 10+, macOS, Linux). Никаких зависимостей на клиенте.
- Лаунчер (`.bat` / `.command` / `.sh`) делает одно: SSH-ключ + scp installer.py + `ssh -t server python3 installer.py`
- Все экраны, детект сети, тест VPS, запуск `setup.sh` — внутри TUI на сервере через subprocess

**Лаунчеры — единый flow, красивый вывод:**
- Windows: `.bat` с ANSI-цветами (VTP, cmd Windows 10+)
- macOS: `.command` (bash + ANSI)
- Linux: `.sh` (bash + ANSI, тот же скрипт что macOS)
- Единственный вопрос на клиенте: IP сервера
- Пошаговый прогресс: `✓ SSH доступен`, `→ Копирование ключа...`, `✓ Готово`
- Пароль запрашивается один раз (для SSH-ключа), после — только ключ

**TUI-установщик (Textual, 10 экранов):**
- Layout: header (шаг N/8) + content + footer (Назад / Помощь / Далее)
- Экраны: Приветствие → Подключение → Автодетект сети + Режим → VPS → Telegram → Опции → Review → Установка → Завершение
- Экран "Автодетект сети": LAN IP, интерфейс, CGNAT; кнопки `[A] Сервер на хостинге` / `[B] Сервер дома за роутером`
- Экран "Telegram": поле TOKEN + ID; подпись: root-администратор, неудаляем, `/admin invite` для других
- Inline-валидация, [Далее] заблокирован до прохождения
- [Проверить подключение] на экранах сервера и VPS
- [? Помощь] — side-panel с контекстной инструкцией
- Экран установки: прогресс-бар + консоль, `##PROGRESS:N:51:описание`
- State persistence: `~/.vpn-installer-state.json`

**Структура файлов:**
```
installers/
├── gui/
│   ├── installer.py       ← Textual App
│   ├── screens/           ← по файлу на экран
│   ├── components/        ← ValidatedInput, ProgressPane, ConsolePane
│   └── state.py           ← InstallerState + JSON persist
├── windows/install-windows.bat
├── macos/install-macos.command
└── linux/install-linux.sh
```

-----

## Режим B — Gateway Mode

Сервер физически дома за роутером. Роутер имеет белый IP + port forward. Сервер становится шлюзом для всей домашней сети.

```
РОУТЕР (80.93.52.223, port forward UDP 51820/51821 → 192.168.1.100)
  │ LAN 192.168.1.0/24
  ├── ДОМАШНИЙ СЕРВЕР (192.168.1.100) ← шлюз
  ├── Smart TV     (gateway=192.168.1.100) ← прозрачный VPN
  ├── Консоль      (gateway=192.168.1.100) ← прозрачный VPN
  └── ПК           (gateway=192.168.1.100) ← прозрачный VPN

Мобильные AWG/WG клиенты — endpoint = IP роутера
  Дома (WiFi): HAIRPIN redirect → WireGuard ✅
  Вне дома:    интернет → роутер → port forward → сервер ✅
```

### 1. HAIRPIN NAT
```
prerouting_nat (type nat hook prerouting priority dstnat):
iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr $ROUTER_EXTERNAL_IP
    udp dport 51820 redirect to :51820
iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr $ROUTER_EXTERNAL_IP
    udp dport 51821 redirect to :51821
```
Динамический IP роутера: nft set `router_external_ips`, watchdog обновляет.

### 2. Split tunneling для LAN-устройств (nftables)
```
chain prerouting (mangle):
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @dpi_direct    meta mark set 0x2 accept
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @blocked_static  meta mark set 0x1 accept
  iifname $LAN_IFACE ip saddr $LAN_SUBNET ip daddr @blocked_dynamic meta mark set 0x1 accept

chain forward:
  ip saddr $LAN_SUBNET ip daddr @blocked_static  oifname != "tun*" drop  # Kill switch LAN
  ip saddr $LAN_SUBNET ip daddr @blocked_dynamic oifname != "tun*" drop
  iifname $LAN_IFACE ip saddr $LAN_SUBNET accept
  oifname $LAN_IFACE ip daddr $LAN_SUBNET accept

chain postrouting:
  ip saddr $LAN_SUBNET oifname "tun*" masquerade  # Только для tun, не eth0
```

### 3. Policy routing для LAN
- `ip rule add priority 200 from $LAN_SUBNET lookup 100`
- table 100: `default via 192.168.1.1 dev $LAN_IFACE`

### 4. dnsmasq — слушать на LAN
- `interface=$LAN_IFACE` в dnsmasq.conf
- Smart TV → DNS → dnsmasq → VPS DNS → IP в blocked_dynamic → fwmark → tun

### 5. Failsafe
- tun упал: незаблокированное ✅, заблокированное → kill switch DROP ✅
- nftables упал: forward drop → watchdog мониторит 30 сек → перезагрузка
- Сервер недоступен: LAN теряет шлюз (PoF) → документировать ИБП

### 6. Изменения в setup.sh
- Фаза 0: `[A] Сервер на хостинге  [B] Сервер дома за роутером`
- .env: `SERVER_MODE=gateway`, `LAN_IFACE`, `LAN_SUBNET`, `ROUTER_EXTERNAL_IP`
- nftables-gateway.conf.j2 (отдельный шаблон)
- Фаза 5: инструкция по port forward + DHCP gateway/DNS

### 7. Watchdog дополнения
- Мониторинг nftables 30 сек → перезагрузка при пустом ruleset
- /status: кол-во LAN-клиентов из conntrack
- nft set router_external_ips обновлять при смене IP

### 8. Что НЕ меняется
Плагины стеков, failover, конфиг-билдер, бот, базы РКН, AllowedIPs. Конфиги клиентов: endpoint = ROUTER_EXTERNAL_IP, HAIRPIN решает.

-----

## Multi-admin

**Модель прав:**
- **Root admin** = `TELEGRAM_ADMIN_CHAT_ID` из `.env`. Хранится только в `.env`, не в БД. Неудаляем.
- **Дополнительные admins** = `is_admin=1` в таблице `clients`. Те же права, кроме `/admin add|remove` (только root).

**`_is_admin()` в handlers/admin.py:**
```python
def _is_admin(uid: int) -> bool:
    return str(uid) == str(config.admin_chat_id) or db.is_admin(uid)

def _is_root(uid: int) -> bool:
    return str(uid) == str(config.admin_chat_id)
```
Все 59 мест с проверкой прав используют `_is_admin()`. Управление adminами — `_is_root()`.

**Миграция БД:** `admin_added_by TEXT` в clients, `grants_admin INTEGER DEFAULT 0` в invite_codes.

**Команды:**
```
/admin list                  — все админы
/admin invite                — admin-invite (только root)
/admin remove <username>     — снять права (только root)
```

**Watchdog:** `state.admin_chat_ids` — список всех для алертов. `POST /admin-notify`, `POST /admin-notify/reload`.

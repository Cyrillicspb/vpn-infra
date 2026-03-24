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

-----

## Экспорт/импорт данных для чистой переустановки

**Приоритет: средний**

### Анализ существующего механизма

`backup.sh` + `restore.sh` уже реализуют большую часть нужного функционала. Это **расширение**, а не новая система.

**Что backup.sh уже сохраняет:**
- `/etc/wireguard/` — WireGuard/AWG ключи и серверные конфиги (включая пиры)
- `/opt/vpn/.env` — все секреты (UUID, токены, пароли, REALITY ключи, Hysteria2)
- `vpn_bot.db` — SQLite БД (clients, devices, invite_codes, excludes, domain_requests)
- nftables конфиги, hysteria config, xray конфиги, dnsmasq конфиги
- `/etc/vpn-routes/manual-*.txt` — ручные маршруты
- watchdog plugins

**Что НЕ сохраняется (gap):**
| Данные | Где живут | Критичность |
|--------|-----------|-------------|
| mTLS CA ключ + сертификат | VPS `/opt/vpn/nginx/mtls/ca.key`, `ca.crt` | Высокая — без CA нельзя выпустить новый клиентский cert |
| watchdog `state.json` | `/opt/vpn/watchdog/state.json` | Средняя — baseline RTT, активный стек |
| DPI presets | `/etc/vpn/dpi-presets.json` | Низкая — Thompson Sampling состояние |
| Cloudflared tunnel cert | `~/.cloudflared/*.json` | Средняя — нужен если используется CDN стек |

**setup.sh:** нет флага `--from-export` — при чистой установке шаг `step07_generate_secrets` всегда генерирует новые ключи.

---

### Архитектурное решение

Три расширения существующих скриптов + один новый эндпоинт:

1. `backup.sh --full-export` — расширенный бэкап с mTLS и state
2. `restore.sh` — обработка новых данных + IP-детекция
3. `setup.sh --from-export <file>` — пропуск генерации ключей
4. watchdog `POST /backup/export` + бот `/backup export`

---

### Шаг 1 — backup.sh: флаг --full-export

**Файл:** `home/scripts/backup.sh` | **Сложность:** Low

**Изменения:**
```bash
# Новый флаг
--full-export   # включает mTLS (SSH к VPS), DPI presets; имя файла vpn-export-*

# Дополнения к стандартному составу (всегда, без флага):
/opt/vpn/watchdog/state.json          → watchdog-state.json

# Только при --full-export:
VPS:/opt/vpn/nginx/mtls/ca.key        → mtls/ca.key       (scp через SSH_KEY)
VPS:/opt/vpn/nginx/mtls/ca.crt        → mtls/ca.crt
/etc/vpn/dpi-presets.json             → dpi-presets.json  (если существует)
~/.cloudflared/                       → cloudflared/       (если существует)

# Обновление metadata.json:
{
  "export_type":    "full-export" | "backup",
  "vpn_version":    "...",
  "client_count":   N,           # из SQLite: SELECT COUNT(*) FROM clients
  "has_mtls":       true/false,
  "home_server_ip": "...",       # EXTERNAL_IP из .env — для IP-детекции при импорте
}
```

**Graceful degradation:** если VPS недоступен по SSH → создать экспорт без mTLS + предупреждение в stdout и Telegram. Не fatal.

**Имя файла:** `vpn-export-${TIMESTAMP}.tar.gz.gpg` (для `--full-export`), `vpn-backup-*` остаётся для обычного бэкапа.

---

### Шаг 2 — restore.sh: новые компоненты + IP-детекция

**Файл:** `restore.sh` | **Сложность:** Medium

**Добавить в `restore_configs()`:**
```bash
# 1. watchdog state
if [[ -f "$src/watchdog-state.json" ]]; then
    cp "$src/watchdog-state.json" /opt/vpn/watchdog/state.json
fi

# 2. mTLS CA — восстановить на VPS через SSH
if [[ -d "$src/mtls" && -f "$src/mtls/ca.key" ]]; then
    # VPS_IP берётся из .env (уже восстановлен)
    ssh -i $SSH_KEY sysadmin@$VPS_IP "mkdir -p /opt/vpn/nginx/mtls"
    scp -i $SSH_KEY "$src/mtls/ca.key" "$src/mtls/ca.crt" \
        "sysadmin@${VPS_IP}:/opt/vpn/nginx/mtls/"
    ssh -i $SSH_KEY sysadmin@$VPS_IP "chmod 600 /opt/vpn/nginx/mtls/ca.key && \
        docker restart nginx 2>/dev/null || true"
fi

# 3. DPI presets
if [[ -f "$src/dpi-presets.json" ]]; then
    mkdir -p /etc/vpn
    cp "$src/dpi-presets.json" /etc/vpn/dpi-presets.json
fi

# 4. cloudflared tunnel credentials
if [[ -d "$src/cloudflared" ]]; then
    mkdir -p ~/.cloudflared
    cp -r "$src/cloudflared/." ~/.cloudflared/
fi
```

**Добавить после `restart_services()`:**
```bash
# IP-детекция: если IP сервера изменился — разослать обновлённые конфиги
detect_ip_change() {
    local meta="$RESTORE_TMP/metadata.json"
    [[ -f "$meta" ]] || return 0

    local backup_ip current_ip
    backup_ip=$(python3 -c "import json; \
        d=json.load(open('$meta')); print(d.get('home_server_ip',''))" 2>/dev/null || echo "")
    current_ip=$(curl -sf --max-time 5 https://icanhazip.com 2>/dev/null || echo "")

    if [[ -n "$backup_ip" && -n "$current_ip" && "$backup_ip" != "$current_ip" ]]; then
        log_warn "IP изменился: $backup_ip → $current_ip"
        # Обновить EXTERNAL_IP в .env
        sed -i "s/^EXTERNAL_IP=.*/EXTERNAL_IP=${current_ip}/" "$ENV_FILE"
        # После запуска watchdog — запустить рассылку конфигов
        TRIGGER_NOTIFY_CLIENTS=true
    fi
}
```

**Новый флаг `--check-export <file>`:** pre-flight валидация без применения — проверить наличие обязательных файлов (.env, wireguard/, vpn_bot.db), вывести состав и предупреждения.

---

### Шаг 3 — setup.sh: флаг --from-export

**Файл:** `setup.sh` | **Сложность:** Medium-High

**Парсинг в начале main:**
```bash
IMPORT_MODE=false
IMPORT_FILE=""
for arg in "$@"; do
    case "$arg" in
        --from-export) IMPORT_MODE=true; shift; IMPORT_FILE="${1:-}"; shift ;;
    esac
done
```

**Перед фазой 0 (если IMPORT_MODE):**
```bash
# Расшифровать и распаковать экспорт
IMPORT_DIR="$(mktemp -d)"
# Decrypt + extract → $IMPORT_DIR
# Загрузить .env из экспорта → /opt/vpn/.env
# Сохранить путь к распакованному архиву
```

**В step05 (collect_inputs) — если IMPORT_MODE:**
- Пропустить интерактивный ввод VPS IP, Telegram токена и т.д. — всё берётся из `.env`
- Показать загруженные значения пользователю для подтверждения

**В step07 (generate_secrets) — если IMPORT_MODE:**
```bash
if $IMPORT_MODE; then
    step_skip "step07_generate_secrets"  # ключи уже в .env из экспорта
    step_done "step07_generate_secrets"
    return 0
fi
```

WireGuard конфиги из экспорта копируются в `/etc/wireguard/` на шаге установки AmneziaWG (step12) — аналогично через `if $IMPORT_MODE → cp из $IMPORT_DIR/wireguard/`.

**После завершения всех фаз:**
```bash
if $IMPORT_MODE; then
    log_info "Восстановление данных из экспорта..."
    # Восстановить SQLite, mTLS, routes, watchdog state, DPI presets
    bash restore.sh --restore-data-only "$IMPORT_FILE"
    # IP-детекция и рассылка конфигов при необходимости
fi
```

**Совместимость версий:** сравнить `vpn_version` из metadata экспорта с текущей. Если отличается → запустить `migrations/apply.sh` автоматически.

**Итоговый UX:**
```bash
# Переустановка на новый сервер с сохранением всех пользователей:
sudo bash setup.sh --from-export vpn-export-20260324_120000.tar.gz.gpg
```

---

### Шаг 4 — Telegram-бот + watchdog: /backup export

**Файлы:** `home/telegram-bot/handlers/admin.py`, `home/watchdog/watchdog.py` | **Сложность:** Low

**Watchdog — новый эндпоинт:**
```
POST /backup/export      → запускает backup.sh --full-export в фоне
                           по завершении отправляет файл через TelegramQueue
```

**Бот — новая подкоманда:**
```
/backup                  — обычный бэкап (существующий)
/backup export           — полный экспорт: WG-ключи + mTLS + state + DPI
                           "Создаётся полный экспорт (~30–60 сек)..."
                           → файл vpn-export-*.tar.gz.gpg в чат
```

---

### Зависимости между шагами

```
Шаг 1 (backup.sh)  →  независим, делать первым
Шаг 2 (restore.sh) →  после Шага 1 (понимает новую структуру архива)
Шаг 3 (setup.sh)   →  после Шагов 1 + 2
Шаг 4 (бот)        →  после Шага 1 (вызывает --full-export)
```

Шаги 1 и 4 можно сделать параллельно. Шаги 2 и 3 — после Шага 1.

---

### Риски и митигации

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| VPS недоступен при экспорте → нет mTLS CA | Средняя | Graceful degradation: экспорт без mTLS + предупреждение. При импорте → автоматически перевыпустить CA через `/renew-ca` |
| setup.sh идемпотентность с IMPORT_MODE | Средняя | При `--from-export` стереть шаги step05/step07 из `.setup-state` перед запуском |
| Cloudflared tunnel token ≠ credentials файл | Низкая | Проверить `~/.cloudflared/` при экспорте; если нет → только token из .env достаточен для пересоздания |
| Версионная несовместимость схемы БД | Низкая | `migrations/apply.sh` уже существует, запускать автоматически при IMPORT_MODE |
| Файл экспорта > 50 МБ (Telegram лимит) | Низкая | Отправить только ссылку для скачивания с VPS; в Telegram — только уведомление |

### Что НЕ меняется
- Обычный бэкап (`backup.sh` без флага) — без изменений, не ломаем существующий cron
- `home/scripts/restore.sh` — только алиас на корневой, остаётся
- Формат GPG-шифрования — тот же AES256
- Ротация 30 дней — та же политика

-----

## WiFi как резервный канал (опционально)

**Приоритет: Low**

При установке на Mac mini (или любой сервер с WiFi):
- Установить Broadcom драйвер (bcmwl-kernel-source) если нужно
- Спросить SSID + пароль
- Netplan: Ethernet metric 100 (основной), WiFi metric 600 (резервный)
- При падении Ethernet — автоматический failover на WiFi
- Watchdog алерт: "Ethernet недоступен, работаю через WiFi (деградация)"
- При восстановлении Ethernet — автоматический возврат

Также: автозапуск при подаче питания (Mac: nvram AutoBoot=%03, PC: предупреждение о BIOS).

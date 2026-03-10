# Восстановление после сбоя (Disaster Recovery)

## Содержание

- [Сценарий 1: Домашний сервер вышел из строя](#сценарий-1-домашний-сервер-вышел-из-строя)
- [Сценарий 2: Повреждена ОС (сервер жив)](#сценарий-2-повреждена-ос-сервер-жив)
- [Сценарий 3: VPS недоступен](#сценарий-3-vps-недоступен)
- [Сценарий 4: Оба сервера недоступны](#сценарий-4-оба-сервера-недоступны)
- [Сценарий 5: Смена VPS](#сценарий-5-смена-vps)
- [Сценарий 6: Компрометация ключей](#сценарий-6-компрометация-ключей)
- [Сценарий 7: Утеря бэкапа](#сценарий-7-утеря-бэкапа)
- [Быстрый старт: команды восстановления](#быстрый-старт-команды-восстановления)

---

## Хранение бэкапов

Перед восстановлением убедитесь что бэкап доступен:

| Расположение | Где найти | TTL |
|--------------|-----------|-----|
| Домашний сервер | `/opt/vpn/backups/vpn-backup-*.tar.gz.gpg` | 30 дней |
| VPS | `/opt/vpn/backups/vpn-backup-*.tar.gz.gpg` | 30 дней |
| Telegram | Сообщение от бота в чате с собой | Навсегда |

Бэкап содержит: WireGuard ключи, `.env`, SQLite БД, nftables конфиги, Hysteria2, Xray, dnsmasq конфиги, плагины watchdog.

GPG-passphrase для расшифровки: был сохранён в `BACKUP_GPG_PASSPHRASE` из `.env`. Если `.env` утерян — нужен passphrase из Telegram-бэкапа.

---

## Сценарий 1: Домашний сервер вышел из строя

**Симптомы:** Сервер не загружается / физически повреждён / нет POST.

**Время восстановления: ~40–60 минут**

### Шаг 1: Подготовить новое железо

- Установить Ubuntu Server 24.04.2 LTS
- Имя пользователя: `sysadmin`
- Включить OpenSSH Server
- Подключить по Ethernet, назначить тот же LAN IP (через DHCP reservation на роутере)

### Шаг 2: Получить бэкап

```bash
# Скачать бэкап с VPS (если VPS доступен):
scp -i ~/.ssh/vpn_vps -P <SSH_PORT> \
    sysadmin@<VPS_IP>:/opt/vpn/backups/vpn-backup-<YYYYMMDD_HHMMSS>.tar.gz.gpg \
    ~/vpn-backup.tar.gz.gpg

# Или скачать из Telegram (сохранённый файл)
```

### Шаг 3: Запустить восстановление

```bash
# Скачать restore.sh:
curl -sO https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/restore.sh
# (если GitHub заблокирован — скачайте с VPS через туннель)

# Запустить восстановление:
sudo bash restore.sh vpn-backup-YYYYMMDD_HHMMSS.tar.gz.gpg
```

`restore.sh` сделает:
1. Расшифрует бэкап (GPG)
2. Проверит sha256
3. Запустит `install-home.sh` если нужна свежая установка
4. Восстановит по порядку: `.env` → WG ключи → nftables → Hysteria2 → Xray → dnsmasq → SQLite → vpn-routes → watchdog плагины
5. Перезапустит все сервисы в правильном порядке

### Шаг 4: Проверить

```bash
sudo systemctl status watchdog dnsmasq wg-quick@wg0
# Затем в Telegram-боте:
# /status
```

> **Примечание:** Клиентам не нужно обновлять конфиги — ключи остались прежними.
> Если IP домашнего сервера изменился — нужно обновить DDNS или разослать новые конфиги.

---

## Сценарий 2: Повреждена ОС (сервер жив)

**Симптомы:** Загружается, но сервисы не работают / конфиги повреждены / неудачное обновление ядра.

**Время восстановления: ~20–30 минут**

### Вариант А: Откат к снимку deploy

```bash
sudo bash /opt/vpn/deploy.sh --rollback
```

### Вариант Б: Восстановление из бэкапа (ОС цела)

```bash
cd /opt/vpn
sudo bash restore.sh /opt/vpn/backups/vpn-backup-<LATEST>.tar.gz.gpg

# Или восстановить конкретные компоненты вручную:
sudo bash restore.sh --component wireguard vpn-backup-LATEST.tar.gz.gpg
sudo bash restore.sh --component env      vpn-backup-LATEST.tar.gz.gpg
sudo bash restore.sh --component database vpn-backup-LATEST.tar.gz.gpg
```

### Вариант В: DKMS слетел после обновления ядра

```bash
sudo apt install linux-headers-$(uname -r)
sudo dkms install amneziawg -v $(dkms status | grep amneziawg | awk '{print $2}' | tr -d ',')
sudo systemctl restart wg-quick@wg0 wg-quick@wg1
```

### Вариант Г: watchdog не запускается

```bash
# Пересоздать venv:
sudo rm -rf /opt/vpn/venv
sudo python3 -m venv /opt/vpn/venv
sudo /opt/vpn/venv/bin/pip install -r /opt/vpn/home/watchdog/requirements.txt
sudo systemctl restart watchdog
```

---

## Сценарий 3: VPS недоступен

**Симптомы:** Watchdog присылает «Heartbeat lost», туннель упал.

### Диагностика

```bash
# Ping VPS:
ping <VPS_IP> -c 5

# SSH:
ssh -i /root/.ssh/vpn_id_ed25519 sysadmin@<VPS_IP>

# Через веб-консоль VPS-провайдера: проверьте состояние VM
```

### Временное решение: продолжать работу без VPS

При недоступности VPS watchdog не может переключиться на другой стек — все 4 стека ведут к одному VPS. Клиенты видят «VPN недоступен».

Нет автоматического аварийного режима без VPS — это ограничение архитектуры.

### Восстановление VPS

**Если VPS завис (не отвечает на SSH):**
1. Войдите в веб-консоль VPS-провайдера (Hetzner Console, Vultr VNC, etc.)
2. Hard reset VM
3. После загрузки: `docker compose up -d`

**Если VPS потерян полностью:** → см. [Сценарий 5: Смена VPS](#сценарий-5-смена-vps)

---

## Сценарий 4: Оба сервера недоступны

**Время восстановления: ~60–90 минут**

1. Арендуйте новый VPS
2. На новом железе установите Ubuntu 24.04
3. Скачайте бэкап из Telegram
4. Запустите восстановление:

```bash
# На новом домашнем сервере:
sudo bash restore.sh vpn-backup-LATEST.tar.gz.gpg

# restore.sh автоматически настроит новый VPS (нужен IP нового VPS)
sudo bash restore.sh vpn-backup-LATEST.tar.gz.gpg --vps-ip <НОВЫЙ_VPS_IP>
```

5. Обновите port forwarding на роутере (если домашний IP изменился)
6. Разошлите новые конфиги клиентам: `/notify-clients`

---

## Сценарий 5: Смена VPS

**Когда:** VPS заблокирован по IP / провайдер прекратил работу / переезд в другую локацию.

### Через Telegram-бота

```
/migrate-vps <IP_НОВОГО_VPS>
```

Бот проведёт пошаговую миграцию:
1. Установит Docker + зависимости на новом VPS
2. Скопирует конфиги и секреты
3. Поднимет все сервисы
4. Создаст Tier-2 туннель к новому VPS
5. Протестирует все 4 стека
6. Разошлёт новые конфиги клиентам (ключи остаются — только Endpoint меняется)

### Через restore.sh

```bash
# Восстановить VPS-компоненты на новый VPS:
sudo bash restore.sh vpn-backup-LATEST.tar.gz.gpg --migrate-vps <НОВЫЙ_VPS_IP>

# С восстановлением ключей из бэкапа:
sudo bash restore.sh vpn-backup-LATEST.tar.gz.gpg --migrate-vps <НОВЫЙ_VPS_IP> --from-backup
```

### Что делают клиенты?

Если используется **DDNS** — ничего. Endpoint в конфигах = DDNS-домен, который обновится автоматически.

Если **без DDNS** — бот разошлёт новые конфиги автоматически. Клиентам нужно обновить WireGuard.

---

## Сценарий 6: Компрометация ключей

**Когда подозревать:** необычный трафик, посторонний peer в `wg show`, доступ к серверу без вашего ведома.

### Немедленные действия

```bash
# 1. Заблокировать всех пиров:
sudo wg set wg0 peer <PUBKEY> remove  # для каждого подозрительного

# 2. Через бота — ротация всех ключей:
/rotate-keys
```

`/rotate-keys` сделает:
1. Сгенерирует новые приватные/публичные ключи для сервера
2. Пересоздаст всех peers с новыми ключами
3. Разошлёт новые конфиги всем клиентам
4. Перезапустит WireGuard интерфейсы

### После ротации

```bash
# Проверьте что нет посторонних peers:
sudo wg show wg0
sudo wg show wg1

# Проверьте authorized_keys:
cat /root/.ssh/authorized_keys
cat /home/sysadmin/.ssh/authorized_keys

# Проверьте логи входов:
last -n 30
journalctl -u sshd --since "7 days ago"
```

---

## Сценарий 7: Утеря бэкапа

Если все бэкапы утеряны (редкий случай):

1. Установка с нуля: `sudo bash setup.sh`
2. Все клиенты получат новые конфиги через FSM-регистрацию
3. Базы РКН загрузятся автоматически
4. Из потерь: история домейн-запросов, список клиентов, invite-коды

### Профилактика

```bash
# Проверить что бэкапы создаются:
ls -la /opt/vpn/backups/ | tail -5
cat /var/log/vpn-backup.log | tail -20

# Сделать немедленный бэкап:
sudo bash /opt/vpn/home/scripts/backup.sh

# Проверить dry-run:
sudo bash /opt/vpn/home/scripts/backup.sh --dry-run
```

---

## Быстрый старт: команды восстановления

```bash
# Полное восстановление из бэкапа:
sudo bash restore.sh <ФАЙЛ_БЕКАПА>

# Посмотреть содержимое бэкапа:
sudo bash restore.sh --list <ФАЙЛ_БЕКАПА>

# Восстановить только конкретные компоненты:
sudo bash restore.sh --component env       <ФАЙЛ>  # только .env
sudo bash restore.sh --component wireguard <ФАЙЛ>  # только WG ключи
sudo bash restore.sh --component database  <ФАЙЛ>  # только SQLite
sudo bash restore.sh --component nftables  <ФАЙЛ>  # только nftables

# Откат deploy:
sudo bash deploy.sh --rollback

# Миграция VPS:
sudo bash restore.sh <ФАЙЛ> --migrate-vps <IP>

# Листинг снимков deploy:
ls /opt/vpn/.deploy-snapshot/
sudo bash deploy.sh --status
```

---

## RTO / RPO (целевые показатели)

| Сценарий | RTO (цель восстановления) | RPO (потеря данных) |
|----------|--------------------------|---------------------|
| Повреждены конфиги, ОС цела | 10–15 минут | 0 (данные из БД не теряются) |
| Новое железо + бэкап | 40–60 минут | ≤24 часов (бэкап раз в сутки) |
| Смена VPS | 15–30 минут | 0 (ключи сохраняются) |
| Утеря обоих серверов | 60–90 минут | ≤24 часов |
| Компрометация ключей | 5 минут (/rotate-keys) | 0 |

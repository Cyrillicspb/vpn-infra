# Установка VPN-инфраструктуры

## Содержание

1. [Предварительные требования](#1-предварительные-требования)
2. [Подготовка оборудования](#2-подготовка-оборудования)
3. [Подготовка VPS](#3-подготовка-vps)
4. [Подготовка Telegram-бота](#4-подготовка-telegram-бота)
5. [Запуск установки](#5-запуск-установки)
6. [Фазы установки](#6-фазы-установки)
7. [Настройка роутера](#7-настройка-роутера-port-forwarding)
8. [Подключение первого клиента](#8-подключение-первого-клиента)
9. [Проверка работы](#9-проверка-работы)
10. [Если что-то пошло не так](#10-если-что-то-пошло-не-так)

---

## 1. Предварительные требования

### Обязательно

- [ ] **Реальный (белый) IP** на роутере — не CGNAT.
  Проверьте: если IP на WAN-интерфейсе роутера начинается с `100.64.x.x` — это CGNAT.
  Решение: попросите провайдера выдать белый IP (обычно бесплатно или ~150 руб/мес).
- [ ] **VPS** с KVM-виртуализацией, Ubuntu 22.04/24.04 — подробнее в [REQUIREMENTS.md](REQUIREMENTS.md)
- [ ] **Telegram-аккаунт** и бот через [@BotFather](https://t.me/BotFather)
- [ ] **Ubuntu Server 24.04.2 LTS** на домашнем сервере (чистая установка, не Desktop)
- [ ] **SSH-доступ** к домашнему серверу с вашего компьютера

### Опционально (расширенные функции)

- [ ] **Cloudflare аккаунт** (бесплатный) — CDN-стек, максимальная устойчивость к блокировкам
- [ ] **Домен** — для красивых адресов и DDNS
- [ ] **DuckDNS / No-IP** (бесплатно) — при динамическом IP

### Чего НЕ нужно

- Знания программирования или Linux
- Платные инструменты (кроме VPS ~$5–7/мес)
- Ручная правка конфигов после установки

---

## 2. Подготовка оборудования

### Домашний сервер

1. **Установите Ubuntu Server 24.04.2 LTS** (не Desktop, не 22.04)
   - Скачайте: https://ubuntu.com/download/server
   - При установке: имя пользователя `sysadmin`, включите **OpenSSH Server**
   - Назначьте статический LAN IP через роутер (DHCP-reservation) или в `/etc/netplan/`

2. Убедитесь что сервер **подключён по Ethernet** (Wi-Fi нестабилен для сервера)

3. Проверьте SSH-доступ со своего компьютера:
   ```bash
   ssh sysadmin@<LAN_IP_сервера>
   ```

4. Скачайте репозиторий:
   ```bash
   sudo apt install -y git
   sudo git clone https://github.com/Cyrillicspb/vpn-infra.git /opt/vpn
   cd /opt/vpn
   ```

### VPS

VPS должен работать и быть доступен по SSH root. Проверьте:
- Виртуализация: KVM (не OpenVZ/LXC — они не поддерживают WireGuard)
- ОС: Ubuntu 22.04 или 24.04
- Открытые порты: 22/tcp (SSH), 443/tcp, 8444/tcp, 443/udp

---

## 3. Подготовка VPS

На вашем **локальном компьютере** (или прямо на домашнем сервере):

```bash
# Сгенерируйте SSH-ключ для автоматического доступа к VPS
ssh-keygen -t ed25519 -f ~/.ssh/vpn_vps -C "vpn-infra-vps"

# Скопируйте ключ на VPS (введите пароль root VPS)
ssh-copy-id -i ~/.ssh/vpn_vps.pub root@<IP_ВАШЕГО_VPS>

# Проверьте подключение
ssh -i ~/.ssh/vpn_vps root@<IP_ВАШЕГО_VPS>
```

> **Если SSH порт 22 заблокирован у провайдера VPS:** setup.sh выведет инструкцию по
> настройке через веб-консоль VPS-провайдера.

Запомните:
- IP адрес VPS
- Пароль root VPS (понадобится один раз при установке)

---

## 4. Подготовка Telegram-бота

1. Откройте Telegram, напишите [@BotFather](https://t.me/BotFather)
2. Отправьте `/newbot`
3. Введите отображаемое имя (например: `Мой VPN`)
4. Введите username (например: `myhome_vpn_bot`) — только латиница, заканчивается на `_bot`
5. Скопируйте токен вида `1234567890:ABCdefGHIjklmno...`

**Узнайте свой chat_id:**
- Напишите [@userinfobot](https://t.me/userinfobot) любое сообщение
- Скопируйте числовой ID (например: `123456789`)

**Начните диалог с ботом:**
Найдите вашего бота в Telegram и нажмите Start — иначе бот не сможет отправить первое сообщение.

---

## 5. Запуск установки

На **домашнем сервере**, под пользователем `sysadmin`:

```bash
cd /opt/vpn
sudo bash setup.sh
```

Установщик задаст вопросы и сделает всё автоматически. Всё что нужно — отвечать на вопросы и ждать.

### Параметры которые спросит установщик

| Вопрос | Пример ответа | Где взять |
|--------|---------------|-----------|
| IP адрес VPS | `185.12.34.56` | Письмо от VPS-хостинга |
| Пароль root VPS | `SuperSecret123` | Письмо от VPS-хостинга |
| Telegram Bot Token | `1234567890:ABCdef...` | @BotFather |
| Ваш Telegram chat_id | `123456789` | @userinfobot |
| Внешний IP или DDNS | `185.xx.xx.xx` | `curl icanhazip.com` |
| Cloudflare token | (Enter — пропустить) | Cloudflare Dashboard |

> **Идемпотентность:** если установка прервётся — запустите `sudo bash setup.sh` снова.
> Состояние хранится в `/opt/vpn/.setup-state`. Установщик продолжит с прерванного шага.

---

## 6. Фазы установки (~25–45 минут)

### Фаза 0 — Подготовка (шаги 1–8)
- Проверка ОС (Ubuntu 24.04), root-прав, сети
- Автоопределение: LAN IP, gateway, CGNAT, двойной NAT
- Ввод параметров: VPS, Telegram, DDNS/Cloudflare
- **Автогенерация всех секретов:** WG/AWG ключи, UUID, REALITY-ключи, токены, пароли
- Bootstrap VPS: создание `sysadmin`, отключение root SSH, настройка `sudo`

### Фаза 1 — Домашний сервер (шаги 9–28)
- Установка пакетов: Docker CE, AmneziaWG DKMS, Hysteria2, tun2socks
- Генерация REALITY x25519 ключей через Docker
- nftables правила: kill switch, fwmark-маршрутизация, NAT
- WireGuard интерфейсы: `wg0` (AWG 10.177.1.0/24) + `wg1` (WG 10.177.3.0/24)
- dnsmasq: split DNS, nftset= для динамической блокировки
- systemd units: все 9 сервисов в правильном порядке
- Python venv для watchdog, first start
- fail2ban, logrotate, unattended-upgrades, cron

### Фаза 2 — VPS (шаги 29–39)
- Docker CE на VPS
- nftables rate-limiting порта 443
- mTLS CA (4096 bit, 10 лет) + клиентский сертификат (2 года)
- .env с секретами → VPS
- Запуск: 3x-ui, nginx, cloudflared, prometheus, alertmanager, grafana
- xray-setup.sh: 4 inbound (REALITY, gRPC, WebSocket CDN, Hysteria2)
- git-зеркало репозитория (cron sync с GitHub)

### Фаза 3 — Связка (шаги 40–44)
- Tier-2 WireGuard туннель (10.177.2.0/30): домашний ↔ VPS
- Xray клиентские конфиги (REALITY + gRPC)
- Hysteria2 клиентский конфиг
- Запуск всех 4 стеков, проверка подключения

### Фаза 4 — Smoke-тесты (шаги 45–50)
Автоматические тесты подтверждают что всё работает:

| Тест | Что проверяет |
|------|---------------|
| test_dns | dnsmasq резолвит заблокированные домены через VPS |
| test_split | split tunneling: blocked → tun, unblocked → eth0 |
| test_tunnel | ping VPS через тун |
| test_watchdog | watchdog API /status отвечает |
| test_bot | Telegram-бот отвечает |
| test_kill_switch | заблокированный сайт DROP при упавшем tun |

### Фаза 5 — Ручные действия (шаг 51)
Установщик выведет точный список. Основное:
- **Настроить port forwarding** на роутере (UDP 51820 + 51821)
- Опционально: настроить Cloudflare Tunnel ingress в Dashboard

---

## 7. Настройка роутера (port forwarding)

Это единственный ручной шаг. Нужно пробросить 2 UDP-порта к вашему домашнему серверу:

| WAN-порт | Протокол | LAN-цель | Описание |
|----------|----------|----------|----------|
| 51820 | UDP | `<IP сервера>`:51820 | AmneziaWG |
| 51821 | UDP | `<IP сервера>`:51821 | WireGuard |

### Инструкции для популярных роутеров

**Keenetic:** Интернет → Переадресация → Добавить правило

**TP-Link (старые):** Forwarding → Virtual Servers

**TP-Link (новые):** Advanced → NAT Forwarding → Virtual Servers

**ASUS:** WAN → Virtual Server / Port Forwarding

**MikroTik:** IP → Firewall → NAT → `+` → dstnat, dst-port 51820/51821, action=dst-nat

**Zyxel Keenetic:** Сетевые правила → Переадресация

> **Проверка из интернета:** с мобильного (без Wi-Fi) запустите `/status` в боте.
> Или используйте [canyouseeme.org](https://canyouseeme.org) — проверить UDP 51820.

---

## 8. Подключение первого клиента

Напишите вашему боту `/start`.

Администратор (ваш chat_id из .env) регистрируется **автоматически без invite-кода**.

**Шаги FSM-регистрации:**
1. Введите имя устройства (например: `iPhone Кирилл`)
2. Выберите протокол: `AWG` (рекомендуется) или `WG`
3. Бот пришлёт `.conf` файл + QR-код (если AllowedIPs ≤ 50 записей)

**Предупреждение:** Бот напомнит что конфиг содержит приватный ключ — не пересылайте его другим.

### Установка WireGuard-клиента

| Платформа | Приложение | Ссылка |
|-----------|-----------|--------|
| Android (AWG) | AmneziaWG | [Play Store](https://play.google.com/store/apps/details?id=org.amnezia.awg) |
| iOS (AWG) | AmneziaWG | [App Store](https://apps.apple.com/app/amneziawg/id1600098470) |
| Android (WG) | WireGuard | [Play Store](https://play.google.com/store/apps/details?id=com.wireguard.android) |
| iOS (WG) | WireGuard | [App Store](https://apps.apple.com/app/wireguard/id1441195209) |
| Windows | WireGuard | [wireguard.com](https://www.wireguard.com/install/) |
| macOS | WireGuard | [App Store](https://apps.apple.com/app/wireguard/id1451685025) |
| Linux | wireguard-tools | `sudo apt install wireguard` |

---

## 9. Проверка работы

```
/status    — статус туннеля и активного стека
/ip        — ваш внешний IP (должен быть IP вашего VPS)
/speed     — speedtest через VPN
/check youtube.com   — проверить доступность
```

На клиентском устройстве:
- Зайдите на [2ip.ru](https://2ip.ru) — должен показать IP вашего VPS
- Откройте заблокированный сайт — должен работать
- Откройте незаблокированный сайт — должен открываться быстро (напрямую, без VPN)

---

## 10. Если что-то пошло не так

| Симптом | Диагностика |
|---------|-------------|
| Установка зависла | `sudo bash setup.sh` — продолжит с прерванного шага |
| Smoke-тест провалился | `sudo bash tests/smoke/test_dns.sh` — подробный вывод |
| Бот не отвечает | `docker logs telegram-bot --tail 100` |
| VPN подключён, сайты не открываются | `/diagnose ИмяУстройства` в боте |
| Заблокированные сайты всё равно не работают | `/check youtube.com` + `sudo bash tests/smoke/test_split.sh` |
| Медленная скорость | `/speed` — сравнить стеки |

Подробное руководство: **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**

# vpn-infra — Двухуровневая VPN-инфраструктура для обхода РКН/ТСПУ

Самохостинговое решение для стабильного доступа к заблокированным ресурсам. Трафик к заблокированным сайтам идёт через VPS за рубежом, незаблокированный трафик — напрямую. Управление через Telegram-бота.

---

## Ключевые возможности

- **Адаптивный failover**: 4 стека защиты, автоматическое переключение при блокировке
- **Split tunneling**: только заблокированный трафик идёт через VPS, остальное — напрямую
- **Без ручной настройки**: установка одним скриптом за 25–45 минут, 57 шагов
- **Telegram-бот**: управление инфраструктурой, добавление клиентов, алерты, мониторинг
- **Несколько клиентов**: поддержка AmneziaWG (обфускация) и WireGuard
- **Автообновление баз**: ежедневное обновление списков заблокированных адресов РКН в 03:00
- **Мониторинг**: Prometheus + Grafana на VPS, алерты в Telegram
- **Kill switch**: при падении туннеля трафик к заблокированным сайтам блокируется, не утекает через незащищённый канал
- **Идемпотентность**: скрипт установки можно запустить повторно, он продолжит с места остановки

---

## Схема архитектуры

```
Клиенты (телефон, ноутбук, роутер)
  │  AmneziaWG UDP 51820  /  WireGuard UDP 51821
  ▼
Роутер (реальный IP, port forward 51820 + 51821)
  ▼
Домашний сервер — Ubuntu 24.04
  │
  │  На хосте (systemd):
  │    amneziawg  wg0  10.177.1.0/24
  │    wireguard  wg1  10.177.3.0/24
  │    nftables   kill switch + fwmark routing
  │    dnsmasq    split DNS + nftset blocked_dynamic
  │    watchdog   FastAPI :8080, управление стеками
  │    wg-tier2   10.177.2.0/30 (туннель к VPS)
  │
  │  Docker (172.20.0.0/24):
  │    telegram-bot      управление ботом
  │    xray-client       SOCKS5 :1080 (стек reality)
  │    xray-client-2     SOCKS5 :1081 (стек reality-grpc)
  │    cloudflared       CDN стек
  │    socket-proxy      доступ к Docker API
  │    node-exporter     метрики для Prometheus
  │
  │  Заблокированный трафик (nftables fwmark routing):
  │    blocked_static + blocked_dynamic → fwmark 0x1 → table marked → tun → VPS
  │    остальное → table vpn → eth0 (прямой интернет)
  │
  ├──[стек 1: CDN]         VLESS+WebSocket → Cloudflare Worker → VPS:8080
  ├──[стек 2: reality-grpc] VLESS+XHTTP, SNI cdn.jsdelivr.net → VPS:2083
  ├──[стек 3: reality]      VLESS+XHTTP, SNI microsoft.com    → VPS:2087
  └──[стек 4: hysteria2]    QUIC+Salamander UDP 443
  ▼
VPS — Ubuntu 22.04/24.04 (за рубежом)
  │  3x-ui (Xray: VLESS-XHTTP-jsdelivr :2083, VLESS-XHTTP-microsoft :2087)
  │  nginx (mTLS :8443), cloudflared
  │  prometheus, grafana, alertmanager, node-exporter
  │  hysteria2 (standalone)
  ▼
Интернет (заблокированные ресурсы)
```

---

## Требования

### Оборудование

| Компонент | Минимум | Рекомендуется |
|---|---|---|
| **Домашний сервер** | 4 ГБ RAM, 64 ГБ SSD, Ethernet, Ubuntu 24.04 | 8 ГБ RAM, 128 ГБ SSD |
| **Роутер** | Реальный (белый) IP, поддержка port forwarding | — |
| **VPS** | KVM, 1 vCPU, 1 ГБ RAM, Ubuntu 22.04/24.04 | 2 vCPU, 2 ГБ RAM |
| **Интернет дома** | Upload ≥ 5 Мбит/с | Upload ≥ 20 Мбит/с |

> **Важно**: роутер должен иметь реальный (белый) IP, не CGNAT. Домашний сервер должен быть подключён к роутеру по Ethernet.
> Формула максимального числа клиентов: `upload_Мбит ÷ 5`.

### Аккаунты и доступы

| Что | Зачем | Обязательно |
|---|---|---|
| Telegram-аккаунт | Управление ботом, алерты | Да |
| VPS с SSH-доступом (root) | Второй уровень туннеля | Да |
| Cloudflare аккаунт (бесплатный) | CDN-стек (наиболее устойчивый к блокировкам) | Нет |

---

## Быстрый старт

```bash
# На домашнем сервере (Ubuntu 24.04, под root или через sudo)
curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/setup.sh -o setup.sh
sudo bash setup.sh
```

Скрипт запросит данные (VPS IP, Telegram токен и т.д.) на фазе 0, затем выполнит установку автоматически.

**Windows**: запустите `installers/windows/install.bat` — он подключится к серверу по SSH и запустит скрипт.

**macOS**: запустите `installers/macos/install.command` — аналогично.

---

## Что подготовить заранее

Скрипт запрашивает эти данные интерактивно на фазе 0 (~5 минут). Подготовьте заранее:

| Данные | Где взять |
|---|---|
| **IP-адрес VPS** | В панели управления VPS-провайдера |
| **SSH-порт VPS** | Обычно 22; уточните в панели провайдера |
| **Root-пароль VPS** | Там же, при заказе сервера |
| **Telegram Bot Token** | [@BotFather](https://t.me/BotFather) → `/newbot` |
| **Telegram Chat ID** | [@userinfobot](https://t.me/userinfobot) или [@getmyid_bot](https://t.me/getmyid_bot) |
| **URL Cloudflare Worker** | Опционально; см. [docs/INSTALL.md](docs/INSTALL.md#cdn-стек-cloudflare) |

Все криптографические ключи, пароли и UUID генерируются автоматически.

---

## Ручные шаги после установки

После завершения скрипта потребуется сделать два ручных шага:

### 1. Проброс портов на роутере

Настройте port forwarding на IP домашнего сервера:

| Протокол | Внешний порт | Внутренний порт |
|---|---|---|
| UDP | 51820 | 51820 |
| UDP | 51821 | 51821 |

Инструкции для конкретных роутеров: [docs/INSTALL.md → Port Forwarding](docs/INSTALL.md#обязательный-ручной-шаг-port-forwarding)

### 2. Первый запуск Telegram-бота

Напишите боту `/start` — он автоматически зарегистрирует вас как администратора и покажет меню управления.

---

## Документация

| Документ | Описание |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Подробная инструкция по установке |
| [docs/COMMANDS.md](docs/COMMANDS.md) | Все команды Telegram-бота |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура системы |
| [docs/HARDWARE.md](docs/HARDWARE.md) | Выбор оборудования и VPS-провайдера |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Решение типичных проблем |
| [docs/UPDATE.md](docs/UPDATE.md) | Обновление системы |
| [docs/DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) | Восстановление после сбоя |
| [docs/SECURITY.md](docs/SECURITY.md) | Безопасность |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Приватность |
| [docs/FAQ.md](docs/FAQ.md) | Частые вопросы |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Детальные требования к оборудованию |

---

## Протоколы (4 стека)

Стеки перечислены от наиболее к наименее устойчивому. Watchdog переключает автоматически при деградации.

| # | Стек | Протокол | Порт VPS | Устойчивость | Скорость |
|---|---|---|---|---|---|
| 1 | **CDN** | VLESS+WebSocket через Cloudflare Worker | 8080 TCP | Максимальная | Умеренная |
| 2 | **reality-grpc** | VLESS+XHTTP (splithttp), SNI cdn.jsdelivr.net | 2083 TCP | Очень высокая | Высокая |
| 3 | **reality** | VLESS+XHTTP (splithttp), SNI microsoft.com | 2087 TCP | Высокая | Высокая |
| 4 | **hysteria2** | QUIC+Salamander obfuscation | 443 UDP | Средняя | Максимальная |

> CDN-стек требует предварительной настройки Cloudflare Worker (опционально, но рекомендуется для максимальной устойчивости).
> Без него система работает на стеках 2–4.

---

## Лицензия

MIT

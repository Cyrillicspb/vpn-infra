# Установка

## Требования

- Ubuntu Server 24.04 LTS
- VPS с KVM (2 vCPU, 2GB RAM)
- Telegram Bot Token
- Реальный (белый) IP или DDNS

## Быстрый старт

```bash
sudo bash setup.sh
```

## Пошаговая установка

### 1. Подготовка сервера

Установите Ubuntu Server 24.04 на домашний сервер.

### 2. Настройка роутера

Откройте порты:
- UDP 51820 → домашний сервер (AmneziaWG)
- UDP 51821 → домашний сервер (WireGuard)

### 3. Получите Telegram Bot Token

1. Напишите @BotFather
2. `/newbot` → введите имя и username
3. Скопируйте токен

### 4. Запуск установки

```bash
curl -fsSL https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/main/setup.sh | sudo bash
```

Скрипт спросит:
- IP VPS
- Telegram токен и chat_id
- Опционально: DDNS, Cloudflare

### 5. Ручные шаги после установки

- Настроить port forwarding на роутере
- Открыть 3x-ui и настроить Xray инбаунды
- Добавить первого клиента через бота: /start

## Установщики для Windows/macOS

Windows: `installers/windows/install.bat`
macOS: `installers/macos/install.command`

Оба подключаются к серверу по SSH и запускают setup.sh.

## Восстановление из бэкапа

```bash
sudo bash restore.sh backup.tar.gz.gpg
```

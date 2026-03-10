@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
title VPN Infrastructure — Установщик

cls
echo.
echo  ==========================================
echo    VPN Infrastructure -- Установка
echo  ==========================================
echo.
echo  Этот скрипт подключится к вашему домашнему
echo  серверу и запустит установку VPN.
echo.
echo  Требования:
echo   - Windows 10/11 с OpenSSH (встроен)
echo   - Домашний сервер: Ubuntu Server 24.04
echo   - Сервер подключён к роутеру по Ethernet
echo.

REM Проверка OpenSSH
where ssh >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ОШИБКА] SSH не найден.
    echo.
    echo  Установите OpenSSH Client:
    echo  Параметры -> Приложения -> Дополнительные функции
    echo  -> Добавить компонент -> Клиент OpenSSH
    echo.
    pause
    exit /b 1
)
echo  [OK] SSH найден.
echo.

REM Ввод данных сервера
:input_ip
set /p SERVER_IP=IP-адрес домашнего сервера (например 192.168.1.100):
if "!SERVER_IP!"=="" (
    echo  Введите IP-адрес.
    goto input_ip
)

set /p SERVER_USER=Пользователь SSH [sysadmin]:
if "!SERVER_USER!"=="" set SERVER_USER=sysadmin

set /p SSH_PORT=SSH порт [22]:
if "!SSH_PORT!"=="" set SSH_PORT=22

echo.

REM Проверка/генерация SSH ключа
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key
if not exist "!SSH_KEY!" (
    echo  Создание SSH ключа...
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C "vpn-deploy" > nul 2>&1
    if !errorlevel! equ 0 (
        echo  [OK] SSH ключ создан: !SSH_KEY!
    ) else (
        echo  [ОШИБКА] Не удалось создать SSH ключ.
        pause
        exit /b 1
    )
)

echo.
echo  Копирование SSH ключа на сервер...
echo  (Введите пароль от !SERVER_USER!@!SERVER_IP! при запросе)
echo.

REM Копирование публичного ключа
set /p PUBKEY=< "!SSH_KEY!.pub"
ssh -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p ~/.ssh && echo '!PUBKEY!' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo SSH_KEY_OK"
if !errorlevel! neq 0 (
    echo.
    echo  [ПРЕДУПРЕЖДЕНИЕ] Автоматическое копирование ключа не удалось.
    echo.
    echo  Добавьте ключ вручную на сервере:
    echo  1. Подключитесь: ssh !SERVER_USER!@!SERVER_IP! -p !SSH_PORT!
    echo  2. Выполните:
    type "!SSH_KEY!.pub"
    echo.
    echo  Скопируйте вывод выше и добавьте в ~/.ssh/authorized_keys на сервере.
    echo.
    pause
)

echo.
echo  Проверка подключения к серверу...
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=10" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "echo OK_CONNECTION"
if !errorlevel! neq 0 (
    echo.
    echo  [ОШИБКА] Не удалось подключиться к серверу.
    echo.
    echo  Проверьте:
    echo   1. Сервер включён и подключён к сети
    echo   2. IP: !SERVER_IP!, пользователь: !SERVER_USER!, порт: !SSH_PORT!
    echo   3. OpenSSH-server установлен на Ubuntu-сервере
    echo.
    pause
    exit /b 1
)

echo.
echo  ==========================================
echo   Сервер доступен. Готово к установке!
echo  ==========================================
echo.
echo  Подключение к: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!
echo.
echo  ВНИМАНИЕ: Установка займёт 15-30 минут.
echo  Не закрывайте это окно!
echo.
set /p CONFIRM=Начать установку? (y/N):
if /i "!CONFIRM!" neq "y" (
    echo Отменено.
    pause
    exit /b 0
)

echo.
echo  Запуск установки на сервере...
echo  ==========================================
echo.

REM Запуск setup.sh
ssh -i "!SSH_KEY!" ^
    -o "StrictHostKeyChecking=accept-new" ^
    -o "ServerAliveInterval=30" ^
    -o "ServerAliveCountMax=10" ^
    -p !SSH_PORT! ^
    -t !SERVER_USER!@!SERVER_IP! ^
    "bash -lc \"set -e; echo '=== Скачивание setup.sh ==='; if curl -sf --max-time 30 https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/setup.sh -o /tmp/vpn-setup.sh 2>/dev/null; then echo 'OK: скачано с GitHub'; else echo 'Ошибка: GitHub недоступен'; exit 1; fi; chmod +x /tmp/vpn-setup.sh; echo '=== Запуск setup.sh ==='; sudo bash /tmp/vpn-setup.sh\""

set RESULT=!errorlevel!
echo.
echo  ==========================================

if !RESULT! equ 0 (
    echo.
    echo  [OK] Установка завершена успешно!
    echo.
    echo  Следующие шаги:
    echo   1. Настройте Port Forwarding на роутере:
    echo      UDP 51820 --^> !SERVER_IP!:51820 (AmneziaWG)
    echo      UDP 51821 --^> !SERVER_IP!:51821 (WireGuard)
    echo   2. Откройте Telegram и напишите боту /start
    echo   3. Получите конфиг и импортируйте в WireGuard
) else (
    echo.
    echo  [ОШИБКА] Установка завершилась с ошибкой (код !RESULT!)
    echo.
    echo  Для диагностики:
    echo   ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
    echo   cat /tmp/vpn-setup.log
)

echo.
pause
endlocal

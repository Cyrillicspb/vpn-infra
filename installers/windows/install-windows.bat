@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title VPN Infrastructure Setup

cls
echo ==========================================
echo    VPN Infrastructure -- Установка
echo    Windows -- Ubuntu Server
echo ==========================================
echo.

:: ── [1/5] SSH ────────────────────────────────────────────────────────────────
echo [1/5] Проверка SSH...
where ssh >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] SSH не найден
    echo     Включите: Параметры - Приложения - Дополнительные возможности - Клиент OpenSSH
    echo.
    pause
    exit /b 1
)
where scp >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] scp не найден
    pause
    exit /b 1
)
echo   [OK] SSH доступен

:: ── [2/5] SSH-ключ ───────────────────────────────────────────────────────────
echo.
echo [2/5] SSH-ключ...
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key

if exist "!SSH_KEY!" (
    echo   [OK] Ключ существует: !SSH_KEY!
) else (
    echo   --> Создание SSH ключа...
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C "vpn-deploy" -q
    if !errorlevel! neq 0 (
        echo   [FAIL] Не удалось создать SSH ключ
        pause
        exit /b 1
    )
    echo   [OK] Ключ создан: !SSH_KEY!
)

:: ── Данные сервера ────────────────────────────────────────────────────────────
echo.
echo Введите адрес вашего Ubuntu-сервера:
echo.

:input_ip
set /p SERVER_IP=  IP-адрес сервера:
if "!SERVER_IP!"=="" goto input_ip
echo.

set SERVER_USER=
set /p SERVER_USER=  Пользователь SSH [sysadmin]:
if "!SERVER_USER!"=="" set SERVER_USER=sysadmin

set SSH_PORT=
set /p SSH_PORT=  SSH порт [22]:
if "!SSH_PORT!"=="" set SSH_PORT=22

echo.

:: Очистка старого known_hosts (на случай переустановки сервера)
ssh-keygen -R [!SERVER_IP!]:!SSH_PORT! >nul 2>&1
ssh-keygen -R !SERVER_IP! >nul 2>&1

:: ── Установка ключа на сервере ───────────────────────────────────────────────

:: Проверяем, работает ли ключ уже
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" -o "BatchMode=yes" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! equ 0 (
    echo   [OK] Ключ уже установлен для !SERVER_USER!
    goto connected
)

:: Ключ не установлен — копируем через пароль
echo   --> Копирование SSH ключа на сервер...
echo   Введите пароль для !SERVER_USER!@!SERVER_IP! (один раз):
type "!SSH_KEY!.pub" | ssh -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
if !errorlevel! neq 0 (
    echo.
    echo   [FAIL] Не удалось скопировать ключ
    echo     Проверьте: пароль, сеть, SSH включён на сервере, порт !SSH_PORT!
    echo.
    pause
    exit /b 1
)
echo   [OK] SSH ключ установлен

:: Проверка входа по ключу
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "BatchMode=yes" -o "ConnectTimeout=10" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] Вход по ключу не работает
    pause
    exit /b 1
)
echo   [OK] Вход по ключу работает

:connected

:: ── [3/5] Подготовка сервера ─────────────────────────────────────────────────
echo.
echo [3/5] Подготовка сервера...

:: Проверка подключения
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=10" -o "BatchMode=yes" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "hostname" >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] Подключение не удалось
    echo     Проверьте: IP !SERVER_IP!, пользователь !SERVER_USER!, порт !SSH_PORT!
    pause
    exit /b 1
)
echo   [OK] Сервер доступен: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!

:: Настройка sudo без пароля (один раз — потребуется пароль пользователя)
echo.
echo   Настройка прав (потребуется пароль !SERVER_USER!):
ssh -t -p !SSH_PORT! -i "!SSH_KEY!" !SERVER_USER!@!SERVER_IP! "sudo bash -c 'echo !SERVER_USER! ALL=(ALL) NOPASSWD:ALL > /etc/sudoers.d/vpn-installer && chmod 440 /etc/sudoers.d/vpn-installer'"
if !errorlevel! neq 0 (
    echo   [WARN] Не удалось настроить sudo -- возможны запросы пароля
) else (
    echo   [OK] sudo настроен
)

:: ── Подтверждение ─────────────────────────────────────────────────────────────
echo.
echo   [WARN] Установка займёт 20-40 минут. Не закрывайте окно.
echo.
set /p CONFIRM=  Начать установку? [y/N]:
if /i "!CONFIRM!" neq "y" (
    echo   Отменено.
    pause
    exit /b 0
)

:: ── [4/5] Загрузка репозитория ───────────────────────────────────────────────
echo.
echo [4/5] Загрузка репозитория...

set REPO_ROOT=%~dp0..\..
set SETUP_PATH=/opt/vpn/setup.sh
set UPLOAD_OK=0

if not exist "!REPO_ROOT!\setup.sh" goto download_release
if not exist "!REPO_ROOT!\install-home.sh" goto download_release
if not exist "!REPO_ROOT!\home" goto download_release

echo   --> Упаковка локального репозитория...
tar -czf "%TEMP%\vpn-infra.tar.gz" --exclude=".git" --exclude="*.pyc" --exclude="__pycache__" --exclude="*/venv/*" --exclude="node_modules" --exclude="*.log" --exclude=".env" -C "!REPO_ROOT!" . >nul 2>&1
if !errorlevel! neq 0 goto download_release

echo   --> Создание директории на сервере...
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "sudo mkdir -p /opt/vpn && sudo chown !SERVER_USER!:!SERVER_USER! /opt/vpn"

echo   --> Загрузка архива на сервер...
scp -i "!SSH_KEY!" -P !SSH_PORT! -o "StrictHostKeyChecking=accept-new" "%TEMP%\vpn-infra.tar.gz" !SERVER_USER!@!SERVER_IP!:/tmp/vpn-infra.tar.gz
if !errorlevel! neq 0 goto download_release

ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner; rm /tmp/vpn-infra.tar.gz"
if !errorlevel! neq 0 goto download_release

del "%TEMP%\vpn-infra.tar.gz" >nul 2>&1
set UPLOAD_OK=1
echo   [OK] Репозиторий загружен из локальной копии
goto check_tui

:download_release
echo   --> Скачивание последнего релиза с GitHub...
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ServerAliveInterval=30" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "curl -fsSL --max-time 120 -L https://github.com/Cyrillicspb/vpn-infra/releases/latest/download/vpn-infra.tar.gz -o /tmp/vpn-infra.tar.gz"
if !errorlevel! neq 0 (
    echo   [FAIL] Не удалось скачать релиз
    echo     Проверьте интернет или загрузите вручную.
    pause
    exit /b 1
)
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "sudo mkdir -p /opt/vpn && sudo tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner; rm -f /tmp/vpn-infra.tar.gz"
if !errorlevel! neq 0 (
    echo   [FAIL] Не удалось распаковать релиз
    pause
    exit /b 1
)
set UPLOAD_OK=1
echo   [OK] Релиз скачан с GitHub

:: ── [5/5] Установка ───────────────────────────────────────────────────────────
:check_tui
echo.
echo [5/5] Установка...

set USE_TUI=0
set PY_MAJ=0
set PY_MIN=0
set PY_VER=

:: Проверяем Python 3.10+
echo   --> Проверка Python 3.10+ на сервере...
for /f "tokens=2" %%V in ('ssh -n -p !SSH_PORT! -i "!SSH_KEY!" -o "BatchMode=yes" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" !SERVER_USER!@!SERVER_IP! "python3 --version 2>&1" 2^>nul') do set PY_VER=%%V
if not defined PY_VER set PY_VER=0.0.0
for /f "tokens=1,2 delims=." %%A in ("!PY_VER!") do (set PY_MAJ=%%A & set PY_MIN=%%B)
set /a PY_CMP=PY_MAJ*100+PY_MIN

if !PY_CMP! GEQ 310 (
    :: Проверяем наличие installer.py
    ssh -n -p !SSH_PORT! -i "!SSH_KEY!" -o "BatchMode=yes" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" !SERVER_USER!@!SERVER_IP! "test -f /opt/vpn/installers/gui/installer.py" >nul 2>&1
    if !errorlevel! equ 0 (
        :: Устанавливаем textual если нет
        echo   --> Установка textual на сервере...
        ssh -p !SSH_PORT! -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "BatchMode=yes" !SERVER_USER!@!SERVER_IP! "sudo pip3 install textual --break-system-packages --quiet" >nul 2>&1
        :: Проверяем что textual доступен
        ssh -n -p !SSH_PORT! -i "!SSH_KEY!" -o "BatchMode=yes" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" !SERVER_USER!@!SERVER_IP! "python3 -c 'import textual'" >nul 2>&1
        if !errorlevel! equ 0 (
            set USE_TUI=1
            echo   [OK] Python !PY_MAJ!.!PY_MIN! -- TUI-установщик готов
        ) else (
            echo   [WARN] textual недоступен -- консольный режим
        )
    ) else (
        echo   [WARN] installer.py не найден -- консольный режим
    )
) else (
    echo   [WARN] Python !PY_MAJ!.!PY_MIN! ^< 3.10 -- консольный режим
)

echo.

:: ── Запуск TUI ───────────────────────────────────────────────────────────────
if "!USE_TUI!"=="1" (
    echo [>>] Запуск TUI-установщика...
    echo ==========================================
    echo.
    ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! "cd /opt/vpn && sudo python3 installers/gui/installer.py"
    set TUI_RC=!errorlevel!
    echo.
    if !TUI_RC! equ 0 (
        echo ==========================================
        echo   [OK] Готово!
        echo.
        echo   Если установка завершена -- следующие шаги:
        echo     1. Port Forwarding: UDP 51820+51821 --> !SERVER_IP!
        echo     2. Telegram: напишите /start вашему боту
        echo     3. /adddevice -- получите конфиг WireGuard/AWG
        echo.
        pause
        exit /b 0
    )
    echo   [WARN] TUI завершился с кодом !TUI_RC! -- откат на консольный режим...
    echo.
)

:: ── Fallback: tmux + setup.sh ─────────────────────────────────────────────────
echo [>>] Запуск setup.sh в tmux...
echo ==========================================
echo.

ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! "tmux new-session -A -s vpn-install 'sudo bash !SETUP_PATH!'"
set RESULT=!errorlevel!

echo.
echo ==========================================

if !RESULT! equ 0 (
    echo   [OK] Установка завершена успешно!
    echo.
    echo   Следующие шаги:
    echo     1. Port Forwarding на роутере:
    echo        UDP 51820 --> !SERVER_IP!:51820  ^(AmneziaWG^)
    echo        UDP 51821 --> !SERVER_IP!:51821  ^(WireGuard^)
    echo     2. Telegram: напишите /start вашему боту
    echo     3. /adddevice -- получите конфиг
    echo.
    echo   SSH доступ:
    echo     ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
) else (
    echo   [FAIL] Установка завершилась с ошибкой ^(код !RESULT!^)
    echo.
    echo   Диагностика:
    echo     ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
    echo     cat /tmp/vpn-setup.log
    echo.
    echo   Повторный запуск безопасен -- выполненные шаги пропустятся.
)

echo.
pause
endlocal

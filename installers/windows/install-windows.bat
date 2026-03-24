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
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" ^
    -o "BatchMode=yes" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! equ 0 (
    echo   [OK] Ключ уже установлен для !SERVER_USER!
    goto connected
)

:: Ключ не установлен — копируем через пароль
echo   --> Копирование SSH ключа на сервер...
echo   Введите пароль для !SERVER_USER!@!SERVER_IP! (один раз):
type "!SSH_KEY!.pub" | ssh -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! ^
    !SERVER_USER!@!SERVER_IP! ^
    "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
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
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "BatchMode=yes" ^
    -o "ConnectTimeout=10" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] Вход по ключу не работает
    pause
    exit /b 1
)
echo   [OK] Вход по ключу работает

:connected

:: ── [3/5] Проверка подключения ───────────────────────────────────────────────
echo.
echo [3/5] Проверка подключения...
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=10" ^
    -o "BatchMode=yes" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! ^
    "hostname && (lsb_release -d 2>/dev/null | cut -f2 || grep PRETTY /etc/os-release | cut -d= -f2 | tr -d '\"' || echo Ubuntu)" ^
    >nul 2>&1
if !errorlevel! neq 0 (
    echo   [FAIL] Подключение не удалось
    echo     Проверьте: IP !SERVER_IP!, пользователь !SERVER_USER!, порт !SSH_PORT!
    pause
    exit /b 1
)
echo   [OK] Подключено: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!

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
tar -czf "%TEMP%\vpn-infra.tar.gz" ^
    --exclude=".git" --exclude="*.pyc" --exclude="__pycache__" ^
    --exclude="*/venv/*" --exclude="node_modules" ^
    --exclude="*.log" --exclude=".env" ^
    -C "!REPO_ROOT!" . >nul 2>&1
if !errorlevel! neq 0 goto download_release

echo   --> Загрузка архива на сервер...
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! ^
    !SERVER_USER!@!SERVER_IP! ^
    "sudo mkdir -p /opt/vpn && sudo chown !SERVER_USER!:!SERVER_USER! /opt/vpn"
scp -i "!SSH_KEY!" -P !SSH_PORT! -o "StrictHostKeyChecking=accept-new" ^
    "%TEMP%\vpn-infra.tar.gz" !SERVER_USER!@!SERVER_IP!:/tmp/vpn-infra.tar.gz
if !errorlevel! neq 0 goto download_release

ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! ^
    !SERVER_USER!@!SERVER_IP! ^
    "tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner 2>/dev/null; rm /tmp/vpn-infra.tar.gz"
if !errorlevel! neq 0 goto download_release

del "%TEMP%\vpn-infra.tar.gz" >nul 2>&1
set UPLOAD_OK=1
echo   [OK] Репозиторий загружен из локальной копии
goto check_tui

:download_release
echo   --> Скачивание последнего релиза с GitHub...
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
    -o "ServerAliveInterval=30" -p !SSH_PORT! ^
    !SERVER_USER!@!SERVER_IP! ^
    "RELEASE_URL=$(curl -sSfL --max-time 10 https://api.github.com/repos/Cyrillicspb/vpn-infra/releases/latest 2>/dev/null | python3 -c \"import sys,json; assets=[a for a in json.load(sys.stdin)['assets'] if a['name']=='vpn-infra.tar.gz']; print(assets[0]['browser_download_url'] if assets else '')\" 2>/dev/null) && [ -n \"$RELEASE_URL\" ] && curl -fsSL --max-time 120 \"$RELEASE_URL\" -o /tmp/vpn-infra.tar.gz && sudo mkdir -p /opt/vpn && sudo tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner 2>/dev/null; rm -f /tmp/vpn-infra.tar.gz && echo OK || (echo FAILED; exit 1)"
if !errorlevel! neq 0 (
    echo   [FAIL] Не удалось скачать релиз
    echo     Проверьте интернет или загрузите вручную.
    pause
    exit /b 1
)
set UPLOAD_OK=1
echo   [OK] Релиз скачан с GitHub

:: ── [5/5] Установка ───────────────────────────────────────────────────────────
:check_tui
echo.
echo [5/5] Установка...

set TUI_INSTALLER=/opt/vpn/installers/gui/installer.py
set USE_TUI=0
set PY_VER=0

:: Проверяем Python 3.10+
echo   --> Проверка Python 3.10+ на сервере...
for /f "tokens=* delims=" %%V in ('ssh -n -i "!SSH_KEY!" -o "BatchMode=yes" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "python3 -c \"import sys; v=sys.version_info; print(v.major*100+v.minor)\" 2>/dev/null || echo 0" 2^>nul') do set PY_VER=%%V
if not defined PY_VER set PY_VER=0
:: Strip spaces and take first 3 chars (guards against trailing CR/LF from SSH)
set PY_VER=!PY_VER: =!
set PY_VER=!PY_VER:~0,3!

set /a PY_CMP=!PY_VER!+0
if !PY_CMP! GEQ 310 (
    :: Проверяем наличие installer.py
    set FILE_CHK=no
    for /f "tokens=* delims=" %%F in ('ssh -n -i "!SSH_KEY!" -o "BatchMode=yes" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "test -f !TUI_INSTALLER! && echo yes || echo no" 2^>nul') do set FILE_CHK=%%F
    if "!FILE_CHK!"=="yes" (
        set USE_TUI=1
        set /a PY_MAJ=!PY_CMP!/100
        set /a PY_MIN=!PY_CMP!-!PY_MAJ!*100
        echo   [OK] Python !PY_MAJ!.!PY_MIN! -- TUI-установщик готов
    ) else (
        echo   [WARN] installer.py не найден -- консольный режим
    )
) else (
    set /a PY_MAJ=!PY_CMP!/100
    set /a PY_MIN=!PY_CMP!-!PY_MAJ!*100
    echo   [WARN] Python !PY_MAJ!.!PY_MIN! ^< 3.10 -- консольный режим
)

echo.

:: ── Запуск TUI ───────────────────────────────────────────────────────────────
if "!USE_TUI!"=="1" (
    echo [>>] Запуск TUI-установщика...
    echo ==========================================
    echo.
    ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
        -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" ^
        -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! ^
        "cd /opt/vpn && python3 installers/gui/installer.py"
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

ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
    -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" ^
    -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! ^
    "tmux new-session -A -s vpn-install 'sudo bash !SETUP_PATH!'"
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

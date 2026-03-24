@echo off
setlocal enabledelayedexpansion
title VPN Infrastructure Setup

:: Enable ANSI colors (Windows 10+)
reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul 2>&1

set ESC=
set RED=!ESC![91m
set GREEN=!ESC![92m
set YELLOW=!ESC![93m
set BLUE=!ESC![94m
set BOLD=!ESC![1m
set RESET=!ESC![0m

cls
echo !BOLD!╔══════════════════════════════════════════╗!RESET!
echo !BOLD!║    VPN Infrastructure — Установка        ║!RESET!
echo !BOLD!║    Windows → домашний сервер (Ubuntu)    ║!RESET!
echo !BOLD!╚══════════════════════════════════════════╝!RESET!
echo.

:: ── [1/5] SSH ────────────────────────────────────────────────────────────────
echo !BOLD![1/5] Проверка SSH...!RESET!
where ssh >nul 2>&1
if !errorlevel! neq 0 (
    echo   !RED!✗ SSH не найден!RESET!
    echo     Включите: Параметры - Приложения - Дополнительные возможности - Клиент OpenSSH
    echo.
    pause
    exit /b 1
)
where scp >nul 2>&1
if !errorlevel! neq 0 (
    echo   !RED!✗ scp не найден!RESET!
    pause
    exit /b 1
)
echo   !GREEN!✓ SSH доступен!RESET!

:: ── [2/5] SSH-ключ ───────────────────────────────────────────────────────────
echo.
echo !BOLD![2/5] SSH-ключ...!RESET!
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key

if exist "!SSH_KEY!" (
    echo   !GREEN!✓ Ключ существует: !SSH_KEY!!RESET!
) else (
    echo   !BLUE!→ Создание SSH ключа...!RESET!
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C "vpn-deploy" -q
    if !errorlevel! neq 0 (
        echo   !RED!✗ Не удалось создать SSH ключ!RESET!
        pause
        exit /b 1
    )
    echo   !GREEN!✓ Ключ создан: !SSH_KEY!!RESET!
)

:: ── Данные сервера ────────────────────────────────────────────────────────────
echo.
echo !BOLD!Введите адрес вашего Ubuntu-сервера:!RESET!
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
echo   !BLUE!→ Автоопределение пользователя SSH...!RESET!

:: Пробуем root с ключом (BatchMode, без пароля)
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" ^
    -o "BatchMode=yes" -p !SSH_PORT! root@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! equ 0 (
    set SERVER_USER=root
    echo   !GREEN!✓ Подключён как root ^(ключ^)!RESET!
    goto connected
)

:: Пробуем sysadmin с ключом
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=5" ^
    -o "BatchMode=yes" -p !SSH_PORT! sysadmin@!SERVER_IP! "exit 0" >nul 2>&1
if !errorlevel! equ 0 (
    set SERVER_USER=sysadmin
    echo   !GREEN!✓ Подключён как sysadmin ^(ключ^)!RESET!
    goto connected
)

:: Ключ ещё не установлен — просим пароль root
echo   !YELLOW!→ Ключ не установлен. Установка через пароль root...!RESET!
set /p PUBKEY=<"!SSH_KEY!.pub"
ssh -o "StrictHostKeyChecking=accept-new" -p !SSH_PORT! root@!SERVER_IP! ^
    "mkdir -p ~/.ssh && echo !PUBKEY! >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
if !errorlevel! neq 0 (
    echo.
    echo   !RED!✗ Не удалось добавить ключ!RESET!
    echo     Проверьте: IP-адрес, пароль root, SSH порт.
    echo.
    pause
    exit /b 1
)
set SERVER_USER=root
echo   !GREEN!✓ SSH ключ установлен!RESET!

:connected

:: ── [3/5] Проверка подключения ───────────────────────────────────────────────
echo.
echo !BOLD![3/5] Проверка подключения...!RESET!
ssh -n -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" -o "ConnectTimeout=10" ^
    -o "BatchMode=yes" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! ^
    "hostname && (lsb_release -d 2>/dev/null | cut -f2 || grep PRETTY /etc/os-release | cut -d= -f2 | tr -d '\"' || echo Ubuntu)" ^
    >nul 2>&1
if !errorlevel! neq 0 (
    echo   !RED!✗ Подключение не удалось!RESET!
    echo     Проверьте: IP !SERVER_IP!, пользователь !SERVER_USER!, порт !SSH_PORT!
    pause
    exit /b 1
)
echo   !GREEN!✓ Подключено: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!!RESET!

:: ── Подтверждение ─────────────────────────────────────────────────────────────
echo.
echo   !YELLOW!Установка займёт 20–40 минут. Не закрывайте окно.!RESET!
echo.
set /p CONFIRM=  Начать установку? [y/N]:
if /i "!CONFIRM!" neq "y" (
    echo   Отменено.
    pause
    exit /b 0
)

:: ── [4/5] Загрузка репозитория ───────────────────────────────────────────────
echo.
echo !BOLD![4/5] Загрузка репозитория...!RESET!

set REPO_ROOT=%~dp0..\..
set SETUP_PATH=/opt/vpn/setup.sh
set UPLOAD_OK=0

if not exist "!REPO_ROOT!\setup.sh" goto download_release
if not exist "!REPO_ROOT!\install-home.sh" goto download_release
if not exist "!REPO_ROOT!\home" goto download_release

echo   !BLUE!→ Упаковка локального репозитория...!RESET!
tar -czf "%TEMP%\vpn-infra.tar.gz" ^
    --exclude=".git" --exclude="*.pyc" --exclude="__pycache__" ^
    --exclude="*/venv/*" --exclude="node_modules" ^
    --exclude="*.log" --exclude=".env" ^
    -C "!REPO_ROOT!" . >nul 2>&1
if !errorlevel! neq 0 goto download_release

echo   !BLUE!→ Загрузка архива на сервер...!RESET!
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
echo   !GREEN!✓ Репозиторий загружен из локальной копии!RESET!
goto check_tui

:download_release
echo   !BLUE!→ Скачивание последнего релиза с GitHub...!RESET!
ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
    -o "ServerAliveInterval=30" -p !SSH_PORT! ^
    !SERVER_USER!@!SERVER_IP! ^
    "RELEASE_URL=$(curl -sSfL --max-time 10 https://api.github.com/repos/Cyrillicspb/vpn-infra/releases/latest 2>/dev/null | python3 -c \"import sys,json; assets=[a for a in json.load(sys.stdin)['assets'] if a['name']=='vpn-infra.tar.gz']; print(assets[0]['browser_download_url'] if assets else '')\" 2>/dev/null) && [ -n \"$RELEASE_URL\" ] && curl -fsSL --max-time 120 \"$RELEASE_URL\" -o /tmp/vpn-infra.tar.gz && sudo mkdir -p /opt/vpn && sudo tar xzf /tmp/vpn-infra.tar.gz -C /opt/vpn --no-same-permissions --no-same-owner 2>/dev/null; rm -f /tmp/vpn-infra.tar.gz && echo OK || (echo FAILED; exit 1)"
if !errorlevel! neq 0 (
    echo   !RED!✗ Не удалось скачать релиз!RESET!
    echo     Проверьте интернет или загрузите вручную.
    pause
    exit /b 1
)
set UPLOAD_OK=1
echo   !GREEN!✓ Релиз скачан с GitHub!RESET!

:: ── [5/5] Установка ───────────────────────────────────────────────────────────
:check_tui
echo.
echo !BOLD![5/5] Установка...!RESET!

set TUI_INSTALLER=/opt/vpn/installers/gui/installer.py
set USE_TUI=0
set PY_VER=0

:: Проверяем Python 3.10+
echo   !BLUE!→ Проверка Python 3.10+ на сервере...!RESET!
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
        echo   !GREEN!✓ Python !PY_MAJ!.!PY_MIN! — TUI-установщик готов!RESET!
    ) else (
        echo   !YELLOW!⚠ installer.py не найден — консольный режим!RESET!
    )
) else (
    set /a PY_MAJ=!PY_CMP!/100
    set /a PY_MIN=!PY_CMP!-!PY_MAJ!*100
    echo   !YELLOW!⚠ Python !PY_MAJ!.!PY_MIN! ^< 3.10 — консольный режим!RESET!
)

echo.

:: ── Запуск TUI ───────────────────────────────────────────────────────────────
if "!USE_TUI!"=="1" (
    echo !BOLD!▶ Запуск TUI-установщика...!RESET!
    echo !BLUE!══════════════════════════════════════════!RESET!
    echo.
    ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
        -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" ^
        -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! ^
        "cd /opt/vpn && python3 installers/gui/installer.py"
    set TUI_RC=!errorlevel!
    echo.
    if !TUI_RC! equ 0 (
        echo !BLUE!══════════════════════════════════════════!RESET!
        echo   !GREEN!✓ Готово!!RESET!
        echo.
        echo   Если установка завершена — следующие шаги:
        echo     1. Port Forwarding: UDP 51820+51821 → !SERVER_IP!
        echo     2. Telegram: напишите /start вашему боту
        echo     3. /adddevice — получите конфиг WireGuard/AWG
        echo.
        pause
        exit /b 0
    )
    echo   !YELLOW!⚠ TUI завершился с кодом !TUI_RC! — откат на консольный режим...!RESET!
    echo.
)

:: ── Fallback: tmux + setup.sh ─────────────────────────────────────────────────
echo !BOLD!▶ Запуск setup.sh в tmux...!RESET!
echo !BLUE!══════════════════════════════════════════!RESET!
echo.

ssh -i "!SSH_KEY!" -o "StrictHostKeyChecking=accept-new" ^
    -o "ServerAliveInterval=30" -o "ServerAliveCountMax=10" ^
    -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! ^
    "tmux new-session -A -s vpn-install 'sudo bash !SETUP_PATH!'"
set RESULT=!errorlevel!

echo.
echo !BLUE!══════════════════════════════════════════!RESET!

if !RESULT! equ 0 (
    echo   !GREEN!✓ Установка завершена успешно!!RESET!
    echo.
    echo   Следующие шаги:
    echo     1. Port Forwarding на роутере:
    echo        UDP 51820 → !SERVER_IP!:51820  ^(AmneziaWG^)
    echo        UDP 51821 → !SERVER_IP!:51821  ^(WireGuard^)
    echo     2. Telegram: напишите /start вашему боту
    echo     3. /adddevice — получите конфиг
    echo.
    echo   SSH доступ:
    echo     ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
) else (
    echo   !RED!✗ Установка завершилась с ошибкой ^(код !RESULT!^)!RESET!
    echo.
    echo   Диагностика:
    echo     ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
    echo     cat /tmp/vpn-setup.log
    echo.
    echo   Повторный запуск безопасен — выполненные шаги пропустятся.
)

echo.
pause
endlocal

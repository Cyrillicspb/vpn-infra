@echo off
setlocal enabledelayedexpansion
title VPN Infrastructure Setup

cls
echo.
echo  ==========================================
echo   VPN Infrastructure -- Windows Setup
echo  ==========================================
echo.
echo  Connects to your Ubuntu server and runs setup.sh
echo.
pause

:: --- check ssh ---
where ssh >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: SSH not found.
    echo  Fix: Settings - Apps - Optional features - OpenSSH Client
    echo.
    pause
    exit /b 1
)
echo  [OK] SSH found.
echo.

:: --- server credentials ---
:input_ip
echo  Enter server IP address:
set /p SERVER_IP=  IP:
if "!SERVER_IP!"=="" goto input_ip

echo.
echo  SSH port - press Enter for default 22:
set /p SSH_PORT=  Port:
if "!SSH_PORT!"=="" set SSH_PORT=22

echo.

:: --- generate ssh key if missing ---
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key

if exist "!SSH_KEY!" (
    echo  [OK] SSH key exists: !SSH_KEY!
) else (
    echo  Generating SSH key...
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C vpn-deploy
    if %errorlevel% neq 0 (
        echo  ERROR: Could not generate SSH key.
        pause
        exit /b 1
    )
    echo  [OK] Key created: !SSH_KEY!
)
echo.

:: --- clear stale known_hosts entry (handles server reinstall) ---
ssh-keygen -R !SERVER_IP! >nul 2>nul

:: -----------------------------------------------------------------------
:: Auto-detect user: try key auth for root, then sysadmin (after step 11
:: PermitRootLogin is disabled and the key is copied to sysadmin).
:: If both fail -- key not yet installed, ask for root password.
:: -----------------------------------------------------------------------
set SERVER_USER=

echo  Detecting SSH user...

:: Try root with key (no password, BatchMode).
:: -n prevents SSH from reading stdin (equivalent to < /dev/null).
:: Save errorlevel immediately -- any subsequent command (even echo) resets it.
echo  Trying root...
ssh -n -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes -p !SSH_PORT! root@!SERVER_IP! "exit 0" >nul 2>nul
set SSH_RC=!errorlevel!
if "!SSH_RC!"=="0" (
    set SERVER_USER=root
    echo  [OK] Connected as root ^(key auth^).
    goto connected
)

:: Try sysadmin with key (step 11 already ran -- PermitRootLogin=no)
echo  Trying sysadmin...
ssh -n -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes -p !SSH_PORT! sysadmin@!SERVER_IP! "exit 0" >nul 2>nul
set SSH_RC=!errorlevel!
if "!SSH_RC!"=="0" (
    set SERVER_USER=sysadmin
    echo  [OK] Connected as sysadmin ^(root SSH disabled after step 11^).
    goto connected
)

:: Key not installed yet -- first run, ask for root password
echo  Key not yet on server. Enter root password to install it:
echo.
set /p PUBKEY=<"!SSH_KEY!.pub"
ssh -o StrictHostKeyChecking=accept-new -p !SSH_PORT! root@!SERVER_IP! "mkdir -p ~/.ssh && echo !PUBKEY! >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not add SSH key to server.
    echo  Check: IP address, root password, SSH port.
    echo.
    pause
    exit /b 1
)
set SERVER_USER=root
echo  [OK] SSH key installed.

:connected
echo.
echo  Target: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!
echo.

:: --- confirm ---
echo  ==========================================
echo   Server ready. Starting installation.
echo  ==========================================
echo.
echo  WARNING: Takes 20-40 min. Do NOT close window.
echo.
set /p CONFIRM=  Type y and press Enter to start:
if /i "!CONFIRM!" neq "y" (
    echo  Cancelled.
    pause
    exit /b 0
)
echo.

:: --- upload full repo if available locally, else download on server ---
set REPO_ROOT=%~dp0..\..
set SETUP_PATH=/tmp/setup.sh

if not exist "!REPO_ROOT!\setup.sh" goto download_scripts
if not exist "!REPO_ROOT!\install-home.sh" goto download_scripts
if not exist "!REPO_ROOT!\home" goto download_scripts

echo  Uploading full repository to server /opt/vpn ...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "sudo mkdir -p /opt/vpn && sudo chown !SERVER_USER!:!SERVER_USER! /opt/vpn"
scp -i "!SSH_KEY!" -P !SSH_PORT! -o StrictHostKeyChecking=accept-new -r "!REPO_ROOT!\." !SERVER_USER!@!SERVER_IP!:/opt/vpn/
if %errorlevel% neq 0 goto download_scripts
set SETUP_PATH=/opt/vpn/setup.sh
echo  [OK] Uploaded full repo from local copy.
goto run_setup

:download_scripts
echo  Downloading scripts on server (GitHub then jsdelivr CDN fallback)...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "cd /tmp && dl(){ local f=$1; for b in https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master https://cdn.jsdelivr.net/gh/Cyrillicspb/vpn-infra@master; do curl -fsSL --max-time 30 $b/$f -o $f 2>/dev/null && echo OK:$f && return 0; done; echo ERR:$f; return 1; }; dl setup.sh && dl install-home.sh && dl install-vps.sh && chmod +x setup.sh install-home.sh install-vps.sh"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not download scripts from GitHub or jsdelivr CDN.
    echo  Try manually copying setup.sh to the server: scp setup.sh user@ip:/tmp/
    echo.
    pause
    exit /b 1
)
echo  [OK] Downloaded ^(GitHub or jsdelivr CDN^).

:run_setup
echo.
echo  Running setup.sh on server...
echo  ==========================================
echo.
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! "bash -c 'sudo bash !SETUP_PATH! 2>&1 | tee /tmp/vpn-setup.log; exit ${PIPESTATUS[0]}'"
set RESULT=!errorlevel!

echo.
echo  ==========================================
if !RESULT! equ 0 (
    echo.
    echo  [OK] Installation complete!
    echo.
    echo  Next steps:
    echo    1. Router Port Forwarding:
    echo       UDP 51820 --^> !SERVER_IP!:51820  ^(AmneziaWG^)
    echo       UDP 51821 --^> !SERVER_IP!:51821  ^(WireGuard^)
    echo    2. Telegram: send /start to your bot
    echo    3. Import config into WireGuard / AmneziaWG
    echo.
    echo  SSH access:
    echo    ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
) else (
    echo.
    echo  [ERROR] Installation failed, code !RESULT!
    echo.
    echo  Diagnostics:
    echo    ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
    echo    cat /tmp/vpn-setup.log
    echo.
    echo  Re-run is safe -- completed steps are skipped.
)

echo.
pause
endlocal

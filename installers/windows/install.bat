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
echo  SSH username - press Enter for default sysadmin:
set /p SERVER_USER=  User:
if "!SERVER_USER!"=="" set SERVER_USER=sysadmin

echo.
echo  SSH port - press Enter for default 22:
set /p SSH_PORT=  Port:
if "!SSH_PORT!"=="" set SSH_PORT=22

echo.
echo  Target: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!
echo.

:: --- generate ssh key ---
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key

if exist "!SSH_KEY!" goto key_exists
echo  Generating SSH key...
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C vpn-deploy
if %errorlevel% neq 0 (
    echo  ERROR: Could not generate SSH key.
    pause
    exit /b 1
)
echo  [OK] Key created: !SSH_KEY!
goto key_done
:key_exists
echo  [OK] Key exists: !SSH_KEY!
:key_done
echo.

:: --- copy public key to server (scp .pub file, then append) ---
echo  Copying public key to server...
echo  Enter server password when prompted:
echo.
scp -P !SSH_PORT! -o StrictHostKeyChecking=accept-new "!SSH_KEY!.pub" !SERVER_USER!@!SERVER_IP!:/tmp/vpn_id.pub
if %errorlevel% neq 0 (
    echo.
    echo  WARNING: Could not copy key via scp.
    echo  Try manually: copy content of !SSH_KEY!.pub to ~/.ssh/authorized_keys
    echo.
    pause
    goto test_connection
)
ssh -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p ~/.ssh && cat /tmp/vpn_id.pub >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && rm /tmp/vpn_id.pub"
echo.

:: --- test connection with key ---
:test_connection
echo  Testing SSH connection...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "echo OK_CONNECTED"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Cannot connect to server.
    echo  Check: IP, username, port, sshd running on server.
    echo  Try:   ssh !SERVER_USER!@!SERVER_IP! -p !SSH_PORT!
    echo.
    pause
    exit /b 1
)
echo  [OK] Connected.
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

:: --- upload setup.sh if available locally, else download on server ---
set SETUP_UPLOADED=0
set LOCAL_SETUP=%~dp0..\..\setup.sh

if not exist "!LOCAL_SETUP!" goto download_setup

echo  Uploading setup.sh from local repo...
scp -i "!SSH_KEY!" -P !SSH_PORT! -o StrictHostKeyChecking=accept-new "!LOCAL_SETUP!" !SERVER_USER!@!SERVER_IP!:/tmp/vpn-setup.sh
if %errorlevel% equ 0 (
    echo  [OK] Uploaded.
    set SETUP_UPLOADED=1
) else (
    echo  Upload failed - will download on server.
)

:download_setup
if !SETUP_UPLOADED! equ 1 goto run_setup

echo  Downloading setup.sh on server...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "curl -sf https://cdn.jsdelivr.net/gh/Cyrillicspb/vpn-infra@master/setup.sh -o /tmp/vpn-setup.sh || curl -sf https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/setup.sh -o /tmp/vpn-setup.sh"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not download setup.sh.
    echo  Make the GitHub repo public, or run from the repo folder.
    echo.
    pause
    exit /b 1
)

:run_setup
echo.
echo  Running setup.sh on server...
echo  ==========================================
echo.
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -p !SSH_PORT! -t !SERVER_USER!@!SERVER_IP! "sudo bash /tmp/vpn-setup.sh 2>&1 | tee /tmp/vpn-setup.log; exit ${PIPESTATUS[0]}"
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

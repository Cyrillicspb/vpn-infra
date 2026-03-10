@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
title VPN Infrastructure Setup

cls
echo.
echo  ==========================================
echo    VPN Infrastructure -- Setup
echo  ==========================================
echo.
echo  This script connects to your home server
echo  and starts VPN installation.
echo.
echo  Requirements:
echo    - Windows 10/11 with OpenSSH (built-in)
echo    - Home server: Ubuntu Server 24.04
echo    - Server connected to router via Ethernet
echo.
pause

REM ── Check OpenSSH ──────────────────────────────────────────────────────────
where ssh >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] SSH not found.
    echo.
    echo  Install OpenSSH Client:
    echo    Settings -^> Apps -^> Optional features
    echo    -^> Add a feature -^> OpenSSH Client
    echo.
    pause
    exit /b 1
)
echo  [OK] SSH found.
echo.

REM ── Server credentials ─────────────────────────────────────────────────────
echo  Enter home server IP address:
echo  (example: 192.168.1.100 -- find it in your router admin panel)
set /p SERVER_IP=  IP:
if "!SERVER_IP!"=="" (
    echo  IP cannot be empty.
    goto :eof
)

echo.
echo  SSH username (press Enter for default: sysadmin):
set /p SERVER_USER=  User:
if "!SERVER_USER!"=="" set SERVER_USER=sysadmin

echo.
echo  SSH port (press Enter for default: 22):
set /p SSH_PORT=  Port:
if "!SSH_PORT!"=="" set SSH_PORT=22

echo.
echo  Target: !SERVER_USER!@!SERVER_IP!:!SSH_PORT!
echo.

REM ── SSH key ────────────────────────────────────────────────────────────────
set SSH_KEY=%USERPROFILE%\.ssh\vpn_deploy_key

if not exist "!SSH_KEY!" (
    echo  Generating SSH key...
    if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
    ssh-keygen -t ed25519 -f "!SSH_KEY!" -N "" -C "vpn-deploy" >nul 2>&1
    if !errorlevel! equ 0 (
        echo  [OK] SSH key created: !SSH_KEY!
    ) else (
        echo  [ERROR] Failed to create SSH key.
        pause
        exit /b 1
    )
) else (
    echo  [OK] SSH key already exists: !SSH_KEY!
)
echo.

REM ── Copy public key to server ──────────────────────────────────────────────
echo  Copying public key to server...
echo  (Enter password for !SERVER_USER!@!SERVER_IP! when asked)
echo.

set /p PUBKEY=< "!SSH_KEY!.pub"
ssh -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p ~/.ssh && printf '%%s\n' '!PUBKEY!' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo SSH_KEY_OK"

if !errorlevel! neq 0 (
    echo.
    echo  [WARNING] Could not copy key automatically.
    echo.
    echo  Manual steps:
    echo    1. Connect: ssh !SERVER_USER!@!SERVER_IP! -p !SSH_PORT!
    echo    2. Run on server:
    echo       mkdir -p ~/.ssh
    echo       nano ~/.ssh/authorized_keys
    echo    3. Paste this key:
    type "!SSH_KEY!.pub"
    echo.
    pause
)

REM ── Test connection ────────────────────────────────────────────────────────
echo.
echo  Testing connection...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "echo OK_CONNECTION" 2>&1
if !errorlevel! neq 0 (
    echo.
    echo  [ERROR] Cannot connect to server.
    echo.
    echo  Check:
    echo    1. Server is powered on and connected
    echo    2. IP: !SERVER_IP!, user: !SERVER_USER!, port: !SSH_PORT!
    echo    3. openssh-server is installed on Ubuntu server
    echo.
    echo  Try manually:
    echo    ssh !SERVER_USER!@!SERVER_IP! -p !SSH_PORT!
    echo.
    pause
    exit /b 1
)
echo  [OK] Connected successfully.

REM ── Confirm ────────────────────────────────────────────────────────────────
echo.
echo  ==========================================
echo    Server is ready. Starting installation.
echo  ==========================================
echo.
echo  WARNING: Installation takes 20-40 minutes.
echo  Do NOT close this window!
echo.
echo  Press Enter to start, or close window to cancel.
pause

echo.
echo  Connecting to !SERVER_USER!@!SERVER_IP!...
echo  ==========================================
echo.

REM ── Run setup.sh ───────────────────────────────────────────────────────────
ssh -i "!SSH_KEY!" ^
    -o StrictHostKeyChecking=accept-new ^
    -o ServerAliveInterval=30 ^
    -o ServerAliveCountMax=10 ^
    -p !SSH_PORT! ^
    -t !SERVER_USER!@!SERVER_IP! ^
    "bash -lc \"set -e; if ! command -v curl >/dev/null 2>&1; then sudo apt-get install -y curl; fi; echo '=== Downloading setup.sh ==='; if curl -sf --max-time 30 https://raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/setup.sh -o /tmp/vpn-setup.sh; then echo '[OK] Downloaded from GitHub'; else echo '[ERROR] GitHub not available'; exit 1; fi; chmod +x /tmp/vpn-setup.sh; echo '=== Running setup.sh ==='; sudo bash /tmp/vpn-setup.sh 2>&1 | tee /tmp/vpn-setup.log\""

set SETUP_RESULT=!errorlevel!

echo.
echo  ==========================================

if !SETUP_RESULT! equ 0 (
    echo.
    echo  [OK] Installation completed successfully!
    echo.
    echo  Next steps:
    echo    1. Configure Port Forwarding on your router:
    echo       UDP 51820 --^> !SERVER_IP!:51820  (AmneziaWG)
    echo       UDP 51821 --^> !SERVER_IP!:51821  (WireGuard)
    echo    2. Open Telegram and send /start to your bot
    echo    3. Get your config and import into WireGuard/AmneziaWG
    echo.
    echo  Your SSH key for future access:
    echo    ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
) else (
    echo.
    echo  [ERROR] Installation failed with code !SETUP_RESULT!
    echo.
    echo  Diagnostics:
    echo    ssh -i "!SSH_KEY!" -p !SSH_PORT! !SERVER_USER!@!SERVER_IP!
    echo    cat /tmp/vpn-setup.log
    echo.
    echo  Re-run is safe -- completed steps are skipped automatically.
)

echo.
pause
endlocal

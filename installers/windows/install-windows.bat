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
echo  SSH username - press Enter for default root:
set /p SERVER_USER=  User:
if "!SERVER_USER!"=="" set SERVER_USER=root

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

:: --- clear stale known_hosts entry (handles server reinstall) ---
ssh-keygen -R !SERVER_IP! >nul 2>&1

:: --- add public key to server (one SSH command, one password prompt) ---
echo  Adding SSH key to server...
echo  Enter server password when prompted:
echo.
set /p PUBKEY=<"!SSH_KEY!.pub"
ssh -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p ~/.ssh && echo !PUBKEY! >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not add SSH key to server.
    echo  Check: IP address, username, password, SSH port.
    echo.
    pause
    exit /b 1
)
echo  [OK] SSH key added.
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

:: --- upload full repo if available locally, else download on server ---
set REPO_ROOT=%~dp0..\..
set SETUP_PATH=/tmp/setup.sh

if not exist "!REPO_ROOT!\setup.sh" goto download_scripts
if not exist "!REPO_ROOT!\install-home.sh" goto download_scripts
if not exist "!REPO_ROOT!\home" goto download_scripts

echo  Uploading full repository to server /opt/vpn ...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "mkdir -p /opt/vpn"
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
echo  [OK] Downloaded (GitHub or jsdelivr CDN).
echo  Verifying files on server...
ssh -i "!SSH_KEY!" -o StrictHostKeyChecking=accept-new -p !SSH_PORT! !SERVER_USER!@!SERVER_IP! "ls -lh /tmp/setup.sh /tmp/install-home.sh"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Files missing on server after download.
    echo.
    pause
    exit /b 1
)
echo  [OK] Files verified.

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

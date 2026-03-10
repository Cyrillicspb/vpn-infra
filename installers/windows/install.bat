@echo off
chcp 65001 > nul
echo === VPN Infrastructure Installer ===
echo.
set /p SERVER_IP=IP-адрес домашнего сервера:
set /p SSH_USER=Пользователь SSH (default: sysadmin):
if "%SSH_USER%"=="" set SSH_USER=sysadmin

echo.
echo Подключение к %SSH_USER%@%SERVER_IP%...
ssh -t %SSH_USER%@%SERVER_IP% "if [ -f /opt/vpn/setup.sh ]; then cd /opt/vpn && bash setup.sh; else curl -fsSL https://raw.githubusercontent.com/your-repo/vpn-infra/main/setup.sh -o /tmp/setup.sh && bash /tmp/setup.sh; fi"
pause

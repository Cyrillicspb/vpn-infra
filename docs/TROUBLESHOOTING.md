# Устранение неполадок

## Содержание

- [Диагностические команды](#диагностические-команды)
- [VPN не работает после установки](#vpn-не-работает-после-установки)
- [Заблокированные сайты недоступны](#заблокированные-сайты-недоступны)
- [VPN подключён, но медленно](#vpn-подключён-но-медленно)
- [Туннель периодически отваливается](#туннель-периодически-отваливается)
- [Бот не отвечает](#бот-не-отвечает)
- [Клиент не получает конфиг](#клиент-не-получает-конфиг)
- [Проблемы с DNS](#проблемы-с-dns)
- [Watchdog мёртв](#watchdog-мёртв)
- [Ошибки установки](#ошибки-установки)
- [Проблемы с VPS](#проблемы-с-vps)
- [Конфликт с офисным VPN](#конфликт-с-офисным-vpn)

---

## Диагностические команды

Перед поиском проблемы соберите информацию:

```bash
# В Telegram-боте (основная диагностика):
/status                     # общий статус
/diagnose ИмяУстройства     # детальная диагностика пира
/docker                     # статус контейнеров

# На сервере (SSH):
sudo systemctl status watchdog dnsmasq wg-quick@wg0 wg-quick@wg1
sudo wg show                # статус WireGuard интерфейсов
sudo nft list table inet vpn   # таблица nftables
ip rule show               # policy routing rules
ip route show table 200    # маршруты blocked → tun
ip route show table 100    # маршруты unblocked → eth0
docker ps -a               # все контейнеры и их статус
journalctl -u watchdog --since "1 hour ago"  # логи watchdog
```

---

## VPN не работает после установки

### Симптом: WireGuard подключается, но сайты недоступны

**Шаг 1: Проверьте port forwarding**
```bash
# Проверьте с мобильного телефона (4G, без Wi-Fi):
curl -s https://icanhazip.com   # должен вернуть IP вашего роутера
```

Если IP отличается от WAN IP роутера — CGNAT или двойной NAT.

```bash
# Проверьте доступность портов (с мобильного или другого сервера):
nc -zvu <ВНЕШНИЙ_IP> 51820   # AWG
nc -zvu <ВНЕШНИЙ_IP> 51821   # WG
```

**Шаг 2: Проверьте WireGuard**
```bash
sudo wg show
```

Должны быть строки `interface: wg0` и `interface: wg1`. Если нет:
```bash
sudo systemctl restart wg-quick@wg0 wg-quick@wg1
```

**Шаг 3: Проверьте nftables**
```bash
sudo nft list table inet vpn
sudo nft list chain inet vpn forward
```

Должна быть цепочка `forward` с `policy drop` и правилами kill switch.

**Шаг 4: Проверьте маршруты**
```bash
ip rule show
ip route show table 200
ip route show table 100
```

Если таблицы пустые — перезапустите vpn-routes:
```bash
sudo systemctl restart vpn-routes
```

**Шаг 5: Проверьте туннель (tun)**
```bash
ip link show | grep tun
ping -I tun0 10.177.2.2 -c 3   # ping VPS через туннель
```

Если tun-интерфейса нет — watchdog не запустил стек:
```bash
sudo systemctl status watchdog
journalctl -u watchdog -n 50
```

---

### Симптом: Smoke-тест не прошёл при установке

Запустите тесты вручную:
```bash
cd /opt/vpn
sudo bash tests/smoke/test_dns.sh
sudo bash tests/smoke/test_split.sh
sudo bash tests/smoke/test_tunnel.sh
sudo bash tests/smoke/test_watchdog.sh
sudo bash tests/smoke/test_kill_switch.sh
```

Каждый тест выводит причину провала.

---

## Заблокированные сайты недоступны

### Диагностика

```bash
# 1. Проверьте тоннель:
sudo bash /opt/vpn/tests/smoke/test_split.sh

# 2. Проверьте DNS:
dig @127.0.0.1 youtube.com     # должен вернуть IP
dig @127.0.0.1 youtube.com +short

# 3. Проверьте nftset:
sudo nft list set inet vpn blocked_dynamic | grep -c element

# 4. Проверьте fwmark:
sudo conntrack -L | grep 8.8.8.8   # убедитесь что marked пакеты видны
```

### Сайт не добавлен в базы РКН

Добавьте вручную:
```
/vpn add сайт.ru
```

Конфиги клиентов обновятся через 5 минут.

### DNS не резолвит через VPS

```bash
# Проверьте upstream DNS:
dig @10.177.2.2 youtube.com    # должен работать через Tier-2 туннель

# Проверьте vpn-domains.conf:
grep youtube /opt/vpn/home/dnsmasq/dnsmasq.d/vpn-domains.conf

# Перезапустите dnsmasq:
sudo systemctl restart dnsmasq
```

### blocked_static пуст (базы не загрузились)

```bash
sudo nft list set inet vpn blocked_static | head -5

# Обновите вручную:
sudo /opt/vpn/venv/bin/python3 /opt/vpn/home/scripts/update-routes.py --force

# Восстановите из файла:
sudo nft -f /etc/nftables-blocked-static.conf
```

### Клиент использует Private DNS (DoH/DoT)

Android 9+ и iOS 14+ могут использовать встроенный DoH/DoT, обходя dnsmasq.

**Android:** Настройки → Подключения → Дополнительные настройки → Private DNS → Отключить

**iOS:** (Профиль с DoH) → Настройки → Основные → VPN и управление устройством → удалить профиль

---

## VPN подключён, но медленно

### Диагностика скорости

```
/speed          — speedtest через текущий стек
/graph speed    — история скоростей
/graph tunnel   — RTT и стек
```

### Переключить стек вручную

```
/switch hysteria2    — самый быстрый (если не заблокирован)
/switch reality      — хороший баланс
/switch grpc         — устойчивый
/switch cdn          — медленный, но неблокируемый
```

### Возможные причины

| Причина | Диагностика | Решение |
|---------|-------------|---------|
| Upload домашнего интернета насыщен | `/graph system` → сеть | Меньше клиентов одновременно |
| VPS перегружен | `/graph system` → VPS CPU | Сменить тарифный план VPS |
| Текущий стек шейпируется | `/graph speed` → деградация | Watchdog переключит автоматически |
| Много пиров | `/clients` | Отключить неактивных: `/client disable Имя` |
| MTU проблемы | `ping -M do -s 1400 8.8.8.8` | Уменьшить MTU в WG конфиге |

### MTU проблемы

Признак: скорость низкая, маленькие пакеты быстрые, большие — медленные.

```bash
# Найдите оптимальный MTU:
ping -M do -s 1300 <IP_VPS> -c 3
ping -M do -s 1350 <IP_VPS> -c 3
ping -M do -s 1400 <IP_VPS> -c 3
# Уменьшайте пока не появятся потери
```

Установите MTU в `/etc/wireguard/wg0.conf`:
```ini
[Interface]
MTU = 1320   # или найденное значение
```

---

## Туннель периодически отваливается

### Признаки

- Watchdog присылает алерты «Туннель недоступен»
- Заблокированные сайты периодически перестают работать на ~10–30 сек

### Причина 1: NAT Timeout на роутере

WireGuard использует UDP — некоторые роутеры закрывают UDP NAT-сессию при простое.

**Решение:** Увеличьте UDP timeout на роутере. Обычно: 180–300 сек → 600+ сек.

Или проверьте `PersistentKeepalive = 25` в конфиге клиента (должен быть выставлен).

### Причина 2: DPI-блокировка текущего стека

Watchdog определит это и переключится. Если переключается слишком часто:

```bash
journalctl -u watchdog | grep "failover\|switch\|degraded" | tail -20
```

Попробуйте переключить стек с большей устойчивостью: `/switch cdn`

### Причина 3: DKMS модуль слетает после обновления ядра

```bash
# Проверьте:
dpkg -l | grep amneziawg
modinfo amneziawg

# Пересоберите:
sudo dkms build amneziawg -v <VERSION>
sudo dkms install amneziawg -v <VERSION>
sudo systemctl restart wg-quick@wg0
```

---

## Бот не отвечает

### Шаг 1: Проверьте контейнер

```bash
docker ps | grep telegram-bot
docker logs telegram-bot --tail 100
```

### Шаг 2: Проверьте Watchdog API

```bash
curl -s http://localhost:8080/status \
  -H "Authorization: Bearer $(grep WATCHDOG_API_TOKEN /opt/vpn/.env | cut -d= -f2)"
```

Если не отвечает — watchdog упал:
```bash
sudo systemctl restart watchdog
journalctl -u watchdog -n 50
```

### Шаг 3: Проверьте Telegram API

```bash
source /opt/vpn/.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```

Если ошибка `401 Unauthorized` — токен недействителен. Создайте нового бота через @BotFather.

### Шаг 4: Перезапустите бот

```bash
docker restart telegram-bot
# или через бота:
/restart telegram-bot
```

---

## Клиент не получает конфиг

### Бот присылает "AllowedIPs > 50, QR-код недоступен"

При большом количестве записей QR-код не умещается. Получите `.conf` файл вместо QR:
```
/myconfig ИмяУстройства
```

### Конфиг есть, но WireGuard не подключается

1. Проверьте что port forwarding настроен (UDP 51820/51821)
2. Проверьте Endpoint в конфиге — должен быть внешний IP или DDNS-домен
3. Проверьте что AmneziaWG приложение установлено (для AWG-конфига)
4. Попробуйте WG-протокол: `/adddevice` → выбрать WG

### Конфиг устарел (после обновления баз)

```
/update    — получить актуальные конфиги всех ваших устройств
```

---

## Проблемы с DNS

### dnsmasq не запускается

```bash
sudo systemctl status dnsmasq
journalctl -u dnsmasq -n 30

# Проверьте конфиг:
sudo dnsmasq --test -C /etc/dnsmasq.conf

# Проверьте порт 53 (занят другим):
sudo ss -tulpn | grep :53
```

Если порт 53 занят `systemd-resolved`:
```bash
sudo systemctl disable --now systemd-resolved
echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf
```

### DNS работает, но заблокированные домены не добавляются в nft set

```bash
# Тест:
dig @127.0.0.1 youtube.com
sudo nft list set inet vpn blocked_dynamic | grep -c element

# Проверьте формат в vpn-domains.conf:
grep nftset /opt/vpn/home/dnsmasq/dnsmasq.d/vpn-domains.conf | head -3
# должно быть: nftset=/youtube.com/4#inet#vpn#blocked_dynamic
```

---

## Watchdog мёртв

Если watchdog упал и не запускается:

```bash
sudo systemctl status watchdog
journalctl -u watchdog --since "10 minutes ago"

# Попробуйте запустить вручную:
cd /opt/vpn
sudo -u root ./venv/bin/python3 watchdog/watchdog.py

# Проверьте Python venv:
sudo /opt/vpn/venv/bin/python3 -c "import fastapi; print('OK')"

# Пересоздайте venv если нужно:
sudo rm -rf /opt/vpn/venv
sudo python3 -m venv /opt/vpn/venv
sudo /opt/vpn/venv/bin/pip install -r /opt/vpn/home/watchdog/requirements.txt
sudo systemctl restart watchdog
```

### Failsafe не работает

```bash
# Проверьте cron:
sudo cat /etc/cron.d/vpn-watchdog-failsafe
sudo systemctl status cron

# Запустите вручную:
sudo bash /opt/vpn/home/scripts/watchdog-failsafe.sh
```

---

## Ошибки установки

### setup.sh: "Шаг N провалился"

```bash
# Посмотрите детали:
sudo bash setup.sh 2>&1 | tee /tmp/setup-debug.log

# Повторите только нужный шаг (удалите строку из .setup-state):
sudo sed -i '/STEP_N_DONE/d' /opt/vpn/.setup-state
sudo bash setup.sh
```

### DKMS amneziawg не собирается

```bash
# Установите заголовки текущего ядра:
sudo apt install -y linux-headers-$(uname -r)

# Проверьте DKMS:
sudo dkms status
sudo dkms install amneziawg -v <VERSION>
```

### Docker не запускается

```bash
sudo systemctl status docker
# Если ошибка "No space left":
docker system prune -f
df -h
```

---

## Проблемы с VPS

### 3x-ui панель недоступна

Доступ только через mTLS (порт 8443):
```bash
# Нужен клиентский сертификат:
curl -k --cert /etc/nginx/mtls/client.crt --key /etc/nginx/mtls/client.key \
  https://<VPS_IP>:8443/xui/
```

Или через браузер с установленным P12-сертификатом клиента.

### Xray не принимает подключения

```bash
# На VPS:
ssh sysadmin@<VPS_IP>
docker logs 3x-ui --tail 50
docker exec 3x-ui cat /usr/local/bin/xray-test.log 2>/dev/null

# Проверьте порты:
ss -tulpn | grep 443

# Перезапустите:
docker restart 3x-ui
```

### VPS healthcheck шлёт алерты постоянно

```bash
# Проверьте логи:
sudo tail -100 /var/log/vpn-healthcheck.log

# Запустите вручную:
sudo bash /opt/vpn/scripts/vps-healthcheck.sh
```

---

## Конфликт с офисным VPN

### Симптом: после /exclude, офисные ресурсы недоступны

Офисный VPN использует подсеть которая пересекается с нашей (10.177.x.x)?

```bash
# Проверьте на клиентском устройстве:
ip route show | grep tun
```

Добавьте исключение в Telegram-боте:
```
/exclude add 10.0.0.0/8       # если офисный VPN использует 10.x.x.x
/exclude add 192.168.100.0/24  # конкретная офисная подсеть
```

### Симптом: наш VPN и офисный VPN конфликтуют (подсеть 10.177.x.x)

При установке setup.sh автоматически обнаруживает конфликт и предлагает альтернативную подсеть `172.29.177.0/24`.

Если уже установлено — обратитесь к администратору для переконфигурации подсетей.

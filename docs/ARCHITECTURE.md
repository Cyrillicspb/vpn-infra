# Архитектура

## Топология сети

```
КЛИЕНТЫ (телефоны, ноутбуки, роутеры)
    │ AmneziaWG UDP 51820 / WireGuard UDP 51821
    ▼
РОУТЕР (port forward)
    ▼
ДОМАШНИЙ СЕРВЕР — Ubuntu 24.04
    │ 4 стека → VPS
    ▼
VPS (один или несколько)
```

## Адресное пространство

| Сегмент | Подсеть |
|---|---|
| AWG клиенты | 10.177.1.0/24 |
| WG клиенты | 10.177.3.0/24 |
| Tier-2 туннель | 10.177.2.0/30 |
| Docker домашний | 172.20.0.0/24 |

## 4 стека (по устойчивости)

1. VLESS+WebSocket через Cloudflare CDN
2. VLESS+REALITY+gRPC (cdn.jsdelivr.net)
3. VLESS+REALITY (microsoft.com)
4. Hysteria2 (QUIC+Salamander)

## Split tunneling (Гибрид B+)

**Уровень 1** — AllowedIPs на клиенте (≤500 CIDR):
- CDN-подсети, РКН-блоки, DNS

**Уровень 2** — nftables fwmark на сервере:
- `blocked_static` — из баз РКН
- `blocked_dynamic` — из dnsmasq nftset (24h TTL)

## Kill switch

```
nftables: src VPN + dst in blocked + oifname != tun* → DROP
```

## Адаптивный failover

```
Деградация → тест стеков вверх по устойчивости → переключение
Фоновая переоценка раз в час → промоция лучшего
```

## Компоненты

- **watchdog** — центральный агент (Python async, systemd)
- **telegram-bot** — интерфейс (Python + aiogram 3.x, Docker)
- **dnsmasq** — DNS + nftset (systemd)
- **nftables** — NAT + fwmark routing + kill switch
- **3x-ui** — управление Xray на VPS
- **cloudflared** — CDN туннель

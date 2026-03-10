# Как начать работу с Claude Code для проекта vpn-infra

## Шаг 1: Установить Claude Code

Нужна подписка Claude Pro ($20/мес) или Max ($100/мес или $200/мес).
Max рекомендуется — больше токенов, длинные сессии.

### macOS / Linux:
```bash
# Установить Node.js 18+ если нет
# macOS:
brew install node
# Linux:
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Установить Claude Code
npm install -g @anthropic-ai/claude-code
```

### Windows:
```bash
# Установить Git for Windows (обязательно): https://gitforwindows.org
# Установить Node.js 18+: https://nodejs.org
npm install -g @anthropic-ai/claude-code
```

## Шаг 2: Создать репозиторий на GitHub

1. Зайти на https://github.com/new
2. Repository name: `vpn-infra`
3. Private (приватный!)
4. Create repository
5. Скопировать URL: `https://github.com/Cyrillicspb/vpn-infra.git`

## Шаг 3: Настроить локальный проект

```bash
# Создать папку проекта
mkdir vpn-infra
cd vpn-infra

# Инициализировать git
git init
git remote add origin https://github.com/Cyrillicspb/vpn-infra.git

# Скопировать CLAUDE.md в корень проекта
# (файл CLAUDE.md из этого чата — скачай и положи сюда)
```

## Шаг 4: Запустить Claude Code

```bash
# В папке vpn-infra:
claude
```

При первом запуске — авторизация через браузер (Claude аккаунт).

## Шаг 5: Начать работу

Claude Code автоматически прочитает CLAUDE.md и поймёт весь проект.

### Рекомендуемый порядок команд:

**Сессия 1 — Структура и основа:**
```
Прочитай CLAUDE.md. Создай полную структуру директорий проекта 
(home/, vps/, docs/, tests/, installers/, migrations/).
Создай .env.example, .gitignore, README.md.
Сделай первый коммит.
```

**Сессия 2 — Watchdog (ядро системы):**
```
Создай watchdog — центральный агент на хосте.
Python async (aiohttp/FastAPI), все API endpoints из CLAUDE.md.
Plugin-архитектура для стеков. Единый decision loop.
requirements.txt с пинами версий.
```

**Сессия 3 — Telegram-бот:**
```
Создай Telegram-бот на aiogram 3.x.
Все команды из CLAUDE.md (админские + клиентские).
Коммуникация с watchdog через HTTP API.
SQLite с WAL mode, все таблицы.
FSM с таймаутами. config_builder для .conf файлов.
```

**Сессия 4 — nftables, routing, dnsmasq:**
```
Создай шаблоны nftables конфигурации для гибрида B+:
fwmark routing, два nft sets (static + dynamic timeout 24h),
kill switch, rate limiting. Policy routing с двумя таблицами.
Конфиг dnsmasq с nftset=/ директивами.
```

**Сессия 5 — Docker Compose и systemd:**
```
Создай docker-compose.yml для домашнего сервера и VPS.
Все systemd units с правильным порядком загрузки.
Docker image pinning.
```

**Сессия 6 — Setup скрипт:**
```
Создай setup.sh — интерактивный мастер установки.
Фазы 0-5 из CLAUDE.md. Автообнаружение сети, CGNAT-проверка,
автогенерация секретов. Идемпотентность. Прогресс-бар.
```

**Сессия 7 — deploy.sh, restore.sh, backup.sh:**
```
Создай скрипты автоматизации: deploy с auto-rollback и smoke-test,
restore = install + overwrite из бэкапа, backup с sqlite3 .backup.
```

**Сессия 8 — Скрипт обновления маршрутов:**
```
Создай update-routes.py: скачивание из 5+ источников,
per-source кэш, валидация, CIDR-агрегация ≤500,
генерация nft-скрипта (атомарный nft -f),
генерация dnsmasq конфигов, diff + сигнал боту.
```

**Сессия 9 — VPS конфигурации:**
```
Создай конфиги VPS: 3x-ui, Nginx (mTLS + fallback),
Prometheus, Alertmanager (все алерты), Grafana dashboards,
cloudflared tunnel, vps-healthcheck.sh.
```

**Сессия 10 — Документация:**
```
Создай все docs/ из CLAUDE.md: INSTALL.md, ARCHITECTURE.md (Mermaid),
COMMANDS.md, FAQ.md, TROUBLESHOOTING.md, SECURITY.md, PRIVACY.md,
UPDATE.md, DISASTER-RECOVERY.md, REQUIREMENTS.md, HARDWARE.md.
Всё на русском.
```

**Сессия 11 — Тесты и финализация:**
```
Создай smoke-тесты (tests/), installers (.bat, .command),
migrations/, Dependabot config. Финальный review всех файлов.
Пуш на GitHub.
```

## Советы по работе с Claude Code

1. **Одна задача за раз.** Не давай «создай всё» — давай «создай watchdog».
   После каждой сессии проверяй результат.

2. **CLAUDE.md — источник правды.** Если Claude Code предлагает другое решение —
   скажи «следуй CLAUDE.md».

3. **Коммить часто.** После каждой завершённой части: `закоммить с сообщением "..."`.

4. **При ошибках.** Claude Code сам увидит ошибку и исправит. Если зациклился —
   скажи «стоп, давай другой подход».

5. **Контекст.** Claude Code видит все файлы проекта. Но если сессия длинная —
   напомни: «перечитай CLAUDE.md секцию про watchdog».

6. **Git push.** `запуш на GitHub` — и Claude Code сделает git push.

# Migrations

Директория для скриптов миграции конфигурации между версиями.

## Формат имени

`YYYYMMDD_NNN_description.sh`

Пример: `20240315_001_add_grpc_stack.sh`

## Применение

Скрипты применяются автоматически через `deploy.sh` при обновлении.
Каждый скрипт идемпотентен — безопасно запускать повторно.

## Структура скрипта

```bash
#!/bin/bash
# Migration: 20240315_001_add_grpc_stack
# From: v1.0 → To: v1.1
# Description: Добавление gRPC стека

set -euo pipefail
MIGRATION_ID="20240315_001"
STATE_FILE="/opt/vpn/.setup-state"

# Проверка — уже применена?
grep -q "$MIGRATION_ID" "$STATE_FILE" 2>/dev/null && exit 0

# ... действия миграции ...

echo "$MIGRATION_ID" >> "$STATE_FILE"
echo "Migration $MIGRATION_ID applied successfully"
```

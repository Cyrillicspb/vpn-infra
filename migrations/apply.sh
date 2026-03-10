#!/usr/bin/env bash
# Применение миграций БД и конфигов
# Вызывается из deploy.sh автоматически.
# Идемпотентен: каждая миграция применяется ровно один раз.
#
# Использование:
#   bash migrations/apply.sh                    — применить все новые
#   bash migrations/apply.sh --dry-run          — показать что будет применено
#   bash migrations/apply.sh --status           — показать статус всех миграций
#   bash migrations/apply.sh 20240101_001_*.sh  — применить конкретную
set -uo pipefail

MIGRATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="/opt/vpn/.migrations-applied"
DB_FILE="/opt/vpn/telegram-bot/data/vpn_bot.db"

DRY_RUN=false
SHOW_STATUS=false
SPECIFIC=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true; shift ;;
        --status)   SHOW_STATUS=true; shift ;;
        -h|--help)
            echo "Использование: $0 [--dry-run] [--status] [migration_file]"
            exit 0
            ;;
        *.sh|*.sql) SPECIFIC="$1"; shift ;;
        *) echo "Неизвестный параметр: $1"; exit 1 ;;
    esac
done

# Убедиться что STATE_FILE существует
touch "$STATE_FILE" 2>/dev/null || {
    echo "[apply.sh] Ошибка: не удалось создать $STATE_FILE"
    exit 1
}

# Показать статус
if $SHOW_STATUS; then
    echo "=== Статус миграций ==="
    echo "State file: $STATE_FILE"
    echo ""
    APPLIED=$(cat "$STATE_FILE" 2>/dev/null || true)
    for FILE in $(ls "$MIGRATIONS_DIR"/*.sh "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort); do
        NAME=$(basename "$FILE")
        [[ "$NAME" == "apply.sh" ]] && continue
        if echo "$APPLIED" | grep -q "^${NAME}$"; then
            echo "  [APPLIED] $NAME"
        else
            echo "  [PENDING] $NAME"
        fi
    done
    exit 0
fi

# Список миграций для применения
if [[ -n "$SPECIFIC" ]]; then
    MIGRATION_FILES=("$MIGRATIONS_DIR/$SPECIFIC")
else
    MIGRATION_FILES=($(ls "$MIGRATIONS_DIR"/*.sh "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort || true))
fi

APPLIED_COUNT=0
SKIPPED_COUNT=0
FAILED_COUNT=0

echo "[apply.sh] Применение миграций..."
$DRY_RUN && echo "[apply.sh] Режим dry-run: изменения не будут применены"

for FILE in "${MIGRATION_FILES[@]:-}"; do
    [[ -z "${FILE:-}" || ! -f "$FILE" ]] && continue

    NAME=$(basename "$FILE")
    [[ "$NAME" == "apply.sh" ]] && continue

    # Проверить применена ли уже
    if grep -qx "$NAME" "$STATE_FILE" 2>/dev/null; then
        SKIPPED_COUNT=$(( SKIPPED_COUNT + 1 ))
        continue
    fi

    echo "[apply.sh] Применение: $NAME"

    if $DRY_RUN; then
        echo "  [DRY-RUN] Пропускаем выполнение"
        continue
    fi

    # Применить миграцию
    EXIT_CODE=0
    case "$FILE" in
        *.sql)
            # SQL миграция — выполнить через sqlite3
            if [[ ! -f "$DB_FILE" ]]; then
                echo "  [SKIP] БД не найдена: $DB_FILE (бот ещё не инициализирован)"
                continue
            fi
            sqlite3 "$DB_FILE" < "$FILE" 2>/tmp/migration-error.log || EXIT_CODE=$?
            ;;
        *.sh)
            # Shell миграция
            bash "$FILE" 2>/tmp/migration-error.log || EXIT_CODE=$?
            ;;
    esac

    if [[ $EXIT_CODE -eq 0 ]]; then
        # Записать успешно применённую миграцию
        echo "$NAME" >> "$STATE_FILE"
        APPLIED_COUNT=$(( APPLIED_COUNT + 1 ))
        echo "  [OK] $NAME применена"
    else
        FAILED_COUNT=$(( FAILED_COUNT + 1 ))
        echo "  [FAIL] $NAME завершилась с ошибкой (код $EXIT_CODE)"
        cat /tmp/migration-error.log 2>/dev/null | head -20 >&2
        # Остановить при ошибке — не применять следующие миграции
        echo "[apply.sh] Остановка из-за ошибки. Следующие миграции не применены."
        break
    fi
done

echo ""
echo "[apply.sh] Итог: применено=$APPLIED_COUNT, пропущено=$SKIPPED_COUNT, ошибок=$FAILED_COUNT"

[[ $FAILED_COUNT -eq 0 ]] && exit 0 || exit 1

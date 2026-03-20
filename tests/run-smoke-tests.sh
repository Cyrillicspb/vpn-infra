#!/usr/bin/env bash
# Запуск smoke-тестов VPN-инфраструктуры
# Использование:
#   sudo bash run-smoke-tests.sh                 — все тесты
#   sudo bash run-smoke-tests.sh --test dns      — один тест
#   sudo bash run-smoke-tests.sh --quick         — без медленных тестов (blocked_sites)
#   sudo bash run-smoke-tests.sh --verbose       — подробный вывод каждого теста
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_DIR="$SCRIPT_DIR/smoke"

PASS=0; FAIL=0; WARN=0; SKIP=0
FAILED_TESTS=()
START_TIME=$(date +%s)

# Параметры
FILTER_TEST=""
QUICK_MODE=false
VERBOSE=false
TIMEOUT=60  # секунд на каждый тест

while [[ $# -gt 0 ]]; do
    case "$1" in
        --test)    FILTER_TEST="${2:-}"; shift 2 ;;
        --quick)   QUICK_MODE=true; shift ;;
        --verbose) VERBOSE=true; shift ;;
        --timeout) TIMEOUT="${2:-60}"; shift 2 ;;
        -h|--help)
            echo "Использование: $0 [--test <имя>] [--quick] [--verbose] [--timeout <сек>]"
            echo "Имена тестов: key_consistency, state_files, ssh_proxy, dns, tunnel, split, kill_switch, watchdog, bot, docker, blocked_sites"
            exit 0
            ;;
        *) echo "Неизвестный параметр: $1"; exit 1 ;;
    esac
done

# Все доступные тесты (в порядке выполнения)
declare -A TESTS=(
    ["dns"]="$SMOKE_DIR/test_dns.sh"
    ["split"]="$SMOKE_DIR/test_split.sh"
    ["kill_switch"]="$SMOKE_DIR/test_kill_switch.sh"
    ["tunnel"]="$SMOKE_DIR/test_tunnel.sh"
    ["watchdog"]="$SMOKE_DIR/test_watchdog.sh"
    ["docker"]="$SMOKE_DIR/test_docker.sh"
    ["bot"]="$SMOKE_DIR/test_bot.sh"
    ["blocked_sites"]="$SMOKE_DIR/test_blocked_sites.sh"
    ["key_consistency"]="$SMOKE_DIR/test_key_consistency.sh"
    ["state_files"]="$SMOKE_DIR/test_state_files.sh"
    ["ssh_proxy"]="$SMOKE_DIR/test_ssh_proxy.sh"
)

# Порядок выполнения
TEST_ORDER=(key_consistency state_files ssh_proxy dns split kill_switch tunnel watchdog docker bot blocked_sites)

# Медленные тесты (пропускаются в --quick режиме)
SLOW_TESTS=(blocked_sites)

# Цвета (только если tty)
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; RESET=''
fi

run_test() {
    local name="$1"
    local script="$2"

    # Пропустить медленные тесты в quick режиме
    if $QUICK_MODE; then
        for slow in "${SLOW_TESTS[@]}"; do
            if [[ "$name" == "$slow" ]]; then
                echo -e "  ${YELLOW}[SKIP]${RESET} $name (медленный тест, пропущен в --quick режиме)"
                (( SKIP++ ))
                return
            fi
        done
    fi

    # Пропустить если указан фильтр и тест не совпадает
    if [[ -n "$FILTER_TEST" && "$name" != "$FILTER_TEST" ]]; then
        return
    fi

    local log_file
    log_file=$(mktemp /tmp/vpn-test-XXXXXX.log)
    local exit_code=0

    # Запустить с таймаутом
    if timeout "$TIMEOUT" bash "$script" > "$log_file" 2>&1; then
        exit_code=0
    else
        exit_code=$?
        [[ $exit_code -eq 124 ]] && echo "TIMEOUT после ${TIMEOUT}s" >> "$log_file"
    fi

    if [[ $exit_code -eq 0 ]]; then
        echo -e "  ${GREEN}[PASS]${RESET} ${BOLD}$name${RESET}"
        (( PASS++ ))
    else
        echo -e "  ${RED}[FAIL]${RESET} ${BOLD}$name${RESET}"
        (( FAIL++ ))
        FAILED_TESTS+=("$name")
    fi

    # Показать предупреждения в любом случае
    if grep -q "\[WARN\]" "$log_file" 2>/dev/null; then
        grep "\[WARN\]" "$log_file" | while read -r line; do
            echo -e "         ${YELLOW}${line}${RESET}"
        done
    fi

    # Показать полный вывод при провале или в verbose режиме
    if [[ $exit_code -ne 0 ]] || $VERBOSE; then
        echo ""
        sed 's/^/         /' "$log_file"
        echo ""
    fi

    rm -f "$log_file"
}

# Заголовок
echo ""
echo -e "${BOLD}=== VPN Infrastructure Smoke Tests ===${RESET}"
echo -e "  Дата: $(date '+%Y-%m-%d %H:%M:%S')"
echo -e "  Хост: $(hostname)"
[[ -n "$FILTER_TEST" ]] && echo -e "  Фильтр: $FILTER_TEST"
$QUICK_MODE && echo -e "  Режим: быстрый (без медленных тестов)"
echo ""

# Запуск тестов в порядке
for test_name in "${TEST_ORDER[@]}"; do
    if [[ -f "${TESTS[$test_name]:-}" ]]; then
        run_test "$test_name" "${TESTS[$test_name]}"
    else
        if [[ -z "$FILTER_TEST" || "$FILTER_TEST" == "$test_name" ]]; then
            echo -e "  ${YELLOW}[SKIP]${RESET} $test_name (скрипт не найден: ${TESTS[$test_name]:-?})"
            (( SKIP++ ))
        fi
    fi
done

# Итог
END_TIME=$(date +%s)
DURATION=$(( END_TIME - START_TIME ))

echo ""
echo -e "${BOLD}=== Результаты ===${RESET}"
echo -e "  ${GREEN}Прошло:${RESET}   $PASS"
echo -e "  ${RED}Провалено:${RESET} $FAIL"
echo -e "  ${YELLOW}Предупреждений:${RESET} $WARN (в выводе тестов выше)"
[[ $SKIP -gt 0 ]] && echo -e "  Пропущено: $SKIP"
echo -e "  Время:    ${DURATION}s"

if [[ ${#FAILED_TESTS[@]} -gt 0 ]]; then
    echo ""
    echo -e "  ${RED}Провалены:${RESET} ${FAILED_TESTS[*]}"
fi

echo ""

if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}✓ Все тесты прошли успешно${RESET}"
    exit 0
else
    echo -e "${RED}${BOLD}✗ Тесты провалены: $FAIL${RESET}"
    echo "  Запустите с --verbose для подробного вывода"
    exit 1
fi

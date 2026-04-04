#!/usr/bin/env bash
# python-wheel-groups.sh — группы Python wheel bundle для офлайн-установки.

python_wheel_group_requirements_file() {
    local group="${1:-}"
    case "$group" in
        installer-gui) echo "installers/gui/requirements.txt" ;;
        watchdog) echo "home/watchdog/requirements.txt" ;;
        telegram-bot) echo "home/telegram-bot/requirements.txt" ;;
        *)
            echo "Unknown python wheel group requirements file: ${group}" >&2
            return 1
            ;;
    esac
}

python_wheel_group_extra_packages() {
    local group="${1:-}"
    case "$group" in
        installer-gui)
            cat <<'EOF'
virtualenv
pip
EOF
            ;;
        watchdog|telegram-bot)
            return 0
            ;;
        *)
            echo "Unknown python wheel group extra packages: ${group}" >&2
            return 1
            ;;
    esac
}

python_wheel_bundle_asset_name() {
    local group="${1:-}"
    case "$group" in
        installer-gui) echo "installer-gui-wheels.tar.gz" ;;
        watchdog) echo "watchdog-wheels.tar.gz" ;;
        telegram-bot) echo "telegram-bot-wheels.tar.gz" ;;
        *)
            echo "Unknown python wheel bundle group: ${group}" >&2
            return 1
            ;;
    esac
}

python_wheel_bundle_groups() {
    cat <<'EOF'
installer-gui
watchdog
telegram-bot
EOF
}

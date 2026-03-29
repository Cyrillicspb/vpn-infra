#!/usr/bin/env python3
"""
VPN Infrastructure TUI Installer
Запуск: python3 installer.py
Требования: Python 3.10+, textual (устанавливается автоматически)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Bootstrap: проверка textual ───────────────────────────────────────────────

MIN_PYTHON = (3, 10)
TEXTUAL_REQ = "textual>=0.47.0"


def _bootstrap() -> None:
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required. "
            f"Got: {sys.version_info.major}.{sys.version_info.minor}"
        )
    try:
        import textual  # noqa: F401
    except ImportError:
        sys.exit(
            "Модуль textual не установлен.\n"
            "Запустите setup.sh для консольной установки или установите textual заранее."
        )


_bootstrap()

# ── Путь к пакету (работает и из /opt/vpn и из /tmp) ─────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── Импорты (после bootstrap) ─────────────────────────────────────────────────
from textual.app import App
from textual.binding import Binding

from screens.welcome import WelcomeScreen
from state import InstallerState


# ── App ───────────────────────────────────────────────────────────────────────

class VPNInstallerApp(App):
    """TUI-установщик VPN Infrastructure v4.0."""

    TITLE = "VPN Infrastructure"
    SUB_TITLE = "v4.0 Installer"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Выход", priority=True, show=True),
        Binding("escape", "back", "Назад", show=True),
    ]

    CSS = """
    Screen {
        background: $surface;
    }
    Header {
        background: $primary-darken-2;
    }
    Footer {
        background: $primary-darken-2;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())

    def action_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb":
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    app = VPNInstallerApp()
    app.state = InstallerState.load()
    app.run()


if __name__ == "__main__":
    main()

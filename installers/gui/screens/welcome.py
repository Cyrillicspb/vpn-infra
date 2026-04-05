"""Экран приветствия — первый экран TUI."""
import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static
from textual.containers import Center, Middle, ScrollableContainer, Vertical


class WelcomeScreen(Screen):
    BINDINGS = [
        Binding("enter", "start", "Начать", show=False),
        Binding("space", "start", "Начать", show=False),
    ]

    CSS = """
    WelcomeScreen {
        align: center middle;
    }
    #welcome-box {
        width: 70;
        height: 1fr;
        max-height: 20;
        border: round $primary;
        padding: 1 3;
        background: $panel;
    }
    #welcome-title {
        text-align: center;
        margin-bottom: 1;
        color: $accent;
    }
    #welcome-body {
        height: 1fr;
        margin: 1 0;
    }
    #welcome-scroll {
        height: 1fr;
    }
    #btn-start {
        width: 100%;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        version = os.environ.get("VPN_INSTALL_VERSION", "").strip().lstrip("v")
        title = f"StackInfra v{version}" if version else "StackInfra"
        yield Header(show_clock=False)
        with Center():
            with Middle():
                with Vertical(id="welcome-box"):
                    yield Static(
                        f"[bold]{title}[/bold]",
                        id="welcome-title",
                    )
                    with ScrollableContainer(id="welcome-scroll"):
                        yield Static(
                            "[dim]Двухуровневая VPN с обходом DPI-фильтрации[/dim]\n"
                            "\n"
                            "  [cyan]●[/cyan] AmneziaWG + WireGuard (4 стека failover)\n"
                            "  [cyan]●[/cyan] Split tunneling + Kill switch + nfqws\n"
                            "  [cyan]●[/cyan] Telegram-бот для управления\n"
                            "  [cyan]●[/cyan] Установка: ~30–40 минут\n"
                            "\n"
                            "[yellow]Что потребуется:[/yellow]\n"
                            "  • Ubuntu 24.04 LTS (этот сервер)\n"
                            "  • VPS: IP-адрес + root пароль\n"
                            "  • Telegram Bot Token (от @BotFather)\n"
                            "  • Admin Chat ID (от @userinfobot)\n"
                            "\n"
                            "[red]⚠ Proxmox VM:[/red] отключите Secure Boot.\n"
                            "   Иначе модуль AmneziaWG (DKMS) не загрузится.\n",
                            id="welcome-body",
                        )
                    yield Button(
                        "Начать установку →",
                        variant="primary",
                        id="btn-start",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#btn-start", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self._go_next()

    def on_key(self, event) -> None:
        if event.key == "enter":
            self._go_next()

    def action_start(self) -> None:
        self._go_next()

    def _go_next(self) -> None:
        from screens.connection import ConnectionScreen
        self.app.push_screen(ConnectionScreen())

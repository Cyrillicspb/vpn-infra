"""Экран приветствия — первый экран TUI."""
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static
from textual.containers import Center, Middle, Vertical


class WelcomeScreen(Screen):
    CSS = """
    WelcomeScreen {
        align: center middle;
    }
    #welcome-box {
        width: 70;
        height: auto;
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
        margin: 1 0;
    }
    #btn-start {
        width: 100%;
        margin-top: 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Center():
            with Middle():
                with Vertical(id="welcome-box"):
                    yield Static(
                        "[bold]VPN Infrastructure v4.0[/bold]",
                        id="welcome-title",
                    )
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
                        "  • Admin Chat ID (от @userinfobot)\n",
                        id="welcome-body",
                    )
                    yield Button(
                        "Начать установку →",
                        variant="primary",
                        id="btn-start",
                    )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self._go_next()

    def on_key(self, event) -> None:
        if event.key == "enter":
            self._go_next()

    def _go_next(self) -> None:
        from screens.connection import ConnectionScreen
        self.app.push_screen(ConnectionScreen())

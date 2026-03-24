"""Экран ввода параметров установки."""
import re

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static


class ConfigureScreen(Screen):
    CSS = """
    ConfigureScreen {
        layout: vertical;
    }
    #form-scroll {
        height: 1fr;
    }
    #form-inner {
        width: 74;
        margin: 1 auto;
        padding: 1 2;
        border: round $primary;
    }
    .section-label {
        color: $accent;
        margin-top: 1;
        margin-bottom: 0;
    }
    .field-row {
        height: 3;
    }
    .field-label {
        width: 22;
        padding-top: 1;
        color: $text-muted;
    }
    .hint {
        height: 1;
        color: $text-muted;
        margin-left: 22;
        margin-bottom: 0;
    }
    #validation-msg {
        height: 2;
        text-align: center;
        margin-top: 1;
    }
    #btn-row {
        height: 3;
        margin-top: 1;
    }
    #btn-back { width: 14; }
    #btn-install { width: 1fr; }
    """

    def compose(self) -> ComposeResult:
        state = self.app.state
        yield Header(show_clock=False)

        with ScrollableContainer(id="form-scroll"):
            with Vertical(id="form-inner"):
                yield Static("[bold]Шаг 1/3 — Параметры установки[/bold]\n")

                # VPS section
                yield Static("[cyan]VPS (сервер в интернете):[/cyan]", classes="section-label")
                with Horizontal(classes="field-row"):
                    yield Label("IP адрес VPS:", classes="field-label")
                    yield Input(
                        value=state.vps_ip,
                        placeholder="1.2.3.4",
                        id="vps_ip",
                    )
                yield Static("IP-адрес вашего VPS (KVM/Vultr/Hetzner/DigitalOcean)", classes="hint")

                with Horizontal(classes="field-row"):
                    yield Label("SSH порт:", classes="field-label")
                    yield Input(
                        value=state.vps_ssh_port or "22",
                        placeholder="22",
                        id="vps_ssh_port",
                    )

                with Horizontal(classes="field-row"):
                    yield Label("root пароль VPS:", classes="field-label")
                    yield Input(
                        password=True,
                        placeholder="используется один раз, не сохраняется",
                        id="vps_root_password",
                    )
                yield Static(
                    "Нужен только для первоначальной установки SSH-ключа",
                    classes="hint",
                )

                # Telegram section
                yield Static("\n[cyan]Telegram Bot:[/cyan]", classes="section-label")
                with Horizontal(classes="field-row"):
                    yield Label("Bot Token:", classes="field-label")
                    yield Input(
                        password=True,
                        placeholder="1234567890:ABCDEFGabcdefg...",
                        id="telegram_token",
                    )
                yield Static("Получить: @BotFather → /newbot", classes="hint")

                with Horizontal(classes="field-row"):
                    yield Label("Admin Chat ID:", classes="field-label")
                    yield Input(
                        value=state.telegram_admin_chat_id,
                        placeholder="123456789",
                        id="telegram_admin_id",
                    )
                yield Static("Получить: отправьте /start боту @userinfobot", classes="hint")

                yield Static("", id="validation-msg")

                with Horizontal(id="btn-row"):
                    yield Button("← Назад", id="btn-back")
                    yield Button(
                        "Установить →",
                        variant="primary",
                        id="btn-install",
                        disabled=True,
                    )

        yield Footer()

    def on_mount(self) -> None:
        self._validate()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync(event.input.id, event.value)
        self._validate()

    def _sync(self, field_id: str, value: str) -> None:
        state = self.app.state
        mapping = {
            "vps_ip": "vps_ip",
            "vps_ssh_port": "vps_ssh_port",
            "vps_root_password": "vps_root_password",
            "telegram_token": "telegram_bot_token",
            "telegram_admin_id": "telegram_admin_chat_id",
        }
        if attr := mapping.get(field_id):
            setattr(state, attr, value)

    def _validate(self) -> bool:
        state = self.app.state
        errors: list[str] = []

        if not state.vps_ip:
            errors.append("IP адрес VPS обязателен")
        elif not re.match(r"^(?:\d{1,3}\.){3}\d{1,3}$", state.vps_ip):
            errors.append("Некорректный IP (ожидается: 1.2.3.4)")

        port = state.vps_ssh_port
        if not port or not port.isdigit() or not (1 <= int(port) <= 65535):
            errors.append("SSH порт — число от 1 до 65535")

        if not state.vps_root_password:
            errors.append("Пароль root VPS обязателен")

        if not state.telegram_bot_token:
            errors.append("Telegram Bot Token обязателен")
        elif not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", state.telegram_bot_token):
            errors.append("Формат токена: 123456789:ABCDEFGabcdefg... (≥35 символов после :)")

        if not state.telegram_admin_chat_id:
            errors.append("Admin Chat ID обязателен")
        elif not re.match(r"^-?\d+$", state.telegram_admin_chat_id):
            errors.append("Chat ID — целое число (может быть отрицательным для групп)")

        msg = self.query_one("#validation-msg", Static)
        btn = self.query_one("#btn-install", Button)

        if errors:
            msg.update(f"[red]⚠  {errors[0]}[/red]")
            btn.disabled = True
            return False

        msg.update("[green]✓  Всё готово к установке[/green]")
        btn.disabled = False
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-install" and not event.button.disabled:
            if self._validate():
                self.app.state.save()
                from screens.install import InstallScreen
                self.app.push_screen(InstallScreen())

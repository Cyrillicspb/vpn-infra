"""Экран 8/8 — Установка завершена: следующие шаги."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Button, Static

from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class DoneScreen(WizardScreen):
    STEP_NUM = 8
    STEP_TITLE = "Установка завершена"

    CSS = f"""
    DoneScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #done-box {{
        width: 74;
        margin: 1 auto;
        padding: 1 2;
        border: round $success;
        background: $panel;
    }}
    #done-title {{ text-align: center; color: $success; margin-bottom: 1; }}
    """

    def _compose_content(self) -> ComposeResult:
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="done-box"):
                yield Static(
                    "[bold green]✓  VPN Infrastructure установлена успешно![/bold green]",
                    id="done-title",
                )
                yield Static(
                    "\n"
                    "[bold]Следующие шаги:[/bold]\n\n"
                    "  1. Port Forwarding на роутере:\n"
                    "       UDP 51820 → этот сервер  (AmneziaWG)\n"
                    "       UDP 51821 → этот сервер  (WireGuard)\n\n"
                    "  2. В Telegram: напишите /start вашему боту\n\n"
                    "  3. /adddevice — получить WireGuard/AWG конфиг\n\n"
                    "[bold]Управление:[/bold]\n"
                    "  /help         — все команды бота\n"
                    "  /status       — статус системы\n"
                    "  /clients      — список клиентов\n\n"
                    "[bold]Диагностика:[/bold]\n"
                    "  journalctl -u watchdog -f\n"
                    "  journalctl -u vpn-bot -f\n\n"
                    "[dim]Первое подключение может занять 1–2 минуты\n"
                    "пока инициализируются все сервисы.[/dim]"
                )

    def on_mount(self) -> None:
        # No "Help" shown, Next becomes "Выйти"
        self._set_next_label("Выйти")
        self._set_next_enabled(True)
        # Hide "Back" — nothing to go back to after success
        self.query_one("#btn-back", Button).disabled = True

    def _on_next(self) -> None:
        self.app.exit()

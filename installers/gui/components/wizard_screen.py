"""Базовый класс для всех экранов мастера установки.

Layout: Header → step-bar (Шаг N/8) → content (1fr) → btn-row → Footer
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

# Общий CSS (ID-селекторы — работают во всех подклассах)
WIZARD_BASE_CSS = """
#step-bar {
    height: 1;
    padding: 0 2;
    background: $panel;
    color: $accent;
}
#wizard-content {
    height: 1fr;
}
.keyboard-hints {
    height: 1;
    color: $text-muted;
    background: $panel;
    text-align: center;
    padding: 0 1;
}
#wizard-btn-row {
    height: 3;
    padding: 0 2;
}
#btn-back { width: 12; }
#btn-help { width: 15; }
#btn-next { width: 1fr; margin-left: 1; }
"""

TOTAL_STEPS = 8


class WizardScreen(Screen):
    """Базовый экран мастера: header + content + footer с кнопками."""

    STEP_NUM: int = 0
    STEP_TITLE: str = ""
    HELP_TITLE: str = "Помощь"
    HELP_TEXT: str = ""

    BINDINGS = [
        Binding("pagedown", "scroll_down", "PgDn", show=True),
        Binding("pageup", "scroll_up", "PgUp", show=True),
        Binding("escape", "back", "Назад", show=True),
        Binding("question_mark", "help", "?", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            f"[bold]Шаг {self.STEP_NUM}/{TOTAL_STEPS} — {self.STEP_TITLE}[/bold]",
            id="step-bar",
        )
        yield from self._compose_content()
        yield Static(
            "Tab → поле | PgDn/PgUp скролл | Enter ✓ | ? помощь | ПКМ вставка",
            classes="keyboard-hints",
        )
        with Horizontal(id="wizard-btn-row"):
            yield Button("← Назад", id="btn-back")
            if self.HELP_TEXT:
                yield Button("? Помощь", id="btn-help")
            yield Button(
                "Далее →", variant="primary", id="btn-next", disabled=True
            )
        yield Footer()

    def _compose_content(self) -> ComposeResult:
        """Переопределить в подклассах для содержимого экрана."""
        return []

    # ── Обработчики кнопок ────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-help":
            from components.help_panel import HelpPanel
            self.app.push_screen(HelpPanel(self.HELP_TITLE, self.HELP_TEXT))
        elif event.button.id == "btn-next" and not event.button.disabled:
            self._on_next()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_help(self) -> None:
        if self.HELP_TEXT:
            from components.help_panel import HelpPanel
            self.app.push_screen(HelpPanel(self.HELP_TITLE, self.HELP_TEXT))

    def action_submit(self) -> None:
        btn = self.query_one("#btn-next", Button)
        if not btn.disabled:
            self._on_next()

    def action_scroll_down(self) -> None:
        try:
            self.query_one("#wizard-content", ScrollableContainer).scroll_page_down()
        except Exception:
            pass

    def action_scroll_up(self) -> None:
        try:
            self.query_one("#wizard-content", ScrollableContainer).scroll_page_up()
        except Exception:
            pass

    def _on_next(self) -> None:
        """Переопределить для обработки кнопки «Далее»."""

    # ── Вспомогательные методы ────────────────────────────────────────────

    def _set_next_enabled(self, enabled: bool) -> None:
        self.query_one("#btn-next", Button).disabled = not enabled

    def _set_next_label(self, label: str) -> None:
        self.query_one("#btn-next", Button).label = label

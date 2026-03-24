"""Модальная панель контекстной помощи."""
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class HelpPanel(ModalScreen):
    """Всплывающая панель помощи с текстом и кнопкой «Закрыть»."""

    DEFAULT_CSS = """
    HelpPanel {
        align: center middle;
    }
    #help-box {
        width: 62;
        height: auto;
        max-height: 80vh;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    #help-title {
        text-align: center;
        margin-bottom: 1;
        color: $accent;
    }
    #help-close {
        width: 100%;
        margin-top: 1;
    }
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Static(f"[bold]{self._title}[/bold]", id="help-title")
            yield Static(self._text)
            yield Button("Закрыть  [Esc]", id="help-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.dismiss()

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            self.dismiss()

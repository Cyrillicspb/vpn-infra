"""Составной виджет: Label + Input + hint + inline error."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Label, Static


class ValidatedInput(Widget):
    """Input-поле с меткой, подсказкой и строкой ошибки под ним."""

    DEFAULT_CSS = """
    ValidatedInput {
        height: auto;
        margin-bottom: 1;
    }
    ValidatedInput .vi-row {
        height: auto;
        min-height: 3;
    }
    ValidatedInput .vi-label {
        width: 22;
        padding-top: 1;
        color: $text-muted;
    }
    ValidatedInput Input {
        height: auto;
        min-height: 3;
    }
    ValidatedInput .vi-hint {
        height: 1;
        color: $text;
        margin-left: 22;
    }
    ValidatedInput .vi-error {
        height: 1;
        margin-left: 22;
    }
    """

    def __init__(
        self,
        label: str,
        *,
        input_id: str,
        placeholder: str = "",
        value: str = "",
        password: bool = False,
        hint: str = "",
    ) -> None:
        super().__init__()
        self._label = label
        self._input_id = input_id
        self._placeholder = placeholder
        self._value = value
        self._password = password
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Horizontal(classes="vi-row"):
            yield Label(self._label, classes="vi-label")
            yield Input(
                value=self._value,
                placeholder=self._placeholder,
                password=self._password,
                id=self._input_id,
            )
        if self._hint:
            yield Static(self._hint, classes="vi-hint")
        yield Static("", id=f"{self._input_id}-err", classes="vi-error")

    @property
    def value(self) -> str:
        return self.query_one(f"#{self._input_id}", Input).value

    def set_error(self, msg: str) -> None:
        self.query_one(f"#{self._input_id}-err", Static).update(
            f"[red]⚠ {msg}[/red]" if msg else ""
        )

    def clear_error(self) -> None:
        self.query_one(f"#{self._input_id}-err", Static).update("")

"""Экран 4/8 — Настройки Telegram-бота."""
from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Input, Static

from components.validated_input import ValidatedInput
from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class TelegramScreen(WizardScreen):
    STEP_NUM = 4
    STEP_TITLE = "Telegram Bot"
    HELP_TITLE = "Telegram Bot"
    HELP_TEXT = (
        "[bold]Bot Token[/bold]\n"
        "Откройте @BotFather в Telegram:\n"
        "  /newbot → введите имя → скопируйте токен\n"
        "  Формат: 1234567890:ABCDEFGabcdefg...\n\n"
        "[bold]Admin Chat ID[/bold]\n"
        "Вы становитесь root-администратором.\n"
        "Root-аккаунт неудаляем.\n\n"
        "Чтобы узнать свой Chat ID:\n"
        "  Напишите /start боту @userinfobot\n"
        "  Скопируйте число из ответа.\n"
        "  (Для групп ID отрицательный.)\n\n"
        "Добавить других администраторов можно\n"
        "позже через /admin invite в боте."
    )

    CSS = f"""
    TelegramScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #tg-form {{
        width: 74;
        margin: 1 2;
        padding: 1 2;
        border: round $primary;
    }}
    #tg-validation {{ height: 1; text-align: center; margin-top: 1; }}
    """

    def _compose_content(self) -> ComposeResult:
        state = self.app.state
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="tg-form"):
                yield Static("[bold]Telegram Bot:[/bold]\n")
                yield ValidatedInput(
                    "Bot Token:",
                    input_id="telegram_token",
                    password=True,
                    placeholder="1234567890:ABCDEFGabcdefg...",
                    hint="Получить: @BotFather → /newbot",
                )
                yield ValidatedInput(
                    "Admin Chat ID:",
                    input_id="telegram_admin_id",
                    placeholder="123456789",
                    value=state.telegram_admin_chat_id,
                    hint="Root-администратор. Неудаляем. Другие → /admin invite.",
                )
                yield Static("", id="tg-validation")

    def on_mount(self) -> None:
        self._validate()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync(event.input.id, event.value)
        self._validate()

    def _sync(self, field_id: str, value: str) -> None:
        if field_id == "telegram_token":
            self.app.state.telegram_bot_token = value
        elif field_id == "telegram_admin_id":
            self.app.state.telegram_admin_chat_id = value

    def _validate(self) -> bool:
        state = self.app.state
        errors: list[str] = []

        if not state.telegram_bot_token:
            errors.append("Bot Token обязателен")
        elif not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", state.telegram_bot_token):
            errors.append("Формат: 1234567890:ABCDEFGabcdefg... (≥35 символов после :)")

        if not state.telegram_admin_chat_id:
            errors.append("Admin Chat ID обязателен")
        elif not re.match(r"^-?\d+$", state.telegram_admin_chat_id):
            errors.append("Chat ID — целое число (может быть отрицательным для групп)")

        msg = self.query_one("#tg-validation", Static)
        if errors:
            msg.update(f"[red]⚠ {errors[0]}[/red]")
            self._set_next_enabled(False)
            return False

        msg.update("[green]✓ Telegram настроен[/green]")
        self._set_next_enabled(True)
        return True

    def _on_next(self) -> None:
        if self._validate():
            self.app.state.save()
            from screens.options import OptionsScreen
            self.app.push_screen(OptionsScreen())

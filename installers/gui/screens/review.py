"""Экран 6/8 — Обзор настроек перед установкой."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Button, Static

from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class ReviewScreen(WizardScreen):
    STEP_NUM = 6
    STEP_TITLE = "Обзор настроек"
    HELP_TITLE = "Обзор"
    HELP_TEXT = (
        "Проверьте все параметры перед запуском\n"
        "установки.\n\n"
        "Для изменения нажмите [← Назад] нужное\n"
        "количество раз.\n\n"
        "[bold]Установка:[/bold]\n"
        "  • Занимает 20–40 минут\n"
        "  • Не закрывайте терминал\n"
        "  • При ошибке — повторный запуск безопасен,\n"
        "    выполненные шаги пропустятся"
    )

    CSS = f"""
    ReviewScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #review-box {{
        width: 74;
        margin: 1 auto;
        padding: 1 2;
        border: round $primary;
        background: $panel;
    }}
    #review-warn {{ margin: 1 2; }}
    """

    def _compose_content(self) -> ComposeResult:
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="review-box"):
                yield Static("[bold]Параметры установки:[/bold]\n")
                yield Static("", id="review-content")
            yield Static("", id="review-warn")

    def on_mount(self) -> None:
        self._render_review()
        # Change Next button label
        self.query_one("#btn-next", Button).label = "Установить →"

    def _render_review(self) -> None:
        state = self.app.state
        mode_str = (
            "A — сервер на хостинге"
            if state.server_mode == "A"
            else "B — сервер дома за роутером"
        )
        cf_str = "Да" if state.use_cloudflare == "y" else "Нет"
        ddns_str = "Да" if state.use_ddns == "y" else "Нет"

        def _secret(val: str) -> str:
            return "●●●●●●●●" if val else "[red]не задан[/red]"

        lines = [
            "[cyan]VPS:[/cyan]",
            f"  IP:        {state.vps_ip or '[red]не задан[/red]'}",
            f"  SSH порт:  {state.vps_ssh_port or '22'}",
            f"  Пароль:    {_secret(state.vps_root_password)}",
            "",
            "[cyan]Telegram:[/cyan]",
            f"  Bot Token: {_secret(state.telegram_bot_token)}",
            f"  Admin ID:  {state.telegram_admin_chat_id or '[red]не задан[/red]'}",
            "",
            "[cyan]Сеть:[/cyan]",
            f"  Режим:     {mode_str}",
            f"  Интерфейс: {state.lan_iface or 'автоопределение'}",
            f"  LAN IP:    {state.lan_ip or 'автоопределение'}",
            "",
            "[cyan]Опции:[/cyan]",
            f"  Cloudflare CDN: {cf_str}",
            f"  DDNS:           {ddns_str}",
        ]
        if state.cgnat_detected:
            lines.append("")
            lines.append("[red]  ⚠ CGNAT обнаружен — могут быть проблемы[/red]")

        self.query_one("#review-content", Static).update("\n".join(lines))

        # Validation
        missing = []
        if not state.vps_ip:
            missing.append("VPS IP")
        if not state.vps_root_password:
            missing.append("пароль VPS")
        if not state.telegram_bot_token:
            missing.append("Telegram Token")
        if not state.telegram_admin_chat_id:
            missing.append("Admin Chat ID")

        warn = self.query_one("#review-warn", Static)
        if missing:
            warn.update(f"[red]⚠ Не заполнено: {', '.join(missing)}[/red]")
            self._set_next_enabled(False)
        elif state.cgnat_detected:
            warn.update(
                "[yellow]⚠ CGNAT обнаружен. Установка может не работать.[/yellow]\n"
                "   Нажмите «Установить» чтобы продолжить."
            )
            self._set_next_enabled(True)
        else:
            warn.update("[green]✓ Всё готово к установке[/green]")
            self._set_next_enabled(True)

    def _on_next(self) -> None:
        self.app.state.save()
        from screens.install import InstallScreen
        self.app.push_screen(InstallScreen())

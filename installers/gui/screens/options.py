"""Экран 5/8 — Дополнительные опции (Cloudflare CDN, DDNS)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Input, Label, Static

from components.validated_input import ValidatedInput
from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class OptionsScreen(WizardScreen):
    STEP_NUM = 5
    STEP_TITLE = "Дополнительные опции"
    HELP_TITLE = "Дополнительные опции"
    HELP_TEXT = (
        "[bold]Cloudflare CDN[/bold]\n"
        "Stack 1 (наиболее надёжный): VLESS+WS\n"
        "через Cloudflare. Трафик идёт через CDN\n"
        "— практически не блокируется.\n\n"
        "Требует:\n"
        "  • Домен (платный, ~$10/год)\n"
        "  • Аккаунт Cloudflare (бесплатно)\n"
        "  • Домен делегирован на Cloudflare DNS\n\n"
        "Без Cloudflare Stack 1 недоступен —\n"
        "система использует Stack 2/3/4.\n\n"
        "[bold]DDNS[/bold]\n"
        "Динамическое обновление DNS при смене IP.\n"
        "Нужен если у сервера нестабильный IP.\n"
        "Рекомендуется: DuckDNS (бесплатно).\n\n"
        "Обе опции можно включить позже вручную."
    )

    CSS = f"""
    OptionsScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #opt-form {{
        width: 74;
        margin: 1 2;
        padding: 1 2;
        border: round $primary;
    }}
    .opt-row {{ height: 3; margin-bottom: 0; }}
    .opt-label {{ width: 28; padding-top: 1; color: $text-muted; }}
    .opt-hint {{ height: 1; color: $text-muted; margin-left: 28; margin-bottom: 1; }}
    .toggle-btn {{ width: 8; }}
    .opt-details {{
        height: auto;
        margin-top: 1;
        padding: 1 2;
        border: round $primary-darken-2;
        background: $panel;
    }}
    .opt-details.hidden {{ display: none; }}
    """

    def _compose_content(self) -> ComposeResult:
        state = self.app.state
        cf_on = state.use_cloudflare == "y"
        ddns_on = state.use_ddns == "y"

        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="opt-form"):
                yield Static("[bold]Дополнительные опции:[/bold]\n")

                # ── Cloudflare CDN ────────────────────────────────────────
                with Horizontal(classes="opt-row"):
                    yield Label("Cloudflare CDN:", classes="opt-label")
                    yield Button(
                        "Да" if cf_on else "Нет",
                        id="btn-cf",
                        variant="success" if cf_on else "default",
                        classes="toggle-btn",
                    )
                yield Static(
                    "Stack 1 (VLESS+WS). Нужен домен + Cloudflare аккаунт",
                    classes="opt-hint",
                )
                with Vertical(
                    id="cf-details",
                    classes="opt-details" + ("" if cf_on else " hidden"),
                ):
                    yield ValidatedInput(
                        "Cloudflare Worker URL",
                        input_id="cf-worker-url",
                        placeholder="https://worker.example.workers.dev",
                        value=state.cf_worker_url,
                        hint="URL Cloudflare Worker для CDN-стека (опционально)",
                    )
                    yield ValidatedInput(
                        "Домен",
                        input_id="domain",
                        placeholder="vpn.example.com",
                        value=state.domain,
                        hint="Домен для сертификата Let's Encrypt",
                    )
                    yield ValidatedInput(
                        "Cloudflare API Token",
                        input_id="cf-api-token",
                        placeholder="",
                        value=state.cf_api_token,
                        hint="API токен Cloudflare для автообновления сертификата",
                        password=True,
                    )

                # ── DDNS ──────────────────────────────────────────────────
                with Horizontal(classes="opt-row"):
                    yield Label("DDNS:", classes="opt-label")
                    yield Button(
                        "Да" if ddns_on else "Нет",
                        id="btn-ddns",
                        variant="success" if ddns_on else "default",
                        classes="toggle-btn",
                    )
                yield Static(
                    "Обновление DNS при смене IP. Нужен DuckDNS или аналог",
                    classes="opt-hint",
                )
                with Vertical(
                    id="ddns-details",
                    classes="opt-details" + ("" if ddns_on else " hidden"),
                ):
                    yield ValidatedInput(
                        "DDNS провайдер",
                        input_id="ddns-provider",
                        placeholder="DuckDNS",
                        value=state.ddns_provider,
                        hint="DuckDNS, No-IP, Cloudflare (или пустое если статический IP)",
                    )
                    yield ValidatedInput(
                        "DDNS домен",
                        input_id="ddns-domain",
                        placeholder="myvpn.duckdns.org",
                        value=state.ddns_domain,
                        hint="Например: myvpn.duckdns.org",
                    )
                    yield ValidatedInput(
                        "DDNS токен",
                        input_id="ddns-token",
                        placeholder="",
                        value=state.ddns_token,
                        hint="API токен от DDNS провайдера",
                        password=True,
                    )

                yield Static(
                    "\n[dim]Все опции можно настроить позже вручную\n"
                    "через переменные окружения в /opt/vpn/.env[/dim]"
                )

    def on_mount(self) -> None:
        self._set_next_enabled(True)

    def on_input_changed(self, event: Input.Changed) -> None:
        state = self.app.state
        field_map = {
            "cf-worker-url": "cf_worker_url",
            "domain": "domain",
            "cf-api-token": "cf_api_token",
            "ddns-provider": "ddns_provider",
            "ddns-domain": "ddns_domain",
            "ddns-token": "ddns_token",
        }
        if event.input.id in field_map:
            setattr(state, field_map[event.input.id], event.value.strip())
            state.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cf":
            self._toggle("use_cloudflare", "btn-cf")
            if self.app.state.use_cloudflare == "y":
                self.query_one("#cf-details").remove_class("hidden")
            else:
                self.query_one("#cf-details").add_class("hidden")
        elif event.button.id == "btn-ddns":
            self._toggle("use_ddns", "btn-ddns")
            if self.app.state.use_ddns == "y":
                self.query_one("#ddns-details").remove_class("hidden")
            else:
                self.query_one("#ddns-details").add_class("hidden")
        else:
            super().on_button_pressed(event)

    def _toggle(self, attr: str, btn_id: str) -> None:
        current = getattr(self.app.state, attr)
        new_val = "n" if current == "y" else "y"
        setattr(self.app.state, attr, new_val)
        btn = self.query_one(f"#{btn_id}", Button)
        btn.label = "Да" if new_val == "y" else "Нет"
        btn.variant = "success" if new_val == "y" else "default"
        self.app.state.save()

    def _on_next(self) -> None:
        self.app.state.save()
        from screens.review import ReviewScreen
        self.app.push_screen(ReviewScreen())

"""Экран 5/8 — Дополнительные опции (Cloudflare CDN, DDNS/DuckDNS)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Input, Label, Static

from components.validated_input import ValidatedInput
from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen

_DUCKDNS_SUFFIX = ".duckdns.org"

_DUCKDNS_INSTRUCTIONS = (
    "[bold]Как настроить DuckDNS:[/bold]\n"
    "  1. Откройте duckdns.org в браузере\n"
    "  2. Войдите через GitHub, Google или Reddit\n"
    '  3. В поле "sub domain" введите имя → "add domain"\n'
    "  4. Скопируйте [bold]token[/bold] (UUID вверху страницы)\n"
    "  5. Введите субдомен и token ниже"
)

_CF_INSTRUCTIONS_A = (
    "[bold]Шаг A — Аккаунт Cloudflare[/bold]\n"
    "  1. Откройте dash.cloudflare.com/sign-up\n"
    "  2. Email + пароль → Create Account → подтвердите email\n"
    "     Бесплатно, карта не нужна."
)

_CF_INSTRUCTIONS_B = (
    "[bold]Шаг B — Создание Worker[/bold]\n"
    "  1. dash.cloudflare.com →\n"
    "     Workers & Pages → Create → Create Worker\n"
    "  2. Дайте любое имя → Deploy\n"
    "  3. Edit code → замените ВСЁ содержимое на код ниже\n"
    "  4. Save & Deploy\n"
    "  5. Скопируйте URL вида: xxx.workers.dev\n"
    "     Введите его в поле ниже (без https://)"
)


def _worker_code(vps_ip: str) -> str:
    ip = vps_ip or "ВАШ_VPS_IP"
    return (
        "export default {\n"
        "  async fetch(request) {\n"
        "    const url = new URL(request.url);\n"
        f'    const target = `http://{ip}:8080${{url.pathname}}${{url.search}}`;\n'
        "    const h = new Headers();\n"
        "    for (const [k,v] of request.headers)\n"
        "      if (k.toLowerCase() !== 'host') h.set(k,v);\n"
        f"    h.set('Host','{ip}');\n"
        "    return fetch(target,{method:request.method,\n"
        "      headers:h,body:request.body});\n"
        "  }\n"
        "}"
    )


class OptionsScreen(WizardScreen):
    STEP_NUM = 5
    STEP_TITLE = "Дополнительные опции"
    HELP_TITLE = "Дополнительные опции"
    HELP_TEXT = (
        "[bold]Cloudflare CDN[/bold]\n"
        "Stack 1 (наиболее надёжный): VLESS+WS\n"
        "через Cloudflare CDN.\n\n"
        "Заблокировать = заблокировать весь CF.\n"
        "Нужен только бесплатный аккаунт.\n"
        "Без него используются Stack 2/3/4.\n\n"
        "[bold]DDNS (DuckDNS)[/bold]\n"
        "Нужен если у роутера динамический IP.\n"
        "Бесплатно, без покупки домена.\n\n"
        "Создаёт субдомен: myserver.duckdns.org\n\n"
        "Обе опции можно настроить позже через\n"
        "/opt/vpn/.env"
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
    .cf-instructions {{
        height: auto;
        margin-bottom: 1;
        color: $text;
    }}
    .worker-code {{
        height: auto;
        margin: 1 0;
        padding: 1 2;
        background: #0d1117;
        color: $success;
    }}
    .ddns-instructions {{
        height: auto;
        margin-bottom: 1;
        color: $text;
    }}
    .subdomain-suffix {{
        height: 3;
        padding-top: 1;
        padding-left: 1;
        color: $text-muted;
        width: 14;
    }}
    """

    def _compose_content(self) -> ComposeResult:
        state = self.app.state
        cf_on = state.use_cloudflare == "y"
        ddns_on = state.use_ddns == "y"
        ddns_subdomain = (
            state.ddns_domain.removesuffix(_DUCKDNS_SUFFIX)
            if state.ddns_domain
            else ""
        )

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
                    "Stack 1 (VLESS+WS через Cloudflare CDN)",
                    classes="opt-hint",
                )
                with Vertical(
                    id="cf-details",
                    classes="opt-details" + ("" if cf_on else " hidden"),
                ):
                    yield Static(_CF_INSTRUCTIONS_A, classes="cf-instructions")
                    yield Static(_CF_INSTRUCTIONS_B, classes="cf-instructions")
                    yield Static(
                        _worker_code(state.vps_ip),
                        id="cf-worker-code",
                        classes="worker-code",
                    )
                    yield ValidatedInput(
                        "Worker hostname",
                        input_id="cf-cdn-hostname",
                        placeholder="xxx-xxx.account.workers.dev",
                        value=state.cf_cdn_hostname,
                        hint="Без https:// — только hostname",
                    )

                # ── DDNS (DuckDNS) ────────────────────────────────────────
                with Horizontal(classes="opt-row"):
                    yield Label("DDNS (DuckDNS):", classes="opt-label")
                    yield Button(
                        "Да" if ddns_on else "Нет",
                        id="btn-ddns",
                        variant="success" if ddns_on else "default",
                        classes="toggle-btn",
                    )
                yield Static(
                    "Нужен при динамическом IP роутера",
                    classes="opt-hint",
                )
                with Vertical(
                    id="ddns-details",
                    classes="opt-details" + ("" if ddns_on else " hidden"),
                ):
                    yield Static(_DUCKDNS_INSTRUCTIONS, classes="ddns-instructions")
                    with Horizontal(classes="vi-row"):
                        yield Label("Субдомен:", classes="opt-label")
                        yield Input(
                            value=ddns_subdomain,
                            placeholder="myserver",
                            id="ddns-subdomain",
                        )
                        yield Static(".duckdns.org", classes="subdomain-suffix")
                    yield Static(
                        "Имя без .duckdns.org",
                        classes="opt-hint",
                    )
                    yield ValidatedInput(
                        "DuckDNS Token",
                        input_id="ddns-token",
                        placeholder="a1b2c3d4-e5f6-...",
                        value=state.ddns_token,
                        hint="UUID токен с сайта duckdns.org",
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
        val = event.value.strip()
        if event.input.id == "cf-cdn-hostname":
            # убираем https:// если пользователь вставил полный URL
            hostname = val.removeprefix("https://").removeprefix("http://").rstrip("/")
            state.cf_cdn_hostname = hostname
            state.save()
        elif event.input.id == "ddns-subdomain":
            state.ddns_domain = (val + _DUCKDNS_SUFFIX) if val else ""
            state.save()
        elif event.input.id == "ddns-token":
            state.ddns_token = val

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

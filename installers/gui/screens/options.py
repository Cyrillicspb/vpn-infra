"""Экран 5/8 — Дополнительные опции (Cloudflare CDN, DDNS/DuckDNS)."""
from __future__ import annotations

import base64
import sys

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Input, Label, RichLog, Static

from components.validated_input import ValidatedInput
from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen

_DUCKDNS_SUFFIX = ".duckdns.org"


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


def _copy_to_clipboard_osc52(text: str) -> bool:
    """Пробует положить текст в clipboard терминала через OSC52."""
    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        seq = f"\033]52;c;{payload}\a"
        with open("/dev/tty", "w", encoding="utf-8", buffering=1) as tty:
            tty.write(seq)
            tty.flush()
        return True
    except Exception:
        try:
            sys.__stdout__.write(f"\033]52;c;{payload}\a")
            sys.__stdout__.flush()
            return True
        except Exception:
            return False


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
        "[bold]Как настроить Cloudflare Worker:[/bold]\n"
        "  1. dash.cloudflare.com/sign-up (бесплатно)\n"
        "  2. Workers & Pages → Create → Create Worker\n"
        "  3. Edit code → вставьте код ниже → Save & Deploy\n"
        "  4. Скопируйте URL (xxx.workers.dev) → вставьте ниже\n\n"
        "[bold]DDNS (DuckDNS)[/bold]\n"
        "Нужен если у роутера динамический IP.\n"
        "Бесплатно, без покупки домена.\n\n"
        "[bold]Как настроить DuckDNS:[/bold]\n"
        "  1. Откройте duckdns.org в браузере\n"
        "  2. Войдите через GitHub, Google или Reddit\n"
        '  3. В поле "sub domain" введите имя → "add domain"\n'
        "  4. Скопируйте [bold]token[/bold] (UUID вверху страницы)\n"
        "  5. Введите субдомен и token ниже\n\n"
        "Обе опции можно настроить позже через\n"
        "/opt/vpn/.env"
    )

    CSS = f"""
    OptionsScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #opt-form {{
        width: 1fr;
        max-width: 96;
        height: auto;
        margin: 1 2;
        padding: 1 2;
        border: round $primary;
    }}
    .opt-row {{ height: auto; min-height: 3; margin-bottom: 0; }}
    .vi-row {{ height: auto; min-height: 3; margin-bottom: 0; }}
    .opt-label {{ width: 20; padding-top: 1; color: $text-muted; }}
    .opt-hint {{ height: auto; color: $text-muted; margin-left: 20; margin-bottom: 1; }}
    .toggle-btn {{ width: 8; }}
    .toggle-btn.selected {{
        background: $success-darken-2;
        color: $text;
        border: tall $success;
        text-style: bold;
    }}
    .toggle-btn.selected:focus {{
        background: $success;
        color: $text;
    }}
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
    .cf-actions {{ height: auto; margin: 1 0; }}
    #btn-copy-worker {{ width: 1fr; }}
    #btn-toggle-worker {{ width: 24; margin-left: 1; }}
    #worker-copy-status {{
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }}
    #worker-code {{
        height: 4;
        margin: 1 0;
        border: round $success-darken-2;
        background: #0d1117;
    }}
    #worker-code.expanded {{ height: 12; }}
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
        width: 12;
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
                        classes="toggle-btn selected" if cf_on else "toggle-btn",
                    )
                yield Static(
                    "Stack 1 (VLESS+WS через Cloudflare CDN)",
                    classes="opt-hint",
                )
                with Vertical(
                    id="cf-details",
                    classes="opt-details" + ("" if cf_on else " hidden"),
                ):
                    yield Static(
                        "[dim]Workers & Pages → Create Worker → Edit code → вставьте код → Save & Deploy[/dim]",
                        classes="cf-instructions",
                    )
                    code_log = RichLog(
                        highlight=False, markup=False, wrap=False, id="worker-code"
                    )
                    yield code_log
                    with Horizontal(classes="cf-actions"):
                        yield Button(
                            "Скопировать код",
                            id="btn-copy-worker",
                            variant="primary",
                        )
                        yield Button(
                            "Показать код полностью",
                            id="btn-toggle-worker",
                        )
                    yield Static(
                        "[dim]В Termius обычно работает. Если вставка не сработала — откройте полный код и скопируйте вручную.[/dim]",
                        id="worker-copy-status",
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
                    yield Label("DDNS (home ingress):", classes="opt-label")
                    yield Button(
                        "Да" if ddns_on else "Нет",
                        id="btn-ddns",
                        variant="success" if ddns_on else "default",
                        classes="toggle-btn selected" if ddns_on else "toggle-btn",
                    )
                yield Static(
                    "Нужен для клиентского Endpoint при динамическом IP роутера",
                    classes="opt-hint",
                )
                with Vertical(
                    id="ddns-details",
                    classes="opt-details" + ("" if ddns_on else " hidden"),
                ):
                    yield Static(
                        "[dim]duckdns.org → войти → добавить субдомен → скопировать token (? для инструкций)[/dim]",
                        classes="ddns-instructions",
                    )
                    with Horizontal(classes="vi-row"):
                        yield Label("Субдомен:", classes="opt-label")
                        yield Input(
                            value=ddns_subdomain,
                            placeholder="myserver",
                            id="ddns-subdomain",
                        )
                        yield Static(".duckdns.org", classes="subdomain-suffix")
                    yield ValidatedInput(
                        "DuckDNS Token",
                        input_id="ddns-token",
                        placeholder="a1b2c3d4-e5f6-...",
                        value=state.ddns_token,
                        password=True,
                        hint="UUID токен с сайта duckdns.org",
                    )

                yield Static(
                    "\n[dim]Все опции можно настроить позже вручную\n"
                    "через переменные окружения в /opt/vpn/.env[/dim]"
                )

    def on_mount(self) -> None:
        self._validate()
        self._fill_worker_code()

    def _validate(self) -> None:
        state = self.app.state
        ok = True
        if state.use_ddns == "y" and not state.ddns_token:
            ok = False
        if state.use_ddns == "y" and not state.ddns_domain:
            ok = False
        if state.use_cloudflare == "y" and not state.cf_cdn_hostname:
            ok = False
        self._set_next_enabled(ok)

    def _fill_worker_code(self) -> None:
        try:
            log = self.query_one("#worker-code", RichLog)
            log.clear()
            for line in _worker_code(self.app.state.vps_ip).splitlines():
                log.write(line)
        except Exception:
            pass

    def _set_worker_status(self, message: str) -> None:
        try:
            self.query_one("#worker-copy-status", Static).update(message)
        except Exception:
            pass

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
        self._validate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cf":
            self._toggle("use_cloudflare", "btn-cf")
            if self.app.state.use_cloudflare == "y":
                self.query_one("#cf-details").remove_class("hidden")
            else:
                self.query_one("#cf-details").add_class("hidden")
        elif event.button.id == "btn-copy-worker":
            ok = _copy_to_clipboard_osc52(_worker_code(self.app.state.vps_ip))
            if ok:
                self._set_worker_status("[green]Код отправлен в буфер терминала через OSC52.[/green]")
            else:
                self._set_worker_status("[yellow]Не удалось скопировать автоматически. Нажмите «Показать код полностью» и скопируйте вручную.[/yellow]")
        elif event.button.id == "btn-toggle-worker":
            code_log = self.query_one("#worker-code", RichLog)
            btn = self.query_one("#btn-toggle-worker", Button)
            if code_log.has_class("expanded"):
                code_log.remove_class("expanded")
                btn.label = "Показать код полностью"
            else:
                code_log.add_class("expanded")
                btn.label = "Свернуть код"
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
        btn.set_class(new_val == "y", "selected")
        self.app.state.save()
        self._validate()

    def _on_next(self) -> None:
        self.app.state.save()
        from screens.review import ReviewScreen
        self.app.push_screen(ReviewScreen())

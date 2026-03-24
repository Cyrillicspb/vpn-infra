"""Экран 2/8 — Автодетект сети + выбор режима A/B."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Input, Label, RichLog, Static

from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class NetworkScreen(WizardScreen):
    STEP_NUM = 2
    STEP_TITLE = "Конфигурация сети"
    HELP_TITLE = "Режимы работы"
    HELP_TEXT = (
        "[bold]Режим A — Сервер на хостинге[/bold]\n"
        "Сервер имеет реальный публичный IP-адрес\n"
        "(VPS, dedic, colocation).\n"
        "Клиенты подключаются напрямую к нему.\n\n"
        "[bold]Режим B — Сервер дома за роутером[/bold]\n"
        "Сервер дома, роутер имеет реальный IP.\n"
        "Нужен port forward: UDP 51820 + 51821.\n"
        "Клиенты используют IP роутера.\n\n"
        "[bold]CGNAT[/bold]\n"
        "Провайдер выдаёт серый IP из диапазона\n"
        "100.64.0.0/10 — ни один режим не работает.\n"
        "Нужна смена тарифа или VPS."
    )

    CSS = f"""
    NetworkScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #net-info {{
        height: auto;
        margin: 1 2;
        padding: 1 2;
        border: round $primary-darken-2;
        background: $panel;
    }}
    #mode-row {{
        height: 5;
        margin: 1 2;
    }}
    #btn-mode-a, #btn-mode-b {{
        width: 1fr;
        height: 5;
    }}
    #gateway-row {{
        height: auto;
        margin: 0 2;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }}
    #gateway-row.hidden {{
        display: none;
    }}
    #net-log {{
        height: 6;
        margin: 0 2 1 2;
        border: round $primary-darken-2;
        background: #0d1117;
    }}
    """

    def _compose_content(self) -> ComposeResult:
        state = self.app.state
        mode = state.server_mode
        hidden_cls = "" if mode == "B" else "hidden"
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="net-info"):
                yield Static("[bold]Детект сети:[/bold]\n")
                yield Static("", id="net-iface")
                yield Static("", id="net-local-ip")
                yield Static("", id="net-cgnat")
            with Horizontal(id="mode-row"):
                yield Button(
                    "[A] Сервер на хостинге\n    (публичный IP)",
                    id="btn-mode-a",
                    variant="success" if mode == "A" else "default",
                )
                yield Button(
                    "[B] Сервер дома\n    за роутером",
                    id="btn-mode-b",
                    variant="success" if mode == "B" else "default",
                )
            with Vertical(id="gateway-row", classes=hidden_cls):
                yield Label("Внешний IP роутера (для HAIRPIN NAT):")
                yield Input(
                    value=state.router_external_ip,
                    placeholder="например: 1.2.3.4",
                    id="input-router-ip",
                )
            yield RichLog(highlight=False, markup=False, wrap=True, id="net-log")

    def on_mount(self) -> None:
        state = self.app.state
        if state.lan_iface:
            self.query_one("#net-iface", Static).update(
                f"  Интерфейс:  {state.lan_iface}"
            )
        if state.lan_ip:
            self.query_one("#net-local-ip", Static).update(
                f"  Локальный IP: {state.lan_ip}"
            )
        self._set_next_enabled(True)
        self.call_after_refresh(self._detect_cgnat)

    async def _detect_cgnat(self) -> None:
        log = self.query_one("#net-log", RichLog)
        log.write("Проверка CGNAT...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sf", "--max-time", "8", "https://icanhazip.com",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                ext_ip = out.decode().strip()
                cgnat = self._is_cgnat(ext_ip)
                self.app.state.cgnat_detected = cgnat
                if cgnat:
                    self.query_one("#net-cgnat", Static).update(
                        f"  [red]⚠ CGNAT: {ext_ip} — белый IP не обнаружен[/red]"
                    )
                    log.write(f"⚠ CGNAT обнаружен: {ext_ip} из 100.64.0.0/10")
                    log.write("  Нужен белый IP или смена провайдера.")
                else:
                    self.query_one("#net-cgnat", Static).update(
                        f"  [green]✓ Белый IP: {ext_ip}[/green]"
                    )
                    log.write(f"✓ Внешний IP: {ext_ip}")
                self.app.state.save()
            else:
                log.write("[WARN] Не удалось определить внешний IP")
        except Exception as e:
            log.write(f"[WARN] {e}")

    @staticmethod
    def _is_cgnat(ip: str) -> bool:
        try:
            parts = [int(x) for x in ip.split(".")]
            return parts[0] == 100 and 64 <= parts[1] <= 127
        except Exception:
            return False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "input-router-ip":
            self.app.state.router_external_ip = event.value.strip()
            self.app.state.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        log = self.query_one("#net-log", RichLog)
        if event.button.id == "btn-mode-a":
            self.app.state.server_mode = "A"
            self.query_one("#btn-mode-a", Button).variant = "success"
            self.query_one("#btn-mode-b", Button).variant = "default"
            self.query_one("#gateway-row").add_class("hidden")
            self.app.state.save()
        elif event.button.id == "btn-mode-b":
            self.app.state.server_mode = "B"
            self.query_one("#btn-mode-a", Button).variant = "default"
            self.query_one("#btn-mode-b", Button).variant = "success"
            self.query_one("#gateway-row").remove_class("hidden")
            self.app.state.save()
            log.write("ВНИМАНИЕ: Gateway Mode — сервер станет SPOF для LAN-сети")
            log.write("  Настройте UPS и port forwarding на роутере (UDP 51820/51821)")
        else:
            super().on_button_pressed(event)

    def _on_next(self) -> None:
        from screens.vps import VpsScreen
        self.app.push_screen(VpsScreen())

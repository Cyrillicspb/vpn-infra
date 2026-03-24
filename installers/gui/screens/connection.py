"""Экран 1/8 — Подключение: информация о сервере, тест интернета."""
from __future__ import annotations

import asyncio
import socket

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Button, RichLog, Static

from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class ConnectionScreen(WizardScreen):
    STEP_NUM = 1
    STEP_TITLE = "Подключение"
    HELP_TITLE = "Подключение"
    HELP_TEXT = (
        "TUI-установщик работает прямо на вашем\n"
        "сервере — клиент только подключается через\n"
        "SSH и видит этот интерфейс.\n\n"
        "Этот экран показывает сетевые параметры\n"
        "сервера и проверяет доступ в интернет.\n\n"
        "[Проверить] — тест связи с интернетом.\n\n"
        "Если интернет недоступен — проверьте\n"
        "сетевые настройки и повторите."
    )

    CSS = f"""
    ConnectionScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #conn-info {{
        height: auto;
        margin: 1 2;
        padding: 1 2;
        border: round $primary-darken-2;
        background: $panel;
    }}
    #conn-log {{
        height: 8;
        margin: 0 2 1 2;
        border: round $primary-darken-2;
        background: #0d1117;
    }}
    #btn-check {{ margin: 0 2 1 2; width: 30; }}
    """

    def _compose_content(self) -> ComposeResult:
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="conn-info"):
                yield Static("[bold]Информация о сервере:[/bold]\n")
                yield Static("", id="conn-hostname")
                yield Static("", id="conn-ip-local")
                yield Static("", id="conn-ip-ext")
            yield Button("⟳ Проверить подключение", id="btn-check")
            yield RichLog(highlight=False, markup=False, wrap=True, id="conn-log")

    def on_mount(self) -> None:
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "неизвестен"
        self.query_one("#conn-hostname", Static).update(f"  Хост:       {hostname}")
        # Restore from state if already detected
        state = self.app.state
        if state.lan_iface:
            self.query_one("#conn-ip-local", Static).update(
                f"  LAN:        {state.lan_ip}  (iface: {state.lan_iface})"
            )
        self.call_after_refresh(self._detect)

    async def _detect(self) -> None:
        log = self.query_one("#conn-log", RichLog)
        log.write("Определение сетевых параметров...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ip", "route", "get", "1.1.1.1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            line = out.decode().strip()
            iface, local_ip = "", ""
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "dev" and i + 1 < len(parts):
                    iface = parts[i + 1]
                if p == "src" and i + 1 < len(parts):
                    local_ip = parts[i + 1]
            if iface or local_ip:
                self.app.state.lan_iface = iface
                self.app.state.lan_ip = local_ip
                self.query_one("#conn-ip-local", Static).update(
                    f"  LAN:        {local_ip}  (iface: {iface})"
                )
                log.write(f"Интерфейс: {iface}, локальный IP: {local_ip}")
        except Exception as e:
            log.write(f"[WARN] ip route: {e}")

        await self._check_internet(log)

    async def _check_internet(self, log: RichLog | None = None) -> None:
        if log is None:
            log = self.query_one("#conn-log", RichLog)
        log.write("Проверка интернет-соединения...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sf", "--max-time", "8", "https://icanhazip.com",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                ext_ip = out.decode().strip()
                self.query_one("#conn-ip-ext", Static).update(
                    f"  Внешний IP: [bold green]{ext_ip}[/bold green]"
                )
                log.write(f"✓ Интернет доступен. Внешний IP: {ext_ip}")
            else:
                log.write("✗ curl вернул ошибку — проверьте интернет-соединение.")
        except Exception as e:
            log.write(f"[WARN] {e}")
        finally:
            self._set_next_enabled(True)
            self.app.state.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-check":
            log = self.query_one("#conn-log", RichLog)
            log.clear()
            self.call_after_refresh(self._check_internet)
        else:
            super().on_button_pressed(event)

    def _on_next(self) -> None:
        from screens.network import NetworkScreen
        self.app.push_screen(NetworkScreen())

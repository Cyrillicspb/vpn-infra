"""Экран 3/8 — Настройки VPS."""
from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Button, Input, RichLog, Static

from components.validated_input import ValidatedInput
from components.wizard_screen import WIZARD_BASE_CSS, WizardScreen


class VpsScreen(WizardScreen):
    STEP_NUM = 3
    STEP_TITLE = "Настройки VPS"
    HELP_TITLE = "VPS"
    HELP_TEXT = (
        "[bold]VPS (Virtual Private Server)[/bold]\n"
        "Сервер в интернете для ретрансляции\n"
        "зашифрованного трафика.\n\n"
        "[bold]IP-адрес[/bold]\n"
        "Публичный IPv4 вашего VPS.\n"
        "Рекомендуются: Hetzner, Vultr, DigitalOcean.\n"
        "⚠ Не используйте VPS с IP от Google/Amazon —\n"
        "  они часто блокируются Роскомнадзором.\n\n"
        "[bold]SSH-порт[/bold]\n"
        "Обычно 22. Смените для безопасности.\n\n"
        "[bold]Пароль root[/bold]\n"
        "Нужен только для первого входа и установки\n"
        "SSH-ключа. После — только ключ.\n"
        "Не сохраняется на диск."
    )

    CSS = f"""
    VpsScreen {{ layout: vertical; }}
    {WIZARD_BASE_CSS}
    #vps-form {{
        width: 74;
        margin: 1 2;
        padding: 1 2;
        border: round $primary;
    }}
    #vps-validation {{ height: 1; text-align: center; margin-top: 1; }}
    #vps-log {{
        height: 5;
        margin: 0 2 1 2;
        border: round $primary-darken-2;
        background: #0d1117;
    }}
    #btn-check-vps {{ margin-bottom: 1; }}
    """

    def _compose_content(self) -> ComposeResult:
        state = self.app.state
        with ScrollableContainer(id="wizard-content"):
            with Vertical(id="vps-form"):
                yield Static("[bold]VPS (сервер в интернете):[/bold]\n")
                yield ValidatedInput(
                    "IP адрес VPS:",
                    input_id="vps_ip",
                    placeholder="1.2.3.4",
                    value=state.vps_ip,
                    hint="Hetzner / Vultr / DigitalOcean",
                )
                yield ValidatedInput(
                    "SSH порт:",
                    input_id="vps_ssh_port",
                    placeholder="22",
                    value=state.vps_ssh_port or "22",
                )
                yield ValidatedInput(
                    "root пароль VPS:",
                    input_id="vps_root_password",
                    placeholder="используется один раз, не сохраняется",
                    password=True,
                    hint="Нужен только для первоначальной установки SSH-ключа",
                )
                yield Button("⟳ Проверить root SSH к VPS", id="btn-check-vps")
                yield Static("", id="vps-validation")
            yield RichLog(highlight=False, markup=False, wrap=True, id="vps-log")

    def on_mount(self) -> None:
        self._validate()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync(event.input.id, event.value)
        self._validate()

    def _sync(self, field_id: str, value: str) -> None:
        mapping = {
            "vps_ip": "vps_ip",
            "vps_ssh_port": "vps_ssh_port",
            "vps_root_password": "vps_root_password",
        }
        if attr := mapping.get(field_id):
            setattr(self.app.state, attr, value)

    def _validate(self) -> bool:
        state = self.app.state
        errors: list[str] = []

        if not state.vps_ip:
            errors.append("IP адрес VPS обязателен")
        elif not re.match(r"^(?:\d{1,3}\.){3}\d{1,3}$", state.vps_ip):
            errors.append("Некорректный IP (ожидается: 1.2.3.4)")

        port = state.vps_ssh_port
        if not port or not port.isdigit() or not (1 <= int(port) <= 65535):
            errors.append("SSH порт — число от 1 до 65535")

        if not state.vps_root_password:
            errors.append("Пароль root VPS обязателен")

        msg = self.query_one("#vps-validation", Static)
        if errors:
            msg.update(f"[red]⚠ {errors[0]}[/red]")
            self._set_next_enabled(False)
            return False

        msg.update("[green]✓[/green]")
        self._set_next_enabled(True)
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-check-vps":
            self.call_after_refresh(self._check_vps)
        else:
            super().on_button_pressed(event)

    @staticmethod
    def _probe_root_ssh(ip: str, port: int, password: str) -> tuple[bool, str]:
        """
        Реальная проверка root SSH по паролю.
        Используем временный SSH_ASKPASS helper, чтобы не зависеть от sshpass.
        """
        with tempfile.TemporaryDirectory(prefix="vpn-installer-ssh-") as tmpdir:
            askpass = Path(tmpdir) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$VPN_INSTALLER_SSH_PASSWORD\"\n",
                encoding="utf-8",
            )
            askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            env = os.environ.copy()
            env["SSH_ASKPASS"] = str(askpass)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["VPN_INSTALLER_SSH_PASSWORD"] = password
            env["DISPLAY"] = "vpn-installer:0"

            cmd = [
                "setsid",
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "NumberOfPasswordPrompts=1",
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-p",
                str(port),
                f"root@{ip}",
                "exit 0",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=env,
                )
            except FileNotFoundError as exc:
                return False, f"Не найдено: {exc.filename}"
            except subprocess.TimeoutExpired:
                return False, "Timeout (15 с) — SSH не ответил вовремя"

        if proc.returncode == 0:
            return True, f"✓ root@{ip}:{port} — авторизация успешна"

        details = (proc.stderr or proc.stdout or "").strip()
        if "Permission denied" in details:
            return False, "✗ Неверный пароль root или вход по паролю запрещён"
        if "Connection refused" in details:
            return False, f"✗ {ip}:{port} — SSH-порт отклоняет соединение"
        if "No route to host" in details:
            return False, f"✗ Нет маршрута до {ip}:{port}"
        if "Connection timed out" in details:
            return False, f"✗ Timeout — {ip}:{port} не отвечает"
        if details:
            return False, f"✗ {details}"
        return False, "✗ SSH-проверка не прошла"

    async def _check_vps(self) -> None:
        """Проверка реальной SSH-аутентификации root по паролю."""
        log = self.query_one("#vps-log", RichLog)
        state = self.app.state
        if not self._validate():
            log.write("[WARN] Сначала заполните IP, порт и root пароль")
            return

        ip = state.vps_ip
        try:
            port = int(state.vps_ssh_port or "22")
        except ValueError:
            log.write("[WARN] Некорректный порт")
            return
        log.write(f"Проверка root SSH {ip}:{port}...")
        ok, message = await asyncio.to_thread(
            self._probe_root_ssh,
            ip,
            port,
            state.vps_root_password,
        )
        log.write(message)
        if not ok:
            log.write("  Проверьте IP, SSH-порт, root пароль и разрешён ли password login.")

    def _on_next(self) -> None:
        if self._validate():
            self.app.state.save()
            from screens.telegram import TelegramScreen
            self.app.push_screen(TelegramScreen())

"""Экран установки — запускает setup.sh и отображает прогресс."""
import asyncio
import os
import re
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static


def _find_setup_sh() -> Path:
    """Ищет setup.sh: сначала /opt/vpn/, затем рядом с репозиторием."""
    candidate = Path("/opt/vpn/setup.sh")
    if candidate.exists():
        return candidate
    # Запуск из репозитория (разработка): installers/gui/screens/ → ../../..
    return Path(__file__).resolve().parents[3] / "setup.sh"


SETUP_SH = _find_setup_sh()
# Regex для парсинга маркеров прогресса из common.sh
_RE_PROGRESS = re.compile(r"^##PROGRESS:(\d+):(\d+):([^:]+):(\w*)$")
# Regex для очистки ANSI escape-кодов
_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[mKHJAB]")


class InstallScreen(Screen):
    CSS = """
    InstallScreen {
        layout: vertical;
    }
    #install-header-row {
        height: 1;
        padding: 0 2;
        background: $panel;
    }
    #install-status {
        height: 1;
        margin: 1 2 0 2;
    }
    #install-progress {
        margin: 0 2;
    }
    #install-step {
        height: 1;
        margin: 0 2;
        color: $text-muted;
    }
    #install-log {
        height: 1fr;
        margin: 1 2;
        border: round $primary-darken-2;
        background: #0d1117;
    }
    #btn-row {
        height: 3;
        padding: 0 2;
    }
    #btn-run  { width: 22; }
    #btn-done { width: 1fr; }
    """

    _running: bool = False
    _proc = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            "[bold]Шаг 7/8 — Установка[/bold]   [dim]Не закрывайте окно[/dim]",
            id="install-header-row",
        )
        yield Static("Готов к установке.", id="install-status")
        yield ProgressBar(total=61, show_eta=False, id="install-progress")
        yield Static("", id="install-step")
        yield RichLog(
            highlight=False,
            markup=False,
            wrap=True,
            id="install-log",
        )
        with Horizontal(id="btn-row"):
            yield Button("▶  Установить", variant="primary", id="btn-run")
            yield Button("✓  Готово", variant="success", id="btn-done", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#install-log", RichLog)
        state = self.app.state

        if not SETUP_SH.exists():
            self.query_one("#install-status", Static).update(
                f"[red]setup.sh не найден: {SETUP_SH}[/red]"
            )
            self.query_one("#btn-run", Button).disabled = True
            return

        log.write(f"=== VPN Infrastructure Installer ===")
        log.write(f"setup.sh : {SETUP_SH}")
        log.write(f"VPS      : {state.vps_ip}:{state.vps_ssh_port}")
        log.write(f"Admin ID : {state.telegram_admin_chat_id}")
        log.write("")
        log.write("[WARN] Установка займёт 20–40 минут.")
        log.write("[INFO] Нажмите «Установить» для начала.")

    async def _run(self) -> None:
        if self._running:
            return
        self._running = True

        btn_run = self.query_one("#btn-run", Button)
        btn_done = self.query_one("#btn-done", Button)
        status = self.query_one("#install-status", Static)
        step_lbl = self.query_one("#install-step", Static)
        pbar = self.query_one("#install-progress", ProgressBar)
        log = self.query_one("#install-log", RichLog)

        btn_run.disabled = True
        btn_run.label = "Установка..."

        cmd = (
            ["bash", str(SETUP_SH)]
            if os.getuid() == 0
            else ["sudo", "-E", "bash", str(SETUP_SH)]
        )
        env = {**os.environ, **self.app.state.to_env()}

        log.write("")
        log.write(f"$ {' '.join(cmd[:2])} {SETUP_SH.name}")
        log.write("-" * 50)

        last_step_name: str = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._proc = proc

            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()

                m = _RE_PROGRESS.match(line)
                if m:
                    current, total, name, flag = (
                        int(m[1]), int(m[2]), m[3], m[4]
                    )
                    if pbar.total != total:
                        pbar.total = total

                    if flag in ("done", "skip", ""):
                        pbar.advance(1)
                        self.app.state.current_step = current
                        icon = "✓" if flag != "skip" else "→"
                        step_lbl.update(f"  {icon} {name}")
                        status.update(f"Шаг {current}/{total}")
                    elif flag == "start":
                        last_step_name = name
                        status.update(f"Шаг {current}/{total}: {name[:55]}")
                        step_lbl.update(f"  ⟳ {name}")
                    continue

                clean = _RE_ANSI.sub("", line).strip()
                if clean:
                    log.write(clean)

            await proc.wait()
            rc = proc.returncode

            if rc == 0:
                pbar.update(progress=pbar.total)
                status.update("[bold green]✓  Установка завершена успешно![/bold green]")
                step_lbl.update("")
                self.app.state.setup_completed = True
                self.app.state.save()
                log.write("")
                log.write("=" * 50)
                log.write("Установка завершена!")
                btn_done.disabled = False
            else:
                fail_info = f" на шаге: {last_step_name}" if last_step_name else ""
                status.update(f"[bold red]✗  Ошибка (код {rc}){fail_info}[/bold red]")
                step_lbl.update(f"  [red]✗ {last_step_name}[/red]" if last_step_name else "")
                log.write("")
                log.write(f"[ERR] setup.sh завершился с кодом {rc}{fail_info}")
                log.write("Повтор безопасен — выполненные шаги пропустятся.")
                btn_run.label = "▶  Повторить"
                btn_run.disabled = False

        except PermissionError:
            status.update("[red]Нет прав sudo (нужен NOPASSWD)[/red]")
            log.write("[ERR] sudo не настроен или требует пароль.")
            btn_run.label = "▶  Повторить"
            btn_run.disabled = False
        except FileNotFoundError as e:
            status.update(f"[red]Команда не найдена: {e}[/red]")
            log.write(f"[ERR] {e}")
            btn_run.label = "▶  Повторить"
            btn_run.disabled = False
        except Exception as e:
            status.update(f"[red]Ошибка: {e}[/red]")
            log.write(f"[ERR] {e}")
            btn_run.label = "▶  Повторить"
            btn_run.disabled = False
        finally:
            self._running = False
            self._proc = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-run" and not event.button.disabled:
            self._running = False
            self.run_worker(self._run(), exclusive=True)
        elif event.button.id == "btn-done":
            from screens.done import DoneScreen
            self.app.push_screen(DoneScreen())

    def _show_next_steps(self) -> None:
        log = self.query_one("#install-log", RichLog)
        log.write("")
        log.write("╔══════════════════════════════════════════╗")
        log.write("║          Следующие шаги                  ║")
        log.write("╠══════════════════════════════════════════╣")
        log.write("║  1. Port Forwarding на роутере:          ║")
        log.write("║     UDP 51820 → сервер (AmneziaWG)      ║")
        log.write("║     UDP 51821 → сервер (WireGuard)      ║")
        log.write("║                                          ║")
        log.write("║  2. Telegram → /start вашему боту       ║")
        log.write("║  3. /adddevice → получить конфиг         ║")
        log.write("║                                          ║")
        log.write("║  Управление:  Telegram /help             ║")
        log.write("║  Логи:        journalctl -u watchdog -f  ║")
        log.write("╚══════════════════════════════════════════╝")

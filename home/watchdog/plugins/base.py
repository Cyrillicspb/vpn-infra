"""Базовый класс для плагинов VPN-стеков."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SYSTEMD_NOTIFY_ENV_KEYS = ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")
PROCESS_LOG_DIR = Path("/var/log/vpn")


def child_env() -> dict[str, str]:
    """Среда для дочерних процессов без sd_notify переменных systemd."""
    env = os.environ.copy()
    for key in SYSTEMD_NOTIFY_ENV_KEYS:
        env.pop(key, None)
    return env


class BasePlugin(ABC):
    """ABC для всех плагинов стеков."""

    name: str = ""
    pid_file: Optional[Path] = None

    @abstractmethod
    async def start(self) -> int:
        """Запустить стек. Return 0 on success."""
        ...

    @abstractmethod
    async def stop(self) -> int:
        """Остановить стек. Return 0 on success."""
        ...

    @abstractmethod
    async def test(self) -> int:
        """Протестировать стек. Return {"status": "ok"/"fail", ...}."""
        ...

    @abstractmethod
    async def activate(self) -> int:
        """Активировать маршрутизацию через стек. Return 0 on success."""
        ...

    @abstractmethod
    async def deactivate(self) -> int:
        """Деактивировать маршрутизацию. Return 0 on success."""
        ...

    # --- Общие утилиты ---

    def process_log_path(self, pid_file: Optional[Path] = None) -> Path:
        """Файл лога для фонового процесса плагина."""
        pf = pid_file or self.pid_file
        stem = pf.stem if pf else (self.name or "plugin-process")
        return PROCESS_LOG_DIR / f"{stem}.log"

    def process_meta_path(self, pid_file: Optional[Path] = None) -> Optional[Path]:
        pf = pid_file or self.pid_file
        return pf.with_suffix(".meta.json") if pf else None

    async def run_cmd(
        self, cmd: list[str], timeout: int = 30, check: bool = False
    ) -> tuple[int, str, str]:
        """Выполнить команду async с timeout и обработкой ошибок."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            rc = proc.returncode or 0
            out = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()
            if check and rc != 0:
                logger.error("%s cmd failed: %s → rc=%d err=%s", self.name, cmd, rc, err)
            return rc, out, err
        except asyncio.TimeoutError:
            logger.error("%s cmd timeout (%ds): %s", self.name, timeout, cmd)
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return -1, "", "timeout"
        except Exception as exc:
            logger.error("%s cmd error: %s → %s", self.name, cmd, exc)
            return -1, "", str(exc)

    async def start_process(
        self, cmd: list[str], pid_file: Optional[Path] = None
    ) -> Optional[subprocess.Popen]:
        """Запустить фоновый процесс с сохранением PID.

        Использует subprocess.Popen вместо asyncio.create_subprocess_exec,
        чтобы предотвратить SIGKILL при GC объекта Process (CPython issue:
        BaseSubprocessTransport.close() убивает дочерний процесс).
        start_new_session=True изолирует процесс от SIGHUP родителя.
        """
        log_fh = None
        try:
            pf = pid_file or self.pid_file
            PROCESS_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = self.process_log_path(pf)
            log_fh = open(log_path, "ab", buffering=0)
            try:
                header = (
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{self.name or 'plugin'} start cmd={cmd}\n"
                ).encode()
                log_fh.write(header)
            except Exception:
                pass
            proc = subprocess.Popen(
                cmd,
                env=child_env(),
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
                close_fds=True,
            )
            if pf and proc.pid:
                pf.write_text(str(proc.pid))
                meta_path = self.process_meta_path(pf)
                if meta_path:
                    meta_path.write_text(
                        json.dumps(
                            {
                                "pid": proc.pid,
                                "cmd": cmd,
                                "log_file": str(log_path),
                                "started_at_ts": time.time(),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            logger.info("%s process started: pid=%s cmd=%s", self.name, proc.pid, cmd[:3])
            return proc
        except Exception as exc:
            logger.error("%s start_process failed: %s → %s", self.name, cmd, exc)
            return None
        finally:
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass

    async def stop_process(self, pid_file: Optional[Path] = None) -> bool:
        """Остановить процесс по PID-файлу: SIGTERM → wait 5s → SIGKILL."""
        pf = pid_file or self.pid_file
        meta_path = self.process_meta_path(pf)
        if not pf or not pf.exists():
            return True
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            # Ждём завершения
            for _ in range(50):  # 5 секунд
                await asyncio.sleep(0.1)
                try:
                    os.kill(pid, 0)  # проверка жив ли
                except ProcessLookupError:
                    break
            else:
                # Не завершился за 5 сек → SIGKILL
                logger.warning("%s pid %d did not stop, sending SIGKILL", self.name, pid)
                os.kill(pid, signal.SIGKILL)
                await asyncio.sleep(0.1)
            pf.unlink(missing_ok=True)
            if meta_path:
                meta_path.unlink(missing_ok=True)
            logger.info("%s process stopped: pid=%d", self.name, pid)
            return True
        except ProcessLookupError:
            pf.unlink(missing_ok=True)
            if meta_path:
                meta_path.unlink(missing_ok=True)
            return True
        except Exception as exc:
            logger.error("%s stop_process failed: %s", self.name, exc)
            return False

    def read_pid(self, pid_file: Optional[Path] = None) -> Optional[int]:
        """Прочитать PID из файла, None если не существует или невалидный."""
        pf = pid_file or self.pid_file
        if not pf or not pf.exists():
            return None
        try:
            return int(pf.read_text().strip())
        except (ValueError, OSError):
            return None

    def is_running(self, pid_file: Optional[Path] = None) -> bool:
        """Проверить жив ли процесс по PID-файлу."""
        pid = self.read_pid(pid_file)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

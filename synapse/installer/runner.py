"""
Running a command with its output in the install log, not on the screen.

Every line a command prints goes to `install.log` with a timestamp and to an
optional callback, which is how a step turns uv's or Playwright's output into a
progress bar. Lines are split on carriage returns as well as newlines: a
progress bar redrawn with `\\r` is a stream of updates, not one endless line.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable


class Log:
    """The install log. Thread-safe; every write is flushed."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8", errors="replace")
        self._lock = threading.Lock()
        self.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    def write(self, line: str) -> None:
        with self._lock:
            self._f.write(f"{time.strftime('%H:%M:%S')} {line.rstrip()}\n")
            self._f.flush()

    def tail(self, lines: int = 12) -> list[str]:
        """The last lines a command printed, timestamps off, for an error screen."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out = [line[9:] if line[:2].isdigit() and line[8:9] == " " else line
               for line in text.splitlines() if line.strip()]
        return [line for line in out if not line.startswith("=====")][-lines:]

    def close(self) -> None:
        with self._lock:
            self._f.close()


class CommandFailed(Exception):
    def __init__(self, cmd: list, code: int, tail: list[str]):
        self.cmd, self.code, self.tail = cmd, code, tail
        super().__init__(f"{Path(str(cmd[0])).name} exited with code {code}")


#: Every running child, so Ctrl+C can take them down with the installer.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def run(cmd: list, log: Log, on_line: Callable[[str], None] | None = None,
        env: dict | None = None, cwd: Path | None = None,
        stdin_text: str | None = None, check: bool = True) -> tuple[int, str]:
    """Run `cmd`, logging its output. Returns (exit code, everything it printed).

    `stdin_text` is written to the command's stdin and not logged — it is how
    an API key reaches `synapse setup --apply` without being in the process
    list or the log.
    """
    log.write("$ " + " ".join(str(c) for c in cmd))
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=str(cwd) if cwd else None, **kwargs,
    )
    with _children_lock:
        _children.add(proc)
    try:
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
        captured: list[str] = []
        pending = b""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            pending += chunk
            while True:
                cut = min((i for i in (pending.find(b"\n"), pending.find(b"\r")) if i != -1),
                          default=-1)
                if cut == -1:
                    break
                raw, pending = pending[:cut], pending[cut + 1:]
                _emit(raw, log, on_line, captured)
        if pending:
            _emit(pending, log, on_line, captured)
        code = proc.wait()
    finally:
        with _children_lock:
            _children.discard(proc)
    log.write(f"(exit {code})")
    if check and code != 0:
        # This command's own last words — not the log's, which interleaves
        # whatever else was running at the same time.
        raise CommandFailed(cmd, code, captured[-8:])
    return code, "\n".join(captured)


def _emit(raw: bytes, log: Log, on_line, captured: list[str]) -> None:
    line = raw.decode("utf-8", errors="replace").rstrip()
    if not line:
        return
    captured.append(line)
    log.write(line)
    if on_line is not None:
        try:
            on_line(line)
        except Exception:
            pass   # a progress parser must never fail the command it watches


def kill_children() -> None:
    """Stop every command still running (Ctrl+C, or a failed step)."""
    with _children_lock:
        children = list(_children)
    for proc in children:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in children:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass

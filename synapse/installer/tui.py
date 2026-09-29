"""
The terminal: keys in, one redrawn frame out.

Keys are read from the terminal itself — `/dev/tty`, or the Windows console —
never from stdin: under `curl … | bash` stdin is the script being piped in, and
reading it is how the old installer "answered" its own questions with blank
lines and reported success having installed nothing.

The frame is a block of lines redrawn in place: move the cursor back to the
block's first line, clear to the end of the screen, write it again. Every line
is cut to the terminal's width, because a line that wraps is two lines the next
redraw does not know to move back over.

When there is no terminal to draw on — output piped or captured, `TERM=dumb`,
`CI` set — `Term.live` is False and callers print plain lines instead; see
`screen.PlainScreen`.
"""
from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager

IS_WIN = sys.platform == "win32"

# Glyphs, with the ASCII a legacy Windows code page can print instead.
_UNICODE = {
    "done": "✓", "fail": "✗", "todo": "○", "skip": "–", "pointer": "›",
    "bar": "━", "rest": "─", "dot": "·", "arrow": "→", "secret": "●",
    "ellipsis": "…", "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
}
_ASCII = {
    "done": "+", "fail": "x", "todo": "o", "skip": "-", "pointer": ">",
    "bar": "#", "rest": "-", "dot": "-", "arrow": "->", "secret": "*",
    "ellipsis": "...", "spin": "|/-\\",
}

# Styles as SGR codes; empty strings when colour is off.
_STYLES = {"bold": "1", "dim": "2", "green": "32", "red": "31", "cyan": "36",
           "yellow": "33"}


class Term:
    """What this terminal can do, and the one place that writes to it."""

    def __init__(self, stream=None, force_plain: bool = False):
        self.stream = stream or sys.stdout
        self.live = not force_plain and self._can_draw()
        self.color = self.live and not os.environ.get("NO_COLOR")
        self.glyphs = _UNICODE if self._can_encode("".join(_UNICODE.values())) else _ASCII
        self._keys = None

    # ── capabilities ───────────────────────────────────────────────────────
    def _can_draw(self) -> bool:
        if os.environ.get("TERM") == "dumb" or os.environ.get("CI"):
            return False
        try:
            if not self.stream.isatty():
                return False
        except (AttributeError, ValueError):
            return False
        return _enable_vt() if IS_WIN else True

    def _can_encode(self, text: str) -> bool:
        try:
            text.encode(getattr(self.stream, "encoding", None) or "ascii")
            return True
        except (UnicodeEncodeError, LookupError):
            return False

    @property
    def width(self) -> int:
        return max(40, shutil.get_terminal_size((80, 24)).columns)

    def g(self, name: str) -> str:
        return self.glyphs[name]

    def style(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        codes = ";".join(_STYLES[n] for n in names)
        return f"\x1b[{codes}m{text}\x1b[0m"

    # ── output ─────────────────────────────────────────────────────────────
    def write(self, text: str) -> None:
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            self.stream.write(text.encode(encoding, "replace").decode(encoding))
        except (OSError, ValueError):
            return
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def clip(self, text: str) -> str:
        """`text` cut to one terminal line."""
        return clip(text, self.width - 1, self.g("ellipsis"))

    def cursor(self, visible: bool) -> None:
        if self.live:
            self.write("\x1b[?25h" if visible else "\x1b[?25l")

    # ── input ──────────────────────────────────────────────────────────────
    def keys(self) -> "Keys | None":
        """The keyboard, or None when nobody can answer a question."""
        if not self.live:
            return None
        if self._keys is None:
            self._keys = Keys.open()
        return self._keys or None

    def close(self) -> None:
        self.cursor(True)
        if self._keys:
            self._keys.close()


def visible_len(text: str) -> int:
    """Length on screen: SGR codes take no columns."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\x1b":
            end = text.find("m", i)
            i = end + 1 if end != -1 else len(text)
            continue
        out += 1
        i += 1
    return out


def clip(text: str, width: int, ellipsis: str = "...") -> str:
    """Cut `text` to `width` columns, keeping its colour codes intact."""
    if visible_len(text) <= width:
        return text
    out, cols, i = [], 0, 0
    while i < len(text) and cols < width - len(ellipsis):
        if text[i] == "\x1b":
            end = text.find("m", i)
            end = end + 1 if end != -1 else len(text)
            out.append(text[i:end])
            i = end
            continue
        out.append(text[i])
        cols += 1
        i += 1
    return "".join(out) + ellipsis + ("\x1b[0m" if "\x1b[" in text else "")


def _enable_vt() -> bool:
    """Turn on ANSI escape handling in a Windows console. False if it will not."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Keys:
    """Single keypresses from the terminal, as names: 'up', 'enter', 'a', …"""

    @staticmethod
    def open() -> "Keys | bool":
        if IS_WIN:
            try:
                import msvcrt  # noqa: F401
                return _WinKeys()
            except ImportError:
                return False
        try:
            return _PosixKeys(open("/dev/tty", "rb", buffering=0))
        except OSError:
            return False

    @contextmanager
    def raw(self):
        yield

    def read(self) -> str:
        raise NotImplementedError

    def ready(self, timeout: float) -> bool:
        """Whether a key is waiting, within `timeout` seconds."""
        return True

    def close(self) -> None:
        pass


class _PosixKeys(Keys):
    def __init__(self, tty):
        self.tty = tty
        self.fd = tty.fileno()

    @contextmanager
    def raw(self):
        import termios

        old = termios.tcgetattr(self.fd)
        new = termios.tcgetattr(self.fd)
        # No line buffering, no echo; keep ISIG so Ctrl+C is still SIGINT.
        new[3] &= ~(termios.ICANON | termios.ECHO)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        try:
            yield
        finally:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, old)

    def _more(self, timeout: float = 0.03) -> bool:
        import select

        return bool(select.select([self.fd], [], [], timeout)[0])

    def ready(self, timeout: float) -> bool:
        return self._more(timeout)

    def read(self) -> str:
        first = os.read(self.fd, 1)
        if first == b"\x1b":
            if not self._more():
                return "esc"
            seq = os.read(self.fd, 1)
            if seq in (b"[", b"O"):
                code = os.read(self.fd, 1)
                return {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}.get(code, "")
            return "esc"
        if first in (b"\r", b"\n"):
            return "enter"
        if first in (b"\x7f", b"\x08"):
            return "backspace"
        if first == b"\x03":
            raise KeyboardInterrupt
        if first == b"\t":
            return "tab"
        # A UTF-8 character arrives as several bytes.
        data = first
        while True:
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                if len(data) >= 4 or not self._more(0.01):
                    return ""
                data += os.read(self.fd, 1)

    def close(self) -> None:
        try:
            self.tty.close()
        except OSError:
            pass


class _WinKeys(Keys):
    def ready(self, timeout: float) -> bool:
        import msvcrt
        import time

        end = time.monotonic() + timeout
        while not msvcrt.kbhit():
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True

    def read(self) -> str:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(code, "")
        if ch == "\r":
            return "enter"
        if ch == "\x08":
            return "backspace"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\t":
            return "tab"
        return ch

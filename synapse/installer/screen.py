"""
The install screen: a title, the steps, and a space for the current question.

Two implementations with one interface. `LiveScreen` redraws a frame in place
and animates it from a ticker thread, so a step keeps moving while a question
waits for an answer. `PlainScreen` prints one line when a step starts and one
when it ends — for logs, CI and pipes, where a redraw is garbage.

Steps are updated from worker threads; everything that touches the frame takes
`lock`.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .tui import Term

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"


@dataclass
class Step:
    key: str
    title: str
    state: str = PENDING
    detail: str = ""
    progress: float | None = None     # 0..1 while running, None for a spinner
    started: float = 0.0
    ended: float = 0.0
    # Where the eased bar currently is: it chases `progress` so it never jumps.
    shown: float = field(default=0.0, repr=False)

    @property
    def elapsed(self) -> float:
        end = self.ended or time.monotonic()
        return end - self.started if self.started else 0.0


class _Screen:
    def __init__(self, term: Term, title: str):
        self.term = term
        self.title = title
        self.steps: list[Step] = []
        self.panel: list[str] = []
        self.lock = threading.RLock()

    # ── steps ──────────────────────────────────────────────────────────────
    def add(self, key: str, title: str) -> Step:
        with self.lock:
            step = Step(key, title)
            self.steps.append(step)
            return step

    def step(self, key: str) -> Step:
        return next(s for s in self.steps if s.key == key)

    def start(self, key: str, detail: str = "") -> None:
        with self.lock:
            s = self.step(key)
            s.state, s.detail, s.started, s.progress = RUNNING, detail, time.monotonic(), None
        self._changed(s)

    def progress(self, key: str, fraction: float | None, detail: str | None = None) -> None:
        with self.lock:
            s = self.step(key)
            if fraction is not None:
                s.progress = max(s.progress or 0.0, min(1.0, fraction))
            if detail is not None:
                s.detail = detail

    def done(self, key: str, detail: str = "") -> None:
        with self.lock:
            s = self.step(key)
            s.state, s.detail, s.ended, s.progress = DONE, detail, time.monotonic(), 1.0
        self._changed(s)

    def fail(self, key: str, detail: str = "") -> None:
        with self.lock:
            s = self.step(key)
            s.state, s.detail, s.ended = FAILED, detail, time.monotonic()
        self._changed(s)

    def skip(self, key: str, detail: str = "") -> None:
        with self.lock:
            s = self.step(key)
            s.state, s.detail = SKIPPED, detail
        self._changed(s)

    def _changed(self, step: Step) -> None:
        pass

    # ── the question area (LiveScreen draws it; PlainScreen has none) ───────
    def set_panel(self, lines: list[str]) -> None:
        with self.lock:
            self.panel = list(lines)
        self.render()

    def render(self) -> None:
        pass

    def open(self) -> None:
        pass

    def close(self, final: list[str] | None = None) -> None:
        pass


class LiveScreen(_Screen):
    """The frame, redrawn in place a dozen times a second."""

    _NAME_WIDTH = 24
    _BAR_WIDTH = 24

    def __init__(self, term: Term, title: str):
        super().__init__(term, title)
        self._height = 0
        self._tick = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def open(self) -> None:
        self.term.cursor(False)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.08):
            self._tick += 1
            self.render()

    def lines(self) -> list[str]:
        t, g = self.term, self.term.g
        out = ["", "  " + t.style(self.title, "bold"), ""]
        for s in self.steps:
            name = s.title.ljust(self._NAME_WIDTH)
            if s.state == DONE:
                out.append(f"  {t.style(g('done'), 'green')} {name}{t.style(s.detail, 'dim')}")
            elif s.state == FAILED:
                out.append(f"  {t.style(g('fail'), 'red')} {name}{t.style(s.detail, 'red')}")
            elif s.state == SKIPPED:
                out.append(f"  {t.style(g('skip'), 'dim')} {t.style(name, 'dim')}{t.style(s.detail, 'dim')}")
            elif s.state == RUNNING:
                spin = g("spin")[self._tick % len(g("spin"))]
                tail = self._bar(s) if s.progress is not None else t.style(s.detail, "dim")
                if s.progress is not None and s.detail:
                    tail += "  " + t.style(s.detail, "dim")
                out.append(f"  {t.style(spin, 'cyan')} {name}{tail}")
            else:
                out.append(f"  {t.style(g('todo'), 'dim')} {t.style(s.title, 'dim')}")
        if self.panel:
            if self.steps:
                out.append("")
            out.extend(self.panel)
        out.append("")
        return out

    def _bar(self, s: Step) -> str:
        # Ease towards the real figure: a parser that reports in jumps (a big
        # download finishing) still reads as a bar that moves.
        target = s.progress or 0.0
        s.shown = target if target < s.shown else s.shown + (target - s.shown) * 0.25
        width = max(8, min(self._BAR_WIDTH, self.term.width - self._NAME_WIDTH - 20))
        filled = int(round(s.shown * width))
        t, g = self.term, self.term.g
        bar = t.style(g("bar") * filled, "cyan") + t.style(g("rest") * (width - filled), "dim")
        return f"{bar} {int(s.shown * 100):>3}%"

    def render(self) -> None:
        with self.lock:
            lines = [self.term.clip(line) for line in self.lines()]
            move = f"\r\x1b[{self._height - 1}A" if self._height > 1 else "\r"
            self.term.write(move + "\x1b[J" + "\n".join(lines))
            self._height = len(lines)

    def close(self, final: list[str] | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self.lock:
            if final is not None:
                # The frame goes; what is left is only what the user needs.
                move = f"\r\x1b[{self._height - 1}A" if self._height > 1 else "\r"
                self.term.write(move + "\x1b[J")
                self.term.write("\n".join(final) + "\n")
            else:
                self.render()
                self.term.write("\n")
        self.term.cursor(True)


class PlainScreen(_Screen):
    """`[2/6] Installing Synapse ........ ok  61s` — one line per finished step.

    In the steps' own order, though several run at once: a step that finishes
    early waits for the ones before it, so a log reads 1, 2, 3.
    """

    def __init__(self, term: Term, title: str):
        super().__init__(term, title)
        self._next = 0
        self._printed: set[str] = set()

    def open(self) -> None:
        self.term.write(f"{self.title}\n")

    def _changed(self, step: Step) -> None:
        with self.lock:
            # A failure is news now, whatever is still running before it.
            if step.state == FAILED and step.key not in self._printed:
                self._line(step)
            while self._next < len(self.steps):
                current = self.steps[self._next]
                if current.state not in (DONE, FAILED, SKIPPED):
                    return
                if current.key not in self._printed:
                    self._line(current)
                self._next += 1

    def _line(self, step: Step) -> None:
        self._printed.add(step.key)
        index = self.steps.index(step) + 1
        label = f"[{index}/{len(self.steps)}] {step.title} ".ljust(30, ".")
        verdict = {DONE: "ok", FAILED: "FAILED", SKIPPED: "skipped"}[step.state]
        took = f"  {int(step.elapsed)}s" if step.state == DONE and step.elapsed >= 1 else ""
        detail = f"  {step.detail}" if step.detail else ""
        self.term.write(f"{label} {verdict}{took}{detail}\n")

    def close(self, final: list[str] | None = None) -> None:
        if final:
            self.term.write("\n".join(line.strip() for line in final if line.strip()) + "\n")


def make_screen(term: Term, title: str) -> _Screen:
    return LiveScreen(term, title) if term.live else PlainScreen(term, title)

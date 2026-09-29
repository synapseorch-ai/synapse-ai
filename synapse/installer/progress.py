"""
Turning a command's output into a fraction done.

uv and Playwright print what they are doing, not how far along they are, so
each parser maps what it recognises onto milestones and never goes backwards.
Formats were recorded from the real tools (uv 0.11, Playwright 1.5x); a line
that matches nothing changes nothing, so a format change costs precision, not
the install.
"""
from __future__ import annotations

import re

_SIZE = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


class UvInstall:
    """`uv tool install`'s output → 0..1.

    Resolved 181 packages in 1.35s              → 5%
    Downloading cpython-3.12… (32.9MiB)        ┐
    Downloading torch (120.1MiB)                │ 5–85% by bytes finished
     Downloaded torch                           ┘
    Prepared 181 packages in 40s                → 85%
    Installed 181 packages in 2s                → 95%
    Installed 2 executables: synapse, …         → 100%
    """

    _RESOLVED = re.compile(r"^Resolved (\d+) packages?")
    _DOWNLOADING = re.compile(r"^Downloading (\S+) \(([\d.]+)\s*(B|KiB|MiB|GiB)\)")
    _DOWNLOADED = re.compile(r"^Downloaded (\S+)")
    _PREPARED = re.compile(r"^Prepared (\d+) packages?")
    _INSTALLED = re.compile(r"^Installed (\d+) packages?")
    _EXECUTABLES = re.compile(r"^Installed \d+ executables?")

    def __init__(self):
        self.fraction = 0.0
        self.packages = 0
        self._sizes: dict[str, float] = {}
        self._done: set[str] = set()

    def feed(self, line: str) -> float:
        line = line.strip()
        if m := self._RESOLVED.match(line):
            self.packages = int(m.group(1))
            self._to(0.05)
        elif m := self._DOWNLOADING.match(line):
            self._sizes[m.group(1)] = float(m.group(2)) * _SIZE[m.group(3)]
        elif m := self._DOWNLOADED.match(line):
            self._done.add(m.group(1))
            total = sum(self._sizes.values()) or 1.0
            done = sum(self._sizes.get(n, 0.0) for n in self._done)
            self._to(0.05 + 0.80 * done / total)
        elif self._PREPARED.match(line):
            self._to(0.85)
        elif self._INSTALLED.match(line):
            self._to(0.95)
        elif self._EXECUTABLES.match(line):
            self._to(1.0)
        return self.fraction

    def _to(self, value: float) -> None:
        self.fraction = max(self.fraction, min(1.0, value))


class Downloads:
    """Several downloads in a row that each report their own percentage.

    Playwright prints `|■■■■      |  40% of 164.8 MiB` per browser part, from 0
    again for each; `parts` is how many to expect, so the whole reads 0→1.
    """

    _PERCENT = re.compile(r"(\d{1,3})% of ")

    def __init__(self, parts: int = 1):
        self.parts = max(1, parts)
        self.index = 0
        self._last = 0
        self.fraction = 0.0

    def feed(self, line: str) -> float:
        m = self._PERCENT.search(line)
        if not m:
            return self.fraction
        pct = int(m.group(1))
        if pct < self._last and self.index < self.parts - 1:
            self.index += 1          # the next part started from zero
        self._last = pct
        value = (self.index + pct / 100) / self.parts
        self.fraction = max(self.fraction, min(1.0, value))
        return self.fraction

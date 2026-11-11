"""What a child process needs from this one's environment to start at all, whatever else its allow-list keeps (F1533).

Windows will not start a program without some of its own variables: without SYSTEMROOT and the rest, Playwright's
Node driver exits at once ("Connection closed while reading from the driver"). An allow-list written for Linux and
macOS (PATH, HOME, LANG, TMPDIR) keeps none of them, and Synapse's browser never started on Windows, from 2.0.0 to
2.0.3 (BRW5). None of these names holds a secret: they say where the system, the person's folders and the temporary
folder are, and what the machine is. On Linux and macOS they are not set, so keeping them keeps nothing there.

Every env allow-list in backend/core takes them from here (tests/unit/test_every_child_env_keeps_what_windows_needs.py).
"""
from __future__ import annotations

import os
from collections.abc import Mapping

#: What Windows needs to start a process (BRW5's probe: the browser host started with exactly these added).
WINDOWS_START: frozenset[str] = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "TEMP", "TMP",
})


def windows_start(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`WINDOWS_START`'s names that are set in `environ` (this process's environment by default), with their values."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k in WINDOWS_START}

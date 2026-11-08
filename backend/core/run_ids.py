"""Run ids: one per run, even when two runs start in the same millisecond (F259).

An id was `run_{orchestration}_{ms}`, and two requests in one millisecond — a v1
integration's burst, a webhook, two widget visitors, a schedule firing beside a
Run Now — were handed the same one. One run's record overwrote the other's, and
the caller polling its run read another request's input and result.

Six random hex characters go *after* the millisecond, so ids still sort by when
their run started (the run list and the cost sample read them newest first).
A leaf module: the loggers that mint ids are imported by the engine itself.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path


def new_run_id(name: str, prefix: str = "run") -> str:
    """`{prefix}_{name}_{ms}_{6 hex}` — `name` is the orchestration, schedule or agent."""
    return f"{prefix}_{name}_{int(time.time() * 1000)}_{secrets.token_hex(3)}"


def new_child_run_id(parent_run_id: str, step_id: str, depth: int) -> str:
    """`{parent}__{step}_d{depth}_{6 hex}` — one per run of a nested step (F251).

    `{parent}__{step}_d{depth}` alone was one id per step, so every Loop
    iteration and every retry of the step overwrote one record and one log.
    The `__` stays: it is what hides a child from every run list, the startup
    sweep and cloud's public views.
    """
    return f"{parent_run_id}__{step_id}_d{depth}_{secrets.token_hex(3)}"


_RUN_ID = re.compile(r"^run_(.+)_\d{13}_[0-9a-f]{6}$")


def orchestration_of(run_id: str) -> str | None:
    """The orchestration a `run_…` id was minted for, or None for another shape.

    An id minted before the suffix, `run_{orchestration}_{ms}`, still reads. A
    nested run's id names its parent, not its own orchestration: None.
    """
    if "__" in run_id:
        return None
    match = _RUN_ID.match(run_id)
    if match:
        return match.group(1)
    if run_id.startswith("run_") and run_id.count("_") >= 2:
        return run_id[len("run_"):].rsplit("_", 1)[0]
    return None


#: What a file name made from a run id may not hold: a separator on any platform (`\\` is one on Windows), a drive
#: or stream mark (`:`), or NUL. `.` and `..` name a folder, not a run.
_NOT_IN_A_NAME = ("/", "\\", ":", "\x00")


def _plain(run_id: str) -> bool:
    return bool(run_id) and run_id not in (".", "..") and not any(c in run_id for c in _NOT_IN_A_NAME)


def run_file(directory: str | os.PathLike, run_id: str, suffix: str, *, _path=os.path) -> Path:
    """The file `<run_id><suffix>` in `directory`, and nowhere else (F1479, CodeQL py/path-injection).

    A run's log and its journal are named from its id, and the log and events routes take that id from the URL. On
    Windows `\\` is a separator and `C:\\x` replaces the folder outright, so `..\\..\\x` read or deleted any `.log`
    the server could reach. An id that is not a plain file name is kept under a name made from its hash: a run whose
    own id holds one of those characters still has its file, found again the same way, and an id from a URL that
    climbs out names a file that is not there. Then the resolved path must sit in the folder itself.
    `_path` is `os.path`; tests pass `ntpath` to check Windows' rules on any machine.
    """
    name = run_id if _plain(str(run_id)) else "id_" + hashlib.sha256(str(run_id).encode("utf-8", "surrogatepass")).hexdigest()[:32]
    base = _path.realpath(directory)
    target = _path.realpath(_path.join(base, name + suffix))
    if not target.startswith(base.rstrip(_path.sep) + _path.sep) or _path.dirname(target) != base:
        raise ValueError(f"a run's file must be in {base!r}")
    return Path(target)

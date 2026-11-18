"""A Vault file a workflow reads before its own first write (F1692).

A price watcher read its last price from a file, compared, then wrote the new price to that file. On the first run the
file was not there yet, so the read failed and so did the run. Such a read is the one a later step's write feeds and
no write comes before on every way there: it is found here, as F1621's walk finds a state key read before it is
written (`state_reads.Walk`), with each Tool step's write of a file as its "key". The plan marks it `missing_ok` and
the card says so; checking a saved workflow says it with its fix.

Only Tool steps with a set path are judged. A read of a file no step writes is the person's own file, which a missing
one should still fail on; a path built from the run's values (`{{ … }}`) is not known before it runs.
"""
from __future__ import annotations

import copy

from core.orchestration.state_reads import Walk, tool_part

READS = frozenset({"vault_read"})
WRITES = frozenset({"vault_write", "vault_create"})
#: The prefix of a file's "key" in the walk: no state key starts with it.
_FILE = "\x00vault:"


def _tool(step: dict) -> str:
    return tool_part(str(step.get("forced_tool") or step.get("tool") or ""))


def _path(step: dict) -> str | None:
    """The file a Tool step names, as the Vault keys it; None when it is not set or is built from the run's values."""
    args = step.get("tool_arguments") or {}
    path = args.get("path") if _tool(step) != "vault_create" else (args.get("path") or args.get("filename"))
    if not isinstance(path, str) or not path.strip() or "{{" in path or "{%" in path:
        return None
    return path.strip().removeprefix("./").lstrip("/")


def _as_files(orch: dict) -> dict:
    """The workflow with each Tool step's write of a file as its only key, for the walk."""
    out = copy.deepcopy(orch)
    for step in out.get("steps") or []:
        if not isinstance(step, dict):
            continue
        path = _path(step) if step.get("type") == "tool" and _tool(step) in WRITES else None
        step["output_key"] = f"{_FILE}{path}" if path else None
    return out


def first_reads(orch: dict) -> list[tuple[str, str]]:
    """`(step id, file)` for each Tool step that reads a file a later step writes, before any step writes it on some
    way there, not already marked `missing_ok`."""
    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    written = {p for s in steps if s.get("type") == "tool" and _tool(s) in WRITES and (p := _path(s))}
    reads = [(s["id"], p) for s in steps if s.get("type") == "tool" and _tool(s) in READS
             and (p := _path(s)) in written and not (s.get("tool_arguments") or {}).get("missing_ok")]
    if not reads:
        return []
    walk = Walk(_as_files(orch))
    return [(sid, path) for sid, path in reads if sid in walk.unset(f"{_FILE}{path}")[0]]


def first_read_warnings(orch: dict) -> list[str]:
    """One sentence per such read, for checking a saved workflow: the step, the file, and the fix."""
    names = {s.get("id"): s.get("name") or s.get("id") for s in orch.get("steps") or [] if isinstance(s, dict)}
    return [f"Step '{names.get(sid)}' reads {path} before any step writes it, so its first run fails: the file is not "
            "there yet. Set its \"missing_ok\": true to read it as empty until a later step writes it."
            for sid, path in first_reads(orch)]


def first_read_note(name: str, path: str) -> str:
    """What the plan's card says of a read it marked."""
    return f"'{name}' reads {path} before a later step writes it: on its first run there's no file yet, so it reads as empty."

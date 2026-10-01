"""Running a parser in its own process, with a timeout and a memory cap.

A PDF, an Office file or an email is untrusted input to a parser written in C
or in Python: a malformed file can loop, or expand until the machine swaps.
So every format except a small text file parses in a fresh subprocess
(`child.py`), one per document:

* **Timeout** (`limits.parse_timeout_s`): the child is killed.
* **Memory** (`limits.parse_memory_mb`): its resident set, children included
  (LibreOffice), is polled with psutil and the tree is killed past the cap.
  Not `RLIMIT_AS`, which breaks torch's address-space reservations and does
  not exist on Windows.
* **A cut is never silent.** A killed or crashed child that had saved a
  checkpoint gives what it read — `partial`, with `truncated_pages` and the
  cause as warnings. With nothing saved it is `failed` with the cause.
* **Its environment is an allowlist.** The worker's database URL, Redis URL,
  provider keys and cloud credentials never reach the process that opens an
  untrusted file.

The child's directory lives until the caller is done with it: OCR reads the
rendered pages from it (`ChildOutcome.page_png`), so a 2,000-page scan is
never held in memory at once.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from core.knowledge.parse import UnsupportedFormat
from core.knowledge.parse.limits import ParseRefused, ParserFailed
from core.knowledge.parsed import ParsedDocument, ParseWarning

POLL_S = 0.2
BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: Environment variables the parsing process keeps; everything else is dropped.
ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    # Windows needs these to start a process at all.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # Docling's models, offline; thread counts.
    "DOCLING_ARTIFACTS_PATH", "HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS",
    "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TORCH_NUM_THREADS",
    "SYNAPSE_SOFFICE", "SYNAPSE_DATA_DIR", "PYTHONIOENCODING", "PYTHONUTF8",
})
ENV_ALLOW_PREFIXES = ("LC_", "SYNAPSE_KNOWLEDGE_")


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return env


@dataclass
class ChildOutcome:
    parsed: ParsedDocument
    workdir: Path
    peak_rss_mb: float = 0.0
    duration_ms: int = 0
    cut: str | None = None  # parser_timeout | parser_memory | parser_crashed when `parsed` is what was saved
    rendered: list[int] = field(default_factory=list)

    def page_png(self, page: int) -> bytes:
        return (self.workdir / "pages" / f"{page}.png").read_bytes()


@asynccontextmanager
async def run_parse(data: bytes, job: dict, *, timeout_s: float, memory_mb: int,
                    command: list[str] | None = None) -> AsyncIterator[ChildOutcome]:
    """Parse `data` in a subprocess. `job` is `child.py`'s `job.json`; `command` replaces
    `python -m core.knowledge.parse.child` (tests run a misbehaving child through it).

    Yields the outcome while its directory exists. Raises `UnsupportedFormat`,
    a `ParseRefused` the reader raised, or `ParserFailed` (timeout, memory, a
    crash) when nothing was read.
    """
    workdir = Path(tempfile.mkdtemp(prefix="synapse_parse_"))
    try:
        (workdir / "input.bin").write_bytes(data)
        (workdir / "job.json").write_text(json.dumps(job), "utf-8")
        outcome = await _run(workdir, timeout_s=timeout_s, memory_mb=memory_mb,
                             command=command or [sys.executable, "-m", "core.knowledge.parse.child"])
        yield outcome
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _run(workdir: Path, *, timeout_s: float, memory_mb: int, command: list[str]) -> ChildOutcome:
    import psutil

    clock = time.monotonic()
    with open(workdir / "stderr.txt", "wb") as stderr:
        proc = await asyncio.create_subprocess_exec(
            *command, str(workdir),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=stderr,
            env=child_env(), cwd=str(BACKEND_ROOT))
    try:
        ps = psutil.Process(proc.pid)
    except psutil.Error:
        ps = None
    peak, killed = 0, None
    try:
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=POLL_S)
                break
            except asyncio.TimeoutError:
                pass
            rss = _tree_rss(ps)
            peak = max(peak, rss)
            if rss > memory_mb * 1024 * 1024:
                killed = "parser_memory"
            elif time.monotonic() - clock > timeout_s:
                killed = "parser_timeout"
            if killed:
                await _kill_tree(proc, ps)
                break
    except BaseException:
        await _kill_tree(proc, ps)  # cancelled from outside: never leave a parser running
        raise
    duration_ms = int((time.monotonic() - clock) * 1000)
    peak_mb = round(peak / 2**20, 1)

    error = workdir / "error.json"
    if killed is None and error.exists():
        answer = json.loads(error.read_text("utf-8"))
        if answer["kind"] == "unsupported":
            raise UnsupportedFormat(answer["message"])
        refused = ParseRefused(answer["message"], reason=answer["reason"])
        refused.status = answer["status"]
        raise refused
    result = workdir / "result.json"
    if killed is None and result.exists():
        parsed = ParsedDocument.model_validate_json(result.read_text("utf-8"))
        return ChildOutcome(parsed=parsed, workdir=workdir, peak_rss_mb=peak_mb, duration_ms=duration_ms,
                            rendered=_rendered(workdir))

    cause = killed or "parser_crashed"
    message = _cause_message(cause, timeout_s=timeout_s, memory_mb=memory_mb, workdir=workdir,
                             returncode=proc.returncode)
    partial = workdir / "partial.json"
    if partial.exists():
        parsed = ParsedDocument.model_validate_json(partial.read_text("utf-8"))
        parsed = parsed.model_copy(update={"warnings": [*parsed.warnings, ParseWarning(code=cause, message=message)]})
        return ChildOutcome(parsed=parsed, workdir=workdir, peak_rss_mb=peak_mb, duration_ms=duration_ms,
                            cut=cause, rendered=_rendered(workdir))
    raise ParserFailed(message, reason=cause)


def _rendered(workdir: Path) -> list[int]:
    pages = workdir / "pages"
    if not pages.is_dir():
        return []
    return sorted(int(p.stem) for p in pages.glob("*.png") if p.stem.isdigit())


def _cause_message(cause: str, *, timeout_s: float, memory_mb: int, workdir: Path, returncode: int | None) -> str:
    if cause == "parser_timeout":
        return f"Parsing took longer than {int(timeout_s)} s and was stopped."
    if cause == "parser_memory":
        return f"Parsing needed more than {memory_mb} MB of memory and was stopped."
    try:
        tail = (workdir / "stderr.txt").read_text("utf-8", errors="replace").strip()[-600:]
    except OSError:
        tail = ""
    lines = [line for line in tail.splitlines() if line.strip()]
    return f"The parser stopped unexpectedly (exit {returncode})" + (f": {lines[-1][:300]}" if lines else ".")


def _tree_rss(ps) -> int:
    if ps is None:
        return 0
    import psutil

    total = 0
    try:
        procs = [ps, *ps.children(recursive=True)]
    except psutil.Error:
        return 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total


async def _kill_tree(proc: asyncio.subprocess.Process, ps) -> None:
    """SIGKILL the child and whatever it started (LibreOffice), then reap it."""
    if proc.returncode is not None:
        return
    import psutil

    children = []
    if ps is not None:
        try:
            children = ps.children(recursive=True)
        except psutil.Error:
            children = []
    for p in children:
        try:
            p.kill()
        except psutil.Error:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.shield(proc.wait())
    except Exception:  # noqa: BLE001 — it is dead; reaping is best-effort
        pass

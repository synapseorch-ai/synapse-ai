"""
Engine cost benchmark: what one orchestration run costs in writes, prompt
stability, event-loop blocking and store reads.

The load harness next door measures throughput under slow LLMs. This one
measures the *per-run overhead the engine itself adds*, with the LLM instant, so
that an optimisation to the engine shows up as a number moving — and a
regression shows up the same way. Every metric is a spy on a seam the engine
already has:

  DB writes            `StateBackend.save`  — count, bytes serialised, columns
  prompt stability     `fake_llm.calls`     — system prompt / transcript prefix
                                              byte-identical turn to turn?
  loop blocking        `FileRunJournal.append` wall time (pure sync work) plus
                       a watchdog task measuring scheduling delay
  resolution reads     `resolve_agent`, `aggregate_all_tools`,
                       `variables.load`, `resolve_custom_tools` call counts

Report location: $SYNAPSE_STRESS_REPORT_DIR (default: <repo>/stress-reports),
`bench_engine.json` plus a section appended to `stress_report.md`.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from typing import Any

from load_harness import _pct, _report_dir


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class BenchRecorder:
    """Spies for one benchmark phase. Use as an async context manager around a
    run; call `metrics()` afterwards.

    `monkeypatch` is pytest's — every spy is undone at test exit. The spy on
    `save` measures what *would* be serialised for the columns named, so it
    reads the same before and after a partial-write implementation exists.
    """

    def __init__(self, monkeypatch, fake_llm):
        self._mp = monkeypatch
        self._llm = fake_llm
        self.writes: list[dict] = []
        self.resolution: dict[str, int] = {
            "resolve_agent": 0, "aggregate_all_tools": 0,
            "variables.load": 0, "resolve_custom_tools": 0,
        }
        self.append_ms: list[float] = []
        self.observe_ms: list[float] = []
        self.scheduling_delay_ms: list[float] = []
        self._llm_start = 0
        self._watchdog: asyncio.Task | None = None
        self.wall_seconds = 0.0
        self._t0 = 0.0

    # -- install ---------------------------------------------------------------

    async def __aenter__(self):
        import core.orchestration.state as state_mod
        import core.orchestration.journal as journal_mod
        import core.orchestration.runner as runner_mod
        import core.scale.context as ctx_mod
        import core.react_engine as re_mod
        import core.variables as vars_mod

        backend = state_mod.get_state_backend()
        real_save = backend.save

        async def spy_save(run, *a, **kw):
            cols = kw.get("columns")
            doc = run.model_dump(mode="json")
            payload = {c: doc.get(c) for c in cols} if cols else doc
            self.writes.append({
                "bytes": len(json.dumps(payload, default=str)),
                "columns": sorted(cols) if cols else "full",
            })
            return await real_save(run, *a, **kw)

        self._mp.setattr(backend, "save", spy_save)

        def counting(name, real):
            async def wrapper(*a, **kw):
                self.resolution[name] += 1
                return await real(*a, **kw)
            return wrapper

        # resolve_agent / resolve_custom_tools are imported locally at call time
        # from core.scale.context; aggregate_all_tools is bound at react_engine
        # import; variables.load is imported locally as a module attribute.
        self._mp.setattr(ctx_mod, "resolve_agent",
                         counting("resolve_agent", ctx_mod.resolve_agent))
        self._mp.setattr(ctx_mod, "resolve_custom_tools",
                         counting("resolve_custom_tools", ctx_mod.resolve_custom_tools))
        # Bound at import in react_engine and imported by name elsewhere: count
        # both, or a caller that moves to the other binding reads as zero.
        import core.tools as tools_mod
        counted_aggregate = counting("aggregate_all_tools", tools_mod.aggregate_all_tools)
        self._mp.setattr(re_mod, "aggregate_all_tools", counted_aggregate)
        self._mp.setattr(tools_mod, "aggregate_all_tools", counted_aggregate)
        self._mp.setattr(vars_mod, "load", counting("variables.load", vars_mod.load))

        real_append = journal_mod.FileRunJournal.append

        def spy_append(journal, event):
            t0 = time.perf_counter()
            try:
                return real_append(journal, event)
            finally:
                self.append_ms.append((time.perf_counter() - t0) * 1000)

        self._mp.setattr(journal_mod.FileRunJournal, "append", spy_append)

        real_observe = runner_mod._observe

        async def spy_observe(run_id, event):
            t0 = time.perf_counter()
            try:
                return await real_observe(run_id, event)
            finally:
                self.observe_ms.append((time.perf_counter() - t0) * 1000)

        self._mp.setattr(runner_mod, "_observe", spy_observe)

        self._llm_start = len(self._llm.calls)
        self._watchdog = asyncio.create_task(self._watch())
        self._t0 = time.perf_counter()
        return self

    async def _watch(self, tick: float = 0.01):
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(tick)
            late = (loop.time() - before - tick) * 1000
            self.scheduling_delay_ms.append(max(0.0, late))

    async def __aexit__(self, *exc):
        self.wall_seconds = time.perf_counter() - self._t0
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
        return False

    # -- read out ----------------------------------------------------------------

    def llm_calls(self) -> list[dict]:
        return self._llm.calls[self._llm_start:]

    def metrics(self) -> dict[str, Any]:
        calls = self.llm_calls()
        sys_same = 0
        prefix_ratio: list[float] = []
        blocks_same = 0
        blocks_pairs = 0
        for prev, cur in zip(calls, calls[1:]):
            if prev.get("sys_prompt") == cur.get("sys_prompt"):
                sys_same += 1
            p, c = str(prev.get("prompt_msg", "")), str(cur.get("prompt_msg", ""))
            prefix_ratio.append(_common_prefix_len(p, c) / len(p) if p else 0.0)
            pb, cb = prev.get("prompt_blocks"), cur.get("prompt_blocks")
            if pb is not None and cb is not None:
                blocks_pairs += 1
                if cb[:len(pb)] == pb:
                    blocks_same += 1
        pairs = len(calls) - 1

        full = [w["bytes"] for w in self.writes if w["columns"] == "full"]
        partial = [w["bytes"] for w in self.writes if w["columns"] != "full"]
        return {
            "wall_seconds": round(self.wall_seconds, 3),
            "llm_calls": len(calls),
            "db_writes": {
                "count": len(self.writes),
                "full": len(full),
                "partial": len(partial),
                "total_bytes": sum(full) + sum(partial),
                "full_bytes": sum(full),
                "partial_bytes": sum(partial),
                "max_bytes": max([*full, *partial], default=0),
            },
            "prompt_stability": {
                "sys_prompt_identical_pairs": f"{sys_same}/{pairs}" if pairs > 0 else "n/a",
                "sys_prompt_identical_ratio": round(sys_same / pairs, 3) if pairs > 0 else None,
                "transcript_prefix_ratio_mean": round(statistics.mean(prefix_ratio), 3) if prefix_ratio else None,
                "block_prefix_identical": (f"{blocks_same}/{blocks_pairs}" if blocks_pairs else "n/a"),
            },
            "loop_blocking_ms": {
                "journal_append_total": round(sum(self.append_ms), 2),
                "journal_append_max": round(max(self.append_ms, default=0.0), 3),
                "observe_total": round(sum(self.observe_ms), 2),
                "scheduling_delay_p95": round(_pct(self.scheduling_delay_ms, 0.95), 2),
                "scheduling_delay_max": round(max(self.scheduling_delay_ms, default=0.0), 2),
            },
            "resolution_calls": dict(self.resolution),
        }


def write_bench_report(phases: dict[str, dict]) -> None:
    """`phases` maps phase name -> BenchRecorder.metrics()."""
    d = _report_dir()
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "git": os.getenv("GITHUB_SHA") or _git_head(), "phases": phases}
    (d / "bench_engine.json").write_text(json.dumps(report, indent=2))

    lines = [f"## Engine benchmark — {report['git']}", "",
             "| phase | llm calls | writes (full/partial) | bytes | max write | sys-prompt stable | "
             "transcript prefix | blocks | journal ms (total/max) | sched p95 ms | resolve/aggregate/vars/custom | wall s |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, m in phases.items():
        w, ps, lb, rc = m["db_writes"], m["prompt_stability"], m["loop_blocking_ms"], m["resolution_calls"]
        lines.append(
            f"| {name} | {m['llm_calls']} | {w['count']} ({w['full']}/{w['partial']}) | "
            f"{w['total_bytes']:,} | {w['max_bytes']:,} | {ps['sys_prompt_identical_pairs']} | "
            f"{ps['transcript_prefix_ratio_mean']} | {ps['block_prefix_identical']} | "
            f"{lb['journal_append_total']}/{lb['journal_append_max']} | {lb['scheduling_delay_p95']} | "
            f"{rc['resolve_agent']}/{rc['aggregate_all_tools']}/{rc['variables.load']}/{rc['resolve_custom_tools']} | "
            f"{m['wall_seconds']} |")
    md = "\n".join(lines) + "\n"
    with (d / "stress_report.md").open("a") as f:
        f.write(md + "\n")
    print("\n" + md, flush=True)


def _git_head() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"

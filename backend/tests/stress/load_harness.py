"""
Async load harness for the stress suite.

Runs many task-coroutines through a bounded-concurrency semaphore, records
per-task latency and success/failure, and emits a JSON + Markdown report
(throughput, p50/p95/p99/max latency, error rate). Under the stress profile the
fake LLM injects a random 5-90s delay on a fraction of calls, so this measures
how the system behaves when LLM calls are slow — without any real provider.

Report location: $SYNAPSE_STRESS_REPORT_DIR (default: <repo>/stress-reports).
CI points that at a path it then uploads as an artifact.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import statistics
import time
from typing import Awaitable, Callable


def _report_dir() -> pathlib.Path:
    env = os.getenv("SYNAPSE_STRESS_REPORT_DIR")
    if env:
        p = pathlib.Path(env)
    else:
        # backend/tests/stress/load_harness.py -> repo root is parents[3]
        p = pathlib.Path(__file__).resolve().parents[3] / "stress-reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


async def run_load(
    name: str,
    task_factory: Callable[[int], Awaitable],
    *,
    total: int,
    concurrency: int,
) -> dict:
    """Execute ``total`` tasks (``task_factory(i)``) at most ``concurrency`` at a
    time. Returns a metrics dict and writes a report to the report dir."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: list[str] = []

    async def _one(i: int):
        async with sem:
            t0 = time.perf_counter()
            try:
                await task_factory(i)
                latencies.append(time.perf_counter() - t0)
            except Exception as exc:  # noqa: BLE001 — we want to record any failure
                errors.append(f"{type(exc).__name__}: {exc}")

    wall_start = time.perf_counter()
    await asyncio.gather(*[_one(i) for i in range(total)])
    wall = time.perf_counter() - wall_start

    ok = len(latencies)
    metrics = {
        "name": name,
        "total": total,
        "concurrency": concurrency,
        "succeeded": ok,
        "failed": len(errors),
        "error_rate": (len(errors) / total) if total else 0.0,
        "wall_seconds": round(wall, 3),
        "throughput_per_sec": round(ok / wall, 3) if wall > 0 else 0.0,
        "latency_seconds": {
            "min": round(min(latencies), 3) if latencies else 0.0,
            "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "p50": round(_pct(latencies, 0.50), 3),
            "p95": round(_pct(latencies, 0.95), 3),
            "p99": round(_pct(latencies, 0.99), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "sample_errors": errors[:5],
        "delay_profile": {
            "min": os.getenv("SYNAPSE_FAKE_LLM_DELAY_MIN"),
            "max": os.getenv("SYNAPSE_FAKE_LLM_DELAY_MAX"),
            "prob": os.getenv("SYNAPSE_FAKE_LLM_DELAY_PROB"),
        },
    }
    _write_reports(metrics)
    return metrics


def _write_reports(metrics: dict) -> None:
    d = _report_dir()
    safe = metrics["name"].replace("/", "_")
    (d / f"{safe}.json").write_text(json.dumps(metrics, indent=2))

    lat = metrics["latency_seconds"]
    md = f"""## Stress report — {metrics['name']}

| metric | value |
| --- | --- |
| total tasks | {metrics['total']} |
| concurrency | {metrics['concurrency']} |
| succeeded | {metrics['succeeded']} |
| failed | {metrics['failed']} |
| error rate | {metrics['error_rate']:.2%} |
| wall time | {metrics['wall_seconds']}s |
| throughput | {metrics['throughput_per_sec']}/s |
| latency p50 / p95 / p99 / max | {lat['p50']} / {lat['p95']} / {lat['p99']} / {lat['max']}s |
| fake-LLM delay (min/max/prob) | {metrics['delay_profile']['min']}/{metrics['delay_profile']['max']}/{metrics['delay_profile']['prob']} |
"""
    # Append to a combined markdown report (created fresh per session start).
    combined = d / "stress_report.md"
    with combined.open("a") as f:
        f.write(md + "\n")

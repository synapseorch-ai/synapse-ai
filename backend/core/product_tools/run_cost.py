"""What a workspace's runs cost, as the host that ran them counts it (F843, found by QA1a; SAI2-P2, D308).

A run's `total_cost_usd` is the engine's sum of its calls' rate-card estimates. On a host whose own models are priced
elsewhere it is not what was paid: on cloud a managed call (`synapse.*`) is settled by the gateway into the model
ledger and its estimate is 0, and the workspace is charged credits besides. Synapse AI's readers summed the engine's
figure, so a keyless workspace was told its run cost $0.00 when its run page said 5.3 credits and $0.0024.

So every read of run cost asks here: `costs(rows)` gives each run `{"model_dollars", "credits"}`. Self-hosted has no
credits and its dollars are the engine's own estimate. A host installs what its run page says (`set_run_costs`; cloud:
`model_cost_usd` and the meter's credits). A host that cannot answer leaves the engine's figure, never nothing.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.log import get_logger

_log = get_logger(__name__)

#: `fn(run_ids) -> {run_id: {"model_dollars": float, "credits": float | None}}`; a run it does not know is left out.
RunCosts = Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
_host: RunCosts | None = None


def set_run_costs(fn: RunCosts | None) -> None:
    global _host
    _host = fn


def _own(row: dict) -> dict[str, Any]:
    return {"model_dollars": float(row.get("total_cost_usd") or 0), "credits": None}


async def costs(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Each run's cost by its id: the host's figures where it has them, else the engine's own estimate."""
    out = {r["run_id"]: _own(r) for r in rows if r.get("run_id")}
    if _host is None or not out:
        return out
    try:
        paid = await _host(list(out))
    except Exception:  # noqa: BLE001 — the engine's estimate stands; a cost read never fails the answer
        _log.warning("run costs not read from the host", exc_info=True)
        return out
    for run_id, figure in (paid or {}).items():
        if run_id in out and isinstance(figure, dict):
            credits = figure.get("credits")
            out[run_id] = {"model_dollars": float(figure.get("model_dollars") or 0),
                           "credits": None if credits is None else float(credits)}
    return out


def total(figures: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs' costs added up: model dollars always, credits only where the host charged any."""
    out: dict[str, Any] = {"model_dollars": round(sum(f["model_dollars"] for f in figures), 6)}
    charged = [f["credits"] for f in figures if f.get("credits") is not None]
    if charged:
        out["credits"] = round(sum(charged), 3)
    return out

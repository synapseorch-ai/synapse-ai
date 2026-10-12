"""What a run of a workflow will cost, read from its definition before it runs (CE1, D261).

Promise 3 is a bill you can predict, and nothing showed one before a run. This
reads a definition the way the engine will walk it and prices two things:

* **the steps**, by whatever the caller says a step is worth (cloud passes its
  credit rates; OSS has no credits), and
* **the model calls**, as tokens a step of that type usually takes
  (`TOKEN_PRIOR`), times the rate of the model that step will run on.

## A range: the cheapest path to the dearest path

Every number is a span, `low … high`, and both ends are runs that can happen
(Naveen, D261). An `if_else` runs one arm, so pricing both, as the credit hold
does (`api/billing/steps.py`), gives a total no run can reach: Weekly report's
hold is 4.2 credits, and its two possible runs cost 2.1 and 3.1. Here the low
end follows the cheapest arm at every branch and the high end the dearest.

What a definition cannot say is marked rather than guessed, in `open`:

* `per_item`: a for-each or while Loop runs as many times as its data says. It is
  priced at one iteration.
* `repeat`: a route back to an earlier step (an evaluator's retry, an `if_else`
  that loops) can go round again. It is priced once.
* `nested`: a step that runs another workflow is priced as the one step.

A screen says "or more" when any of them is set.

## What is priced as it runs, and what is not

The walk prices what lands in a run's history. **End is never charged**: the
engine stops at it without recording a step, so it costs nothing here whatever
the caller's table says. A Loop's body runs its count times (clamped to its cap,
as the engine clamps it), a Parallel runs every branch, and a step reached only
from inside a Loop or a Parallel is priced through its container, never twice.

## The model half is a prior until the workflow has runs

`TOKEN_PRIOR` is measured, not assumed: per step type, the 25th and 75th
percentile of input and output tokens over the twelve templates' runs on the dev
stack (D261, `drafts/ce1/prior.md` in the cloud repo). The low end prices the
cheapest path at the 25th, the high end the dearest at the 75th. Once a workflow
has finished runs since its last edit, the caller shows those instead.

Pure: no store, no request. Each product brings its own rates (cloud's platform
models and markup, the rate card for one's own keys) through `price_model`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.orchestration.step_models import MODEL_STEP_TYPES, model_or_none

#: What the engine calls a Loop's count and its cap when a step leaves them unset
#: (`core/models_orchestration.py::StepConfig`).
DEFAULT_LOOP_COUNT = 3
DEFAULT_LOOP_CAP = 50

PER_ITEM = "per_item"
REPEAT = "repeat"
NESTED = "nested"


@dataclass(frozen=True)
class Tokens:
    """A step type's input and output tokens per run: (25th, 75th percentile)."""

    input: tuple[int, int]
    output: tuple[int, int]


#: Measured on the dev stack on 2026-10-12 (D261): the twelve templates on Nova
#: Pro, once approved and three of them once revised, 12 agent and 27 llm step
#: runs. An agent step either finished in a few turns (17k-42k input tokens) or
#: used all twelve of its turns (118k-295k), so its range is wide by nature; an
#: llm step is one call over a short prompt. An `evaluator` or a `tool` step is
#: one model call over a prompt, the shape of an `llm` step; no template has one,
#: so they take its figures.
TOKEN_PRIOR: dict[str, Tokens] = {
    "agent": Tokens(input=(21_000, 120_000), output=(600, 1_250)),
    "llm": Tokens(input=(270, 900), output=(130, 400)),
}
TOKEN_PRIOR["evaluator"] = TOKEN_PRIOR["llm"]
TOKEN_PRIOR["tool"] = TOKEN_PRIOR["llm"]


@dataclass(frozen=True)
class Span:
    """A cost as a range, and why the high end might be passed."""

    low: float = 0.0
    high: float = 0.0
    open: frozenset[str] = frozenset()

    def as_dict(self, digits: int = 6) -> dict[str, Any]:
        return {"low": round(self.low, digits), "high": round(self.high, digits), "open": sorted(self.open)}


#: A step's price for one execution: (low, high).
Price = Callable[[Mapping[str, Any]], tuple[float, float]]


def _kind(step: Mapping[str, Any]) -> str:
    kind = step.get("type") or step.get("step_type") or ""
    return str(getattr(kind, "value", kind))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def iterations(step: Mapping[str, Any]) -> tuple[int, bool]:
    """How many times a Loop runs its body, and whether that is only a floor.

    A count loop runs `loop_count` times, clamped to its cap, as the engine
    clamps it. A for-each or while loop's count is its data's: one iteration,
    marked open.
    """
    if step.get("loop_over") or step.get("loop_while_condition"):
        return 1, True
    cap = max(1, _int(step.get("loop_max_iterations"), DEFAULT_LOOP_CAP))
    return min(max(1, _int(step.get("loop_count"), DEFAULT_LOOP_COUNT)), cap), False


def successors(step: Mapping[str, Any]) -> list[str | None]:
    """Where a step can go next. `None` is the end of the run."""
    kind = _kind(step)
    if kind == "if_else":
        arms = [step.get("if_true_step_id"), step.get("if_false_step_id")]
        if any(arms):
            return arms
    elif kind == "switch":
        targets = list((step.get("switch_cases") or {}).values())
        targets.append(step.get("switch_default_step_id"))
        return targets
    elif kind == "evaluator" and step.get("route_map"):
        return list((step.get("route_map") or {}).values())
    return [step.get("next_step_id")]


def _children(step: Mapping[str, Any]) -> list[str]:
    """The steps a container runs itself: a Loop's body, a Parallel's branches."""
    kind = _kind(step)
    if kind == "loop":
        return [str(c) for c in step.get("loop_step_ids") or []]
    if kind == "parallel":
        return [str(c) for branch in step.get("parallel_branches") or [] for c in (branch or [])]
    return []


def span(
    definition: Mapping[str, Any] | None,
    price: Price,
    *,
    agents: Mapping[str, Mapping[str, Any]] | None = None,
) -> Span:
    """The cheapest and dearest run of this definition, priced step by step.

    Never raises for a definition it cannot follow: a missing entry starts at
    the first step, a step that names one that does not exist ends there, and a
    container that holds itself is priced once.
    """
    steps = (definition or {}).get("steps")
    if not isinstance(steps, list):
        return Span()
    by_id = {str(s["id"]): s for s in steps if isinstance(s, Mapping) and s.get("id")}
    if not by_id:
        return Span()
    opened: set[str] = set()
    agents = agents or {}

    def own(sid: str, holding: frozenset[str]) -> tuple[float, float]:
        """One execution of a step, with what it contains."""
        step = by_id[sid]
        kind = _kind(step)
        if kind == "end":
            return 0.0, 0.0
        low, high = price(step)
        if kind == "agent" and (agents.get(str(step.get("agent_id"))) or {}).get("type") == "orchestrator":
            opened.add(NESTED)
        held = [c for c in _children(step) if c in by_id and c not in holding and c != sid]
        if held:
            times, unknown = iterations(step) if kind == "loop" else (1, False)
            if unknown:
                opened.add(PER_ITEM)
            inner = holding | {sid}
            body = [own(c, inner) for c in held]
            low += times * sum(b[0] for b in body)
            high += times * sum(b[1] for b in body)
        return low, high

    contained = {c for s in by_id.values() for c in _children(s)}
    memo: dict[str, tuple[float, float]] = {}
    on_path: set[str] = set()

    def walk(sid: str | None) -> tuple[float, float] | None:
        """The cheapest and dearest rest of a run from this step; None is a route back."""
        if sid is None or str(sid) not in by_id:
            return 0.0, 0.0
        sid = str(sid)
        if sid in on_path:
            opened.add(REPEAT)
            return None
        if sid in memo:
            return memo[sid]
        step = by_id[sid]
        if _kind(step) == "end":
            memo[sid] = (0.0, 0.0)
            return memo[sid]
        on_path.add(sid)
        try:
            low, high = own(sid, frozenset())
            rest = [r for r in (walk(n) for n in dict.fromkeys(successors(step))) if r is not None]
            if rest:
                low += min(r[0] for r in rest)
                high += max(r[1] for r in rest)
        finally:
            on_path.discard(sid)
        memo[sid] = (low, high)
        return memo[sid]

    entry = str((definition or {}).get("entry_step_id") or "")
    if entry not in by_id:
        entry = next((sid for sid in by_id if sid not in contained), next(iter(by_id)))
    low, high = walk(entry) or (0.0, 0.0)
    return Span(low, high, frozenset(opened))


# ── The model half ───────────────────────────────────────────────────────────

#: Where a model's money comes from. A product names its own; these are the
#: words both use.
WALLET = "wallet"      # the platform's models, paid from the workspace's model wallet (cloud)
OWN_KEYS = "own_keys"  # a provider key the workspace saved: the rate card's estimate
LOCAL = "local"        # a model on the workspace's own machine: nothing to pay


@dataclass(frozen=True)
class ModelRate:
    """What a model costs and who pays: USD per million input and output tokens."""

    side: str
    input_per_1m: float
    output_per_1m: float


#: A model's rate, or None when nothing prices it (unknown is not free: F63).
PriceModel = Callable[[str], ModelRate | None]


def step_model(step: Mapping[str, Any], agents: Mapping[str, Mapping[str, Any]], workspace_model: str) -> str | None:
    """The model a step will call, as the engine resolves it; None for a step that calls none.

    A step's own model, then its agent's, then the workspace's (`step_models`).
    A step that runs another workflow calls no model itself.
    """
    kind = _kind(step)
    if kind not in MODEL_STEP_TYPES:
        return None
    own = model_or_none(step.get("model"))
    if kind != "agent":
        return own or workspace_model or None
    agent = agents.get(str(step.get("agent_id"))) or {}
    if agent.get("type") == "orchestrator":
        return None
    return own or model_or_none(agent.get("model")) or workspace_model or None


def model_cost(kind: str, rate: ModelRate) -> tuple[float, float]:
    """One run of a step of this type on this rate, in USD: (low, high)."""
    tokens = TOKEN_PRIOR.get(kind) or TOKEN_PRIOR["llm"]
    return tuple(  # type: ignore[return-value]
        (tokens.input[i] * rate.input_per_1m + tokens.output[i] * rate.output_per_1m) / 1_000_000
        for i in (0, 1)
    )


@dataclass
class ModelEstimate:
    """The model half: a span per side that pays, and the models nothing prices."""

    sides: dict[str, Span] = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sides": {side: s.as_dict() for side, s in self.sides.items()},
            "unpriced": self.unpriced,
            "models": self.models,
        }


def models(
    definition: Mapping[str, Any] | None,
    *,
    agents: Mapping[str, Mapping[str, Any]],
    workspace_model: str,
    price_model: PriceModel,
) -> ModelEstimate:
    """What the model calls of one run will cost, a span per side that pays.

    The sides are never added together: the wallet's figure is what the
    workspace will be charged, an own key's is the rate card's guess at someone
    else's bill (D87). A model nothing prices is named in `unpriced`, never
    counted as free.
    """
    steps = [s for s in (definition or {}).get("steps") or [] if isinstance(s, Mapping)]
    resolved: dict[str, tuple[str, ModelRate | None]] = {}
    for step in steps:
        model = step_model(step, agents, workspace_model)
        if model is not None and step.get("id"):
            resolved[str(step["id"])] = (model, price_model(model))

    out = ModelEstimate()
    out.models = list(dict.fromkeys(m for m, _ in resolved.values()))
    out.unpriced = list(dict.fromkeys(m for m, r in resolved.values() if r is None))
    for side in dict.fromkeys(r.side for _, r in resolved.values() if r is not None):

        def price(step: Mapping[str, Any], side: str = side) -> tuple[float, float]:
            model, rate = resolved.get(str(step.get("id")), (None, None))
            if rate is None or rate.side != side:
                return 0.0, 0.0
            return model_cost(_kind(step), rate)

        out.sides[side] = span(definition, price, agents=agents)
    return out


def runs_span(figures: Iterable[float | int | None]) -> Span | None:
    """The lowest and highest of a workflow's finished runs, or None with none."""
    known = [float(f) for f in figures if f is not None]
    return Span(min(known), max(known)) if known else None

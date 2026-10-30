"""Evals, as product tools (F1255, BLD1): what the Evals screen does, for Synapse AI and MCP clients.

"Create an eval for my curiosity post generator" was built as a workflow copy of Evals (TRI1's triage, part 3): the
judge agent, a Workflow step running the target, a print, its scores on no dashboard. Nothing could reach Evals. Now
these do, through the screen's own code (`core.evals.service`, the one place its routes call too):

* a rubric, made or edited, and attached to a workflow (its `rubric_id`): the engine judges each finished run by it;
* a golden set's cases, proposed by a model (nothing saved) and saved as a person approved them, each one;
* a golden set run, its estimate shown before it starts, and stopped;
* a workflow's scores, regressions and golden set runs, read.

A rubric scores a workflow, never an agent on its own: the judge scores a finished run. A host whose Evals do more
(cloud: its own routes, a builder's role, its plan's counts, a credit check before a run, worker jobs) does each its
own way through `set_evals_host`, on every door.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.evals import service
from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


@dataclass(frozen=True)
class EvalsHost:
    """What a host does its own way (F1255's cloud twin). Each takes the caller first, as its routes would, and raises
    `ToolRefusal` in its own words (a plan's count, a role, the credits a run needs); None is self-hosted's."""

    #: `(caller, body, rubric_id | None) -> rubric`.
    save_rubric: Callable[[Any, dict, str | None], Awaitable[dict]] | None = None
    #: `(caller, orch_id, rubric_id, judge_every | None) -> {"orchestration_id", "name", "rubric_id", "judge_every"}`.
    attach_rubric: Callable[[Any, str, str, int | None], Awaitable[dict]] | None = None
    #: `(caller, body, suite_id | None) -> suite`.
    save_suite: Callable[[Any, dict, str | None], Awaitable[dict]] | None = None
    #: `(caller, orch, count, steer) -> [case]`: proposed, never saved.
    propose_cases: Callable[[Any, dict, int, str], Awaitable[list]] | None = None
    #: `(caller, suite_id) -> {"suite_run_id", "status", "estimate"}`.
    start_suite_run: Callable[[Any, str], Awaitable[dict]] | None = None
    #: `(caller, suite_id) -> {"status", "suite_run_id"}`.
    cancel_suite_run: Callable[[Any, str], Awaitable[dict]] | None = None
    #: `(caller, suite) -> str`: what running it costs, in the host's own units ("about 40 credits").
    estimate_words: Callable[[Any, dict], Awaitable[str]] | None = None
    #: `(rubrics, golden sets) -> why there is no room | None`: its plan's counts, before a build makes them.
    room: Callable[[int, int], Awaitable[str | None]] | None = None
    #: `(caller, rubric_id)` / `(caller, suite_id)`: what a stopped build made, removed as its Delete does.
    delete_rubric: Callable[[Any, str], Awaitable[None]] | None = None
    delete_suite: Callable[[Any, str], Awaitable[None]] | None = None


_host: EvalsHost | None = None


def set_evals_host(host: EvalsHost | None) -> None:
    global _host
    _host = host


async def _done(call):
    try:
        return await call
    except service.EvalsRefused as exc:
        raise ToolRefusal(exc.detail) from None


def _held(check, *args, **kw):
    """A form's own check (`service.rubric_body`, `suite_body`), its refusal a tool's."""
    try:
        return check(*args, **kw)
    except service.EvalsRefused as exc:
        raise ToolRefusal(exc.detail) from None


def _hosted(name: str):
    return getattr(_host, name, None) if _host is not None else None


async def room(rubrics: int, sets: int) -> str | None:
    """Why this plan has no room for a build's eval, in its host's words; None on self-hosted, which counts none."""
    host = _hosted("room")
    return await host(rubrics, sets) if host is not None else None


async def remove(caller: Any, kind: str, made_id: str) -> None:
    """What a stopped build made, removed as the Evals screen's Delete does: a rubric, or a golden set."""
    host = _hosted("delete_rubric" if kind == "rubric" else "delete_suite")
    if host is not None:
        await host(caller, made_id)
    elif kind == "rubric":
        await service.delete_rubric(made_id)
    else:
        await service.delete_suite(made_id)


# ── finding what was named ────────────────────────────────────────────────────

async def _workflow(wanted: str) -> dict:
    """A workflow a person can open, by its id or its name (one, never guessed between two)."""
    from core.builder_actions import listed
    from core.builder_tools import _is_visible
    from core.routes.orchestrations import load_orchestrations

    wanted = str(wanted or "").strip()
    if not wanted:
        raise ToolRefusal("Say which workflow: its name or its id.")
    flows = [o for o in await load_orchestrations() if _is_visible(o)]
    hit = listed(flows, wanted) or next((o for o in flows if o.get("id") == wanted), None)
    if hit is None:
        named = [o for o in flows if str(o.get("name") or "").casefold() == wanted.casefold()]
        if len(named) > 1:
            raise ToolRefusal(f"More than one workflow is called {wanted!r}: "
                              + ", ".join(f'"{o.get("name")}" ({o.get("id")})' for o in named[:8])
                              + ". Say which one, by its id.")
        hit = named[0] if named else None
    if hit is None:
        raise ToolRefusal(f"There is no workflow {wanted!r} here.")
    return hit


async def _rubric(wanted: str) -> dict:
    wanted = str(wanted or "").strip()
    rubrics = await service.list_rubrics()
    hit = next((r for r in rubrics if r.get("id") == wanted), None) or next(
        (r for r in rubrics if str(r.get("name") or "").casefold() == wanted.casefold()), None)
    if hit is None:
        raise ToolRefusal(f"There is no rubric {wanted!r} here.")
    return hit


async def _sets_of(orch_id: str) -> list[dict]:
    return [s for s in await service.list_suites() if s.get("orchestration_id") == orch_id]


async def _set(args: dict) -> dict:
    """A golden set by its id or name, or the one set of a workflow."""
    wanted = str(args.get("golden_set") or "").strip()
    sets = await service.list_suites()
    if wanted:
        hit = next((s for s in sets if s.get("id") == wanted), None) or next(
            (s for s in sets if str(s.get("name") or "").casefold() == wanted.casefold()), None)
        if hit is None:
            raise ToolRefusal(f"There is no golden set {wanted!r} here.")
        return hit
    flow = await _workflow(args.get("workflow"))
    mine = [s for s in sets if s.get("orchestration_id") == flow["id"]]
    if len(mine) != 1:
        raise ToolRefusal(f'"{flow.get("name")}" has {len(mine) or "no"} golden sets: '
                          + ("say which one, by its name." if mine else "make one with save_eval_cases first."))
    return mine[0]


# ── what each does ────────────────────────────────────────────────────────────

def _rubric_payload(args: dict, before: dict | None = None) -> dict:
    keep = {k: (before or {}).get(k) for k in ("name", "description", "criteria", "scale", "floor", "drop",
                                                "baseline_runs", "judge_model") if (before or {}).get(k) is not None}
    given = {k: args[k] for k in ("name", "description", "criteria", "scale", "floor", "drop") if k in args}
    return {**keep, **given}


async def _rubric_call(args: dict) -> tuple[dict, str | None, dict | None]:
    """What saving it writes: the body, the rubric it edits, and the workflow it attaches to."""
    before = await _rubric(args["rubric"]) if str(args.get("rubric") or "").strip() else None
    body = _held(service.rubric_body, _rubric_payload(args, before))
    flow = await _workflow(args["workflow"]) if str(args.get("workflow") or "").strip() else None
    return body, (before or {}).get("id"), flow


async def _save_rubric(caller: Any, args: dict) -> dict:
    body, rubric_id, flow = await _rubric_call(args)
    host = _hosted("save_rubric")
    rubric = await (host(caller, body, rubric_id) if host else _done(service.save_rubric(body, rubric_id)))
    out: dict[str, Any] = {"rubric": rubric}
    if flow is not None:
        out["attached"] = await _attach(caller, flow["id"], rubric["id"], args.get("judge_every"))
    return out


async def _attach(caller: Any, orch_id: str, rubric_id: str, judge_every: Any = None) -> dict:
    host = _hosted("attach_rubric")
    return await (host(caller, orch_id, rubric_id, judge_every) if host
                  else _done(service.attach_rubric(orch_id, rubric_id, judge_every)))


def sample_words(every: int) -> str:
    """How often a workflow's runs are judged, in the card's words (D340 F)."""
    return "each run that finishes is judged" if every <= 1 else \
        f"its first finished run is judged, then 1 run in {every}"


def _criteria_words(rubric: dict) -> str:
    scale = rubric.get("scale") or {}
    return "; ".join(f"{c.get('name')} (weight {c.get('weight', 1):g})" for c in rubric.get("criteria") or []) \
        + f"; scored {scale.get('min', 0):g}–{scale.get('max', 10):g}"


async def _preview_save_rubric(caller: Any, args: dict) -> dict:
    body, rubric_id, flow = await _rubric_call(args)
    out = {"saves": f'the rubric "{body["name"]}"' + (" (an edit, as a new version)" if rubric_id else ""),
           "criteria": _criteria_words(body)}
    if flow is not None:
        every = args.get("judge_every") or flow.get("judge_every") or 1
        out["attaches_to"] = (f'the workflow "{flow.get("name")}": {sample_words(int(every))} by it, a judge call '
                              "each")
    return out


async def _attach_rubric(caller: Any, args: dict) -> dict:
    flow = await _workflow(args.get("workflow"))
    rubric = await _rubric(args["rubric"]) if str(args.get("rubric") or "").strip() else {"id": ""}
    return await _attach(caller, flow["id"], rubric["id"], args.get("judge_every"))


async def _preview_attach_rubric(caller: Any, args: dict) -> dict:
    flow = await _workflow(args.get("workflow"))
    if not str(args.get("rubric") or "").strip():
        return {"detaches": f'the rubric from "{flow.get("name")}": its runs are no longer scored'}
    rubric = await _rubric(args["rubric"])
    every = int(args.get("judge_every") or flow.get("judge_every") or 1)
    return {"attaches": f'the rubric "{rubric.get("name")}" to "{flow.get("name")}"',
            "with_it": f"{sample_words(every)} by it, a judge call each"}


async def _propose_cases(caller: Any, args: dict) -> dict:
    flow = await _workflow(args.get("workflow"))
    count, steer = int(args.get("count") or 5), str(args.get("steer") or "")
    host = _hosted("propose_cases")
    cases = await (host(caller, flow, count, steer) if host else _done(service.propose_cases(flow, count=count,
                                                                                              steer=steer)))
    return {"workflow": flow["id"], "cases": cases, "saved": False}


async def _cases_call(args: dict) -> tuple[dict, str | None]:
    """The golden set the cases go to (the one named, a workflow's one, or a new one) and its body, whole."""
    flow = await _workflow(args.get("workflow"))
    cases = [c for c in args.get("cases") or [] if isinstance(c, dict)]
    if not cases:
        raise ToolRefusal("Give the cases to save: each one's input, and a name and notes if it has them.")
    wanted = str(args.get("golden_set") or "").strip()
    existing = None
    if wanted:
        existing = await _set({"golden_set": wanted})
        if existing.get("orchestration_id") != flow["id"]:
            raise ToolRefusal(f'The golden set "{existing.get("name")}" tests another workflow.')
    name = str(args.get("name") or "").strip() or (existing or {}).get("name") or f'{flow.get("name")} golden set'
    body = _held(service.suite_body, {"name": name, "orchestration_id": flow["id"],
                                      "cases": [*((existing or {}).get("cases") or []), *cases]}, whole=True)
    return body, (existing or {}).get("id")


async def _save_cases(caller: Any, args: dict) -> dict:
    body, suite_id = await _cases_call(args)
    host = _hosted("save_suite")
    suite = await (host(caller, body, suite_id) if host else _done(service.save_suite(body, suite_id, whole=True)))
    return {"golden_set": suite, "added": len([c for c in args.get("cases") or [] if isinstance(c, dict)])}


async def _preview_save_cases(caller: Any, args: dict) -> dict:
    body, suite_id = await _cases_call(args)
    added = [c for c in args.get("cases") or [] if isinstance(c, dict)]
    return {"saves": f'{len(added)} case{"s" if len(added) != 1 else ""} to the golden set "{body["name"]}"'
                     + ("" if suite_id else " (new)"),
            "cases": [f'{c.get("name") or "Case"}: {str(c.get("input") or "")[:300]}' for c in added]}


async def estimate_words(caller: Any, suite: dict) -> str:
    """What running a golden set costs, as its person can check it: its runs and judge calls, and what its workflow's
    recent finished runs cost each (the host's units: credits on cloud), when it has run."""
    host = _hosted("estimate_words")
    if host is not None:
        return await host(caller, suite)
    from core.evals import suites as suites_mod
    from core.orchestration.state import SharedState
    from core.product_tools import run_cost

    est = suites_mod.estimate(suite)
    said = f'{est["runs"]} run{"s" if est["runs"] != 1 else ""} of the workflow and {est["judge_calls"]} judge call' \
           + ("s" if est["judge_calls"] != 1 else "")
    rows = [r for r in await SharedState.list_runs(limit=20, orchestration_ids={suite.get("orchestration_id")})
            if r.get("status") == "completed"][:10]
    if not rows:
        return f"{said}; it has not run yet, so there is no past run to price it from"
    figures = list((await run_cost.costs(rows)).values())
    dollars = sum(f.get("model_dollars") or 0 for f in figures) / len(figures)
    credits = [f["credits"] for f in figures if f.get("credits") is not None]
    each = f"about ${dollars:.4f}" + (f" and {sum(credits) / len(credits):.1f} credits" if credits else "")
    return f"{said}; its recent runs cost {each} each, so about {est['runs']} times that, plus the judge calls"


async def _run_set(caller: Any, args: dict) -> dict:
    suite = await _set(args)
    host = _hosted("start_suite_run")
    if host is not None:
        return await host(caller, suite["id"])
    from core.product_tools.build import _MODULE

    module = _MODULE.get()
    if module is None:
        import core.server as module  # this process's own tool sessions
    return await _done(service.start_suite_run(suite["id"], module))


async def _preview_run_set(caller: Any, args: dict) -> dict:
    suite = await _set(args)
    if not suite.get("cases"):
        raise ToolRefusal("This set has no cases yet.")
    return {"runs": f'the golden set "{suite.get("name")}"', "costs": await estimate_words(caller, suite)}


async def _cancel_set(caller: Any, args: dict) -> dict:
    suite = await _set(args)
    host = _hosted("cancel_suite_run")
    return await (host(caller, suite["id"]) if host else _done(service.cancel_suite_run(suite["id"])))


async def _read_scores(caller: Any, args: dict) -> dict:
    flow = await _workflow(args.get("workflow"))
    trend = await service.trend(flow["id"], int(args.get("limit") or 50))
    rubric = await service.get_rubric(flow["rubric_id"]) if flow.get("rubric_id") else None
    sets = await _sets_of(flow["id"])
    runs = {s["id"]: (await service.suite_history(s["id"], 3)) for s in sets}
    return {"workflow": {"id": flow["id"], "name": flow.get("name")}, "rubric": rubric,
            "scores": trend["scores"][-20:], "regressions": trend["regressions"][-10:], "agreement": trend["agreement"],
            "golden_sets": [{"id": s["id"], "name": s.get("name"), "cases": len(s.get("cases") or []),
                             "running": s.get("running"), "last_runs": runs.get(s["id"]) or []} for s in sets]}


async def _list_rubrics(caller: Any, args: dict) -> dict:
    return {"rubrics": [{"id": r.get("id"), "name": r.get("name"), "version": r.get("version"),
                         "criteria": _criteria_words(r)} for r in await service.list_rubrics()]}


# ── the entries ───────────────────────────────────────────────────────────────

_WORKFLOW = {"workflow": {"type": "string", "description": "The workflow's name or id."}}
_EVERY = {"judge_every": {"type": "integer", "minimum": 1, "maximum": 100,
                          "description": "Judge its first finished run, then 1 run in this many (1: every run). For "
                                         "a workflow that runs often: each judged run is one more model call."}}
_SET = {"golden_set": {"type": "string", "description": "The golden set's name or id; omit for the workflow's one."}}
_CRITERIA = {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
    "name": {"type": "string"}, "weight": {"type": "number"},
    "guidance": {"type": "string", "description": "What a good answer does, for the judge."}}, "required": ["name"]}}
_CASES = {"type": "array", "maxItems": 100, "items": {"type": "object", "properties": {
    "name": {"type": "string"}, "input": {"type": "string", "description": "What the run starts with."},
    "notes": {"type": "string", "description": "What a good result does, for the person reading scores."}},
    "required": ["input"]}}


def entries() -> list[ProductTool]:
    act = {"kind": "act", "area": "evals", "min_role": Role.BUILDER, "scope": "build"}
    return [
        ProductTool(name="list_rubrics", description="The rubrics here: each one's criteria and scale.",
                    input_schema={"type": "object", "properties": {}}, handler=_list_rubrics, kind="read",
                    area="evals"),
        ProductTool(name="save_rubric",
                    description="Make a rubric, or edit one (`rubric`: its name or id; an edit is a new version), and "
                                "attach it to a workflow (`workflow`), whose finished runs a judge then scores by it.",
                    input_schema={"type": "object", "properties": {
                        "rubric": {"type": "string", "description": "An existing rubric to edit; omit to make one."},
                        "name": {"type": "string"}, "description": {"type": "string"}, "criteria": _CRITERIA,
                        "scale": {"type": "object", "properties": {"min": {"type": "number"},
                                                                   "max": {"type": "number"}}},
                        "floor": {"type": "number", "description": "A score below it is a regression."},
                        "drop": {"type": "number", "description": "A fall this far below the baseline is one."},
                        **_WORKFLOW, **_EVERY}, "required": ["name"]},
                    handler=_save_rubric, preview=_preview_save_rubric, audit_action="rubric.saved",
                    target=lambda a, r: ("rubric", str(((r or {}).get("rubric") or {}).get("id") or "")), **act),
        ProductTool(name="attach_rubric",
                    description="Score a workflow's runs with a rubric (or, with no `rubric`, stop scoring them).",
                    input_schema={"type": "object", "properties": {
                        **_WORKFLOW, **_EVERY,
                        "rubric": {"type": "string", "description": "The rubric's name or id."}},
                        "required": ["workflow"]},
                    handler=_attach_rubric, preview=_preview_attach_rubric, audit_action="rubric.attached",
                    target=lambda a, r: ("orchestration", str((r or {}).get("orchestration_id") or "")), **act),
        ProductTool(name="propose_eval_cases",
                    description="Propose golden test cases for a workflow (a model call). Nothing is saved: show "
                                "them to the person, and save the ones they accept with save_eval_cases.",
                    input_schema={"type": "object", "properties": {
                        **_WORKFLOW, "count": {"type": "integer", "minimum": 1, "maximum": 10},
                        "steer": {"type": "string", "description": "What the cases should cover."}},
                        "required": ["workflow"]},
                    handler=_propose_cases, audit_action="eval_suite.cases_proposed", open_world=True, **act),
        ProductTool(name="save_eval_cases",
                    description="Save cases a person approved to a workflow's golden set (made if it has none).",
                    input_schema={"type": "object", "properties": {
                        **_WORKFLOW, **_SET, "name": {"type": "string", "description": "A new set's name."},
                        "cases": _CASES}, "required": ["workflow", "cases"]},
                    handler=_save_cases, preview=_preview_save_cases, audit_action="eval_suite.saved",
                    target=lambda a, r: ("eval_suite", str(((r or {}).get("golden_set") or {}).get("id") or "")),
                    **act),
        ProductTool(name="start_eval_suite",
                    description="Run a golden set: each case is a run of its workflow, scored by its rubric. It costs "
                                "what those runs and judge calls cost; the preview says how much.",
                    input_schema={"type": "object", "properties": {**_WORKFLOW, **_SET}},
                    handler=_run_set, preview=_preview_run_set, confirmed=True, audit_action="eval_suite.started",
                    target=lambda a, r: ("eval_suite_run", str((r or {}).get("suite_run_id") or "")), **act),
        ProductTool(name="cancel_eval_suite",
                    description="Stop a golden set's run after the case in flight; the runs already done stay.",
                    input_schema={"type": "object", "properties": {**_WORKFLOW, **_SET}},
                    handler=_cancel_set, destructive=True, audit_action="eval_suite.cancelled", **act),
        ProductTool(name="read_eval_scores",
                    description="A workflow's eval: its rubric, its recent scores and regressions, how often the "
                                "judge matched a person, and its golden sets with their last runs.",
                    input_schema={"type": "object", "properties": {
                        **_WORKFLOW, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                        "required": ["workflow"]},
                    handler=_read_scores, kind="read", area="evals"),
    ]

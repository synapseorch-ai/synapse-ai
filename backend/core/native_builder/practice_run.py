"""The build-time practice run (F1619): a planned workflow run once on made-up answers before the person sees it.

The plan check judges what a step reads against what the steps before it declare (F1621's one read check, F1620's tool
answers, F1549 and F1618's fields). It can't see what code makes: after a Transform runs, nothing knows its output's
shape, so a Loop, a condition or a tool argument over it is unjudged; and a Transform's own code is never run over the
answer it reads, so a wrong path through that answer showed first on the run.

Here the plan runs once, with no model, no tool, no network and nothing stored:
- the run's input and each answer a step declares are made up from their shapes (`output_fields.sample`): a model step
  with `output_fields`, a Human step's form, an Evaluator's labels, a tool's declared answer (F1620's `tool_answers`);
  an answer nothing declares is unknown, and no step that reads it is practised;
- If/Else, Switch, Merge, Extract JSON and Print run through the engine's own executors on an in-memory run; a Loop
  goes through its list as the engine's does (`loop_record`, `promote_loop_results`); a Tool step's set arguments are
  rendered as the engine renders them (`render_tool_arguments`), and the tool is not called;
- a Transform's code runs in the code runner with no network (`Network.NONE`); a runner that can't promise that, or
  the host runtime, runs nothing here, and that step is said as not practised. A Vault file its code reads is the
  value an earlier step of the plan wrote there, or one with the shape (columns, keys and their types) of the file
  the workspace has, rows made up: never its contents;
- each way through an If/Else, a Switch and an Evaluator runs once, to the caps.

What it finds is only a read of what a made-up value lacks: a key or field not there, a list that is not one, a value
of the wrong type to read into. A failure that depends on the value itself (a made-up 0 divided by, a date parsed out
of made-up text) says nothing, as made-up values can't stand in for real ones. A finding about a value an MCP server's
declaration made is a note, as that declaration may be wrong (F1620's rule).
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

#: How far one practice goes: ways through a plan, step runs in all, and Transforms run. Past a cap the rest is not
#: practised, and nothing is said of it: what was practised still holds.
MAX_PATHS = 32
MAX_STEP_RUNS = 300
MAX_TRANSFORMS = 12
#: How long one Transform may run here, at most.
TRANSFORM_SECONDS = 20
#: How often one way through may come back to the same step (a route back to an earlier step).
MAX_VISITS = 2
#: How much of a Vault file the workspace has is read for its shape.
FILE_ROWS = 200
FILE_BYTES = 256 * 1024

#: What a read of a value that lacks something raises, by the last line of the traceback a Transform prints.
_SHAPE_ERRORS = re.compile(
    r"^(AttributeError|KeyError|IndexError): |"
    r"^TypeError: (string indices must be integers|list indices must be integers|'[A-Za-z]+' object is not "
    r"(subscriptable|iterable)|'NoneType' object|argument of type)")


@dataclass(frozen=True)
class Finding:
    """A step that read what a made-up value lacks: its id, its name, what it read, and whether the value came from
    an MCP server's declaration (said as a note, never sent back)."""

    step_id: str
    step_name: str
    says: str
    mcp: bool = False


@dataclass
class Practice:
    findings: list[Finding] = field(default_factory=list)
    #: Steps whose code was not run here, each with why (D1: a runner that can't promise no network).
    not_run: list[str] = field(default_factory=list)
    paths: int = 0
    step_runs: int = 0
    transforms: int = 0
    #: Each way's state where it ended and the keys it holds no value for (`{"state", "unknown"}`), kept only when the
    #: practice follows one way (`follow`): what a test holds the engine's own run to.
    ended: list[dict] = field(default_factory=list)


class NotPractised(Exception):
    """A Transform this install can't run with no network: said on the card, the rest practised."""


class _Off(Exception):
    """No code runs here at all (`set_runner(OFF)`): the Transform is left out, nothing said."""


@dataclass
class CodeRun:
    """What a Transform's practice run gave: its result, or the last line of what it raised."""

    result: Any = None
    error: str | None = None


#: The code runner a practice's Transforms run in (a `core.execution` runner), `OFF` for none, or None for the one
#: this install runs code in (`get_runner`).
OFF = "off"
_runner: Any = None


def set_runner(runner: Any) -> None:
    """Run a practice's Transforms in `runner`, in none (`OFF`: each is left out, nothing said), or in this install's
    own again (None). The test suite sets `OFF`, as `render.set_renderer` and `build_plan.set_plan_page_reader` are
    set there, so no test starts Docker through a plan it checks; a test of the practice sets its own."""
    global _runner
    _runner = runner


async def _run_code(code: str, state: dict, files: dict, seconds: int) -> CodeRun:
    import os

    from core.execution import DockerUnavailable, ExecRequest, ExecUnsupported, Network, get_runner, wrap_transform
    from core.execution.scripts import transform_failure, transform_line_offset

    if _runner == OFF:
        raise _Off()
    if _runner is None and os.environ.get("SYNAPSE_TRANSFORM_RUNTIME", "").strip().lower() == "host":
        raise NotPractised("code here runs on this computer, which can't be kept off the network")
    try:
        result = await (_runner or get_runner()).run(ExecRequest(
            code=wrap_transform(code, state, files), network=Network.NONE, timeout_s=seconds, vault_path=None,
            max_stdout=1_000_000))
    except ExecUnsupported:
        raise NotPractised("this install can't run code with no network") from None
    except DockerUnavailable:
        raise NotPractised("Docker isn't running here") from None
    if result.timed_out:
        return CodeRun(error=None)
    if result.exit_code != 0:
        said = transform_failure(result.stderr, code, transform_line_offset(state, files), result.exit_code)
        raised = _raised(result.stderr)
        return CodeRun(error=f"{raised}\n{said}" if raised else said)
    try:
        return CodeRun(result=json.loads(result.stdout).get("result"))
    except (ValueError, AttributeError):
        return CodeRun(result=result.stdout.strip() or None)


def _raised(stderr: str) -> str:
    """The traceback's last line: what the code raised, by its type."""
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip() and not ln[:1].isspace()]
    return lines[-1].strip() if lines else ""


# ── made-up values ────────────────────────────────────────────────────────────────────────────────────────────────

UNKNOWN = object()


def _declared_value(declaration: dict) -> Any:
    """A tool's declared answer made up, or `UNKNOWN`. One known at its first level only is made up when each field
    names its type, its lists empty, as what is in them is not known; else it is unknown."""
    from core.orchestration.output_fields import field_type, sample

    fields = declaration.get("fields") or []
    if not declaration.get("first_level"):
        return sample(fields)
    if not all(f.get("type") for f in fields) or any(field_type(f.get("type")) == "object" for f in fields):
        return UNKNOWN
    made = sample(fields)
    for f in fields:
        if field_type(f.get("type")) == "list":
            made[f["name"].strip()] = []
    return made


@dataclass(frozen=True)
class Made:
    """A step's made-up answer as the run would hold it, or `UNKNOWN`; `mcp` when an MCP server's declaration made it."""

    value: Any
    mcp: bool = False


def made_answer(step: Any, answers: dict[str, dict]) -> Made:
    """The answer a model, Human or Tool step gives in the practice, as the engine would write it to its key: a model
    step's declared fields made up (none declared: unknown, as its words are); a Human step's form answered (no form:
    its words); a tool's declared answer made up, as its text, which the engine keeps (`mcp_text`) and types where
    `state_schema` says. `answers`: `step_answers.tool_answers`. Every other step: unknown."""
    from core.orchestration.output_fields import sample
    from core.orchestration.state_reads import tool_part
    from core.orchestration.step_answers import declared_fields, human_fields

    kind = step.type
    if kind in ("llm", "agent"):
        declared = declared_fields(step.model_dump())
        return Made(sample(declared) if declared else UNKNOWN)
    if kind == "human":
        fields = human_fields(step.model_dump())
        return Made(sample(fields) if fields else "example answer")
    if kind == "tool":
        declaration = answers.get(tool_part(step.forced_tool or ""))
        value = _declared_value(declaration) if declaration is not None else UNKNOWN
        if value is UNKNOWN:
            return Made(UNKNOWN)
        return Made(json.dumps(value), mcp=declaration.get("owner") == "mcp")
    return Made(UNKNOWN)


def _file_shape(path: str, text: str) -> str | None:
    """A Vault file's shape with its values made up (D3): a CSV's columns with two made-up rows, a JSON value's keys
    and types; None for anything else, which leaves the step unpractised. Its own values are never kept."""
    from core.orchestration.output_fields import sample

    text = text[:FILE_BYTES]
    if path.lower().endswith(".csv"):
        rows = list(csv.reader(io.StringIO(text)))[: FILE_ROWS + 1]
        if not rows:
            return None
        header, body = rows[0], rows[1:]
        numeric = [bool(body) and all(_is_number(r[i]) for r in body if i < len(r) and r[i].strip())
                   for i in range(len(header))]
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        for n in (1, 2):
            writer.writerow([str(2 + n) if numeric[i] else f"example {h} {n}" for i, h in enumerate(header)])
        return out.getvalue()
    if path.lower().endswith(".json"):
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return json.dumps(_made_like(value, sample))
    return None


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def _made_like(value: Any, sample: Callable) -> Any:
    """A value with `value`'s keys and types, its own values made up."""
    if isinstance(value, dict):
        return {k: _made_like(v, sample) for k, v in value.items()}
    if isinstance(value, list):
        return [_made_like(value[0], sample)] * 2 if value else []
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return 3
    if value is None:
        return None
    return "example text"


# ── one way through ───────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Way:
    state: dict
    unknown: set[str]
    mcp: set[str]
    files: dict[str, str]
    visits: dict[str, int]
    #: The keys whose value a Transform made here: what only the practice knows the shape of.
    made: set[str] = field(default_factory=set)

    def fork(self) -> "_Way":
        return _Way(copy.deepcopy(self.state), set(self.unknown), set(self.mcp), dict(self.files), dict(self.visits),
                    set(self.made))


@dataclass
class _Plan:
    """One workflow of the plan, as the engine holds it."""

    orch: Any
    by_id: dict[str, Any]
    readers: set[str] | None
    answers: dict[str, dict]
    vault_text: Callable[[str], Awaitable[str | None]]


class _Engine:
    """What the engine's pure executors read of their engine: the workflow, its steps by id, agents' names."""

    def __init__(self, orch: Any):
        self.orch = orch
        self.step_map = {s.id: s for s in orch.steps}
        self.agent_names: dict[str, str] = {}
        self.global_variables: dict[str, str] = {}


class _Stop(Exception):
    """A cap reached: the practice ends where it is."""


class _Run:
    def __init__(self, plans: list[_Plan], follow: bool = False):
        self.plans = plans
        self.out = Practice()
        self.follow = follow
        self._code_cache: dict[str, CodeRun | None] = {}

    def _judged(self, plan: _Plan, step: Any) -> bool:
        return plan.readers is None or step.id in plan.readers

    def _find(self, plan: _Plan, step: Any, says: str, way: _Way, read: set[str]) -> None:
        if not self._judged(plan, step):
            return
        finding = Finding(step.id, step.name or step.id, says, mcp=bool(read & way.mcp))
        if finding not in self.out.findings:
            self.out.findings.append(finding)

    async def go(self, plan: _Plan, sid: str | None, way: _Way) -> None:
        """Run `way` from `sid` to its end, forking at each step with several ways on."""
        while sid and sid in plan.by_id:
            step = plan.by_id[sid]
            way.visits[sid] = way.visits.get(sid, 0) + 1
            if way.visits[sid] > MAX_VISITS:
                break
            ways = await self.step(plan, step, way)
            if ways is None:
                break
            if len(ways) > 1:
                for target, branch in ways[1:]:
                    self.out.paths += 1
                    if self.out.paths >= MAX_PATHS:
                        raise _Stop()
                    await self.go(plan, target, branch)
            sid, way = ways[0] if ways else (None, way)
        if self.follow:
            self.out.ended.append({"state": way.state, "unknown": set(way.unknown)})

    async def step(self, plan: _Plan, step: Any, way: _Way) -> list[tuple[str | None, _Way]] | None:
        """Run one step on `way`: the ways on, each with its own state; None where the way ends. Following one way
        (`follow`), a branch goes where its decision went, and an Evaluator takes its first route."""
        self.out.step_runs += 1
        if self.out.step_runs > MAX_STEP_RUNS:
            raise _Stop()
        kind = step.type
        if kind == "end":
            return None
        if kind == "if_else":
            await self._executor(step, way, plan)
            arms = [(step.if_true_step_id or step.next_step_id, way.fork()),
                    (step.if_false_step_id or step.next_step_id, way)]
            if self.follow:
                return [arms[0] if way.state.get(f"_if_decision_{step.id}") == "true" else arms[1]]
            return arms
        if kind == "switch":
            await self._executor(step, way, plan)
            if self.follow:
                matched = way.state.get(f"_switch_decision_{step.id}")
                case = (step.switch_cases or {}).get(matched) if matched is not None else None
                return [(case or step.switch_default_step_id or step.next_step_id, way)]
            targets = [*dict.fromkeys([*(step.switch_cases or {}).values(), step.switch_default_step_id])]
            targets = [t for t in targets if t] or [step.next_step_id]
            return [(t, way.fork() if i + 1 < len(targets) else way) for i, t in enumerate(targets)]
        if kind == "evaluator":
            routes = list((step.route_map or {}).items()) or [("", step.next_step_id)]
            if self.follow:
                routes = routes[:1]
            out = []
            for i, (label, target) in enumerate(routes):
                branch = way.fork() if i + 1 < len(routes) else way
                branch.state[f"_routing_decision_{step.id}"] = label
                if step.output_key:
                    branch.state[step.output_key] = label
                    branch.unknown.discard(step.output_key)
                out.append((target or step.next_step_id, branch))
            return out
        if kind == "loop":
            await self._loop(plan, step, way)
        elif kind == "parallel":
            from core.orchestration.state_reads import branch_chains

            raw = {s.id: s.model_dump() for s in plan.orch.steps}
            for chain in branch_chains(step.model_dump(), raw):
                for sid in chain:
                    if sid in plan.by_id:
                        await self.step(plan, plan.by_id[sid], way)
        else:
            await self.body_step(plan, step, way)
        return [(step.next_step_id, way)]

    async def body_step(self, plan: _Plan, step: Any, way: _Way) -> None:
        """A step that writes its answer and goes on: made up, run, or left unknown."""
        kind, key = step.type, step.output_key
        if kind in ("merge", "extract_json", "print"):
            await self._executor(step, way, plan)
            self._known_if_read_known(step, way)
            return
        if kind == "transform":
            await self._transform(plan, step, way)
            return
        if kind == "tool":
            await self._tool(plan, step, way)
            return
        if kind == "loop":
            await self._loop(plan, step, way)
            return
        if kind in ("llm", "agent", "human"):
            made = made_answer(step, plan.answers)
            self._write(plan, step, way, made.value, typed=True)
            if kind == "human":
                way.state["human_response"] = made.value
                way.unknown.discard("human_response")
            return
        if kind == "ingest":
            self._ingest(plan, step, way)
        if key:
            way.unknown.add(key)
            way.state.pop(key, None)

    # ── each kind ─────────────────────────────────────────────────────────────────────────────────────────────────

    async def _executor(self, step: Any, way: _Way, plan: _Plan) -> None:
        """One of the engine's own pure executors, on an in-memory run holding this way's state."""
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.steps import STEP_EXECUTORS

        run = OrchestrationRun(run_id="practice", orchestration_id="practice", shared_state=way.state)
        try:
            async for _ in STEP_EXECUTORS[step.type].execute(step, run, _Engine(plan.orch)):
                pass
        except Exception as exc:  # noqa: BLE001 — a refused expression is the validator's to say, never the practice's
            _log.debug("practice: %s %s raised %s", step.type, step.id, exc)
        way.state = run.shared_state
        if step.type in ("if_else", "switch"):
            self._condition_reads(plan, step, way)

    def _condition_reads(self, plan: _Plan, step: Any, way: _Way) -> None:
        """A condition that reads a field a known, made-up value lacks: the documented default at run time, the wrong
        branch taken without a word. Only where the value is code's own: a declared one is the read check's (F1621)."""
        expr = step.if_condition if step.type == "if_else" else step.switch_expression
        for m in re.finditer(r"state\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)", expr or ""):
            root, *parts = m.group(1).split(".")
            if root not in way.made or root not in way.state:
                continue
            cur, trail = way.state[root], [root]
            for part in parts:
                if not isinstance(cur, dict):
                    break
                if part not in cur:
                    keys = ", ".join(f"`{k}`" for k in list(cur)[:8]) or "nothing"
                    self._find(plan, step, f"its condition reads `{'.'.join([*trail, part])}`, and on made-up answers "
                               f"`{'.'.join(trail)}`, which code makes, has {keys}: the run would take its default "
                               "way", way, {root})
                    break
                cur, trail = cur[part], [*trail, part]

    def _known_if_read_known(self, step: Any, way: _Way) -> None:
        """A step that combines what it reads (Merge, Extract JSON, Print) holds no value the practice knows when any
        of it is unknown: its key is unknown too, and holds nothing here."""
        reads = set(step.input_keys or [])
        if step.output_key and reads & way.unknown:
            way.unknown.add(step.output_key)
            way.state.pop(step.output_key, None)
            way.made.discard(step.output_key)

    def _write(self, plan: _Plan, step: Any, way: _Way, value: Any, *, typed: bool, mcp: bool = False) -> None:
        key = step.output_key
        if not key:
            return
        way.made.discard(key)
        if value is UNKNOWN:
            way.state.pop(key, None)
            way.unknown.add(key)
            way.mcp.discard(key)
            return
        before = dict(way.state)
        way.state[key] = value
        way.unknown.discard(key)
        (way.mcp.add if mcp else way.mcp.discard)(key)
        if typed:
            self._typed(plan, step, way, before)

    def _typed(self, plan: _Plan, step: Any, way: _Way, before: dict) -> None:
        """The written key given the type `state_schema` declares, as the engine gives it (`_type_written_state`)."""
        from types import SimpleNamespace

        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import OrchestrationEngine

        run = OrchestrationRun(run_id="practice", orchestration_id="practice", shared_state=way.state)
        shim = SimpleNamespace(orch=plan.orch, _type_warning=OrchestrationEngine._type_warning)
        try:
            OrchestrationEngine._type_written_state(shim, run, step, before)
        except Exception as exc:  # noqa: BLE001 — typed as the engine types it, or left as written
            _log.debug("practice: typing %s raised %s", step.id, exc)
        way.state = run.shared_state

    async def _tool(self, plan: _Plan, step: Any, way: _Way) -> None:
        from core.orchestration.state_reads import tool_part
        from core.orchestration.steps import MissingStateRef, render_tool_arguments, tool_argument_refs

        args = step.tool_arguments
        roots = {ref.split(".")[0] for ref in tool_argument_refs(args)} if args else set()
        name = tool_part(step.forced_tool or "")
        rendered = None
        if args and not roots & way.unknown:
            try:
                rendered = render_tool_arguments(args, way.state)
            except MissingStateRef as exc:
                self._find(plan, step, f"its arguments read `{{{{{exc}}}}}`, which isn't there"
                           + _what_is_there(str(exc), way.state), way, roots)
        if name in ("vault_write", "vault_create") and isinstance(rendered, dict) and rendered.get("path"):
            content = rendered.get("content")
            way.files[str(rendered["path"]).lstrip("/")] = (content if isinstance(content, str)
                                                           else json.dumps(content))
        made = made_answer(step, plan.answers)
        self._write(plan, step, way, made.value, typed=True, mcp=made.mcp)

    async def _transform(self, plan: _Plan, step: Any, way: _Way) -> None:
        from core.execution.scripts import vault_paths_named

        code = step.transform_code or ""
        named = set(re.findall(r"[A-Za-z_]\w*", code))
        if not code or named & way.unknown:
            self._write(plan, step, way, UNKNOWN, typed=False)
            return
        paths, unnamed = vault_paths_named(code)
        files: dict[str, str] = {}
        for path in paths:
            text = way.files.get(path)
            if text is None:
                text = await plan.vault_text(path)
                text = _file_shape(path, text) if text is not None else None
            if text is None:
                self._write(plan, step, way, UNKNOWN, typed=False)
                return
            files[path] = text
        if unnamed:
            self._write(plan, step, way, UNKNOWN, typed=False)
            return
        state = {k: v for k, v in way.state.items() if not k.startswith("_")}
        cache = hashlib.sha256(json.dumps([code, state, files], sort_keys=True, default=str).encode()).hexdigest()
        if cache not in self._code_cache:
            if self.out.transforms >= MAX_TRANSFORMS:
                raise _Stop()
            self.out.transforms += 1
            seconds = min(int(step.timeout_seconds or TRANSFORM_SECONDS), TRANSFORM_SECONDS)
            try:
                self._code_cache[cache] = await _run_code(code, state, files, seconds)
            except _Off:
                self._code_cache[cache] = None
            except NotPractised as why:
                self._code_cache[cache] = None
                line = f"{step.name or step.id}: its code was not practised, as {why}"
                if self._judged(plan, step) and line not in self.out.not_run:
                    self.out.not_run.append(line)
        ran = self._code_cache[cache]
        if ran is None:
            self._write(plan, step, way, UNKNOWN, typed=False)
            return
        if ran.error is not None:
            raised, _, said = ran.error.partition("\n")
            if _SHAPE_ERRORS.search(raised):
                self._find(plan, step, f"its code stopped on made-up answers: {said or raised}", way,
                           named & set(way.state))
            else:
                _log.debug("practice: %s failed on a value, not a read: %s", step.id, raised)
            self._write(plan, step, way, UNKNOWN, typed=False)
            return
        self._write(plan, step, way, ran.result, typed=True)
        if step.output_key and step.output_key in way.state:
            way.made.add(step.output_key)

    async def _loop(self, plan: _Plan, step: Any, way: _Way) -> None:
        from core.orchestration.steps import _not_a_list, _route_expr, loop_record, promote_loop_results

        body = [plan.by_id[b] for b in step.loop_step_ids or [] if b in plan.by_id]
        source = (step.loop_over or "").strip()
        roots = set(re.findall(r"state\.([A-Za-z_]\w*)", source)) or set(re.findall(r"[A-Za-z_]\w*", source))
        if not source or roots & way.unknown:
            for s in [*body, step]:
                if s.output_key:
                    way.unknown.add(s.output_key)
                    way.state.pop(s.output_key, None)
            return
        try:
            items, err = _route_expr(step.loop_over, way.state, "The Loop's list (loop_over)")
        except Exception:  # noqa: BLE001 — a refused expression is the validator's to say
            return
        if err or items is None:
            items = []
        if not isinstance(items, (list, tuple)):
            self._find(plan, step, _not_a_list(step, items).replace(" so it has nothing to go through and nothing "
                                                                     "after it ran", " on made-up answers"),
                       way, roots)
            items = []
        for index, item in enumerate(list(items)[: step.loop_max_iterations or 2][:2]):
            way.state[step.loop_item_key or "loop_item"] = item
            way.state[step.loop_index_key or "loop_index"] = index
            for sub in body:
                await self.step(plan, sub, way)
                if sub.output_key and sub.output_key in way.state and sub.output_key not in way.unknown:
                    loop_record(way.state, sub.output_key, index + 1, sub.name)
        promote_loop_results(way.state, body, step.output_key)

    def _ingest(self, plan: _Plan, step: Any, way: _Way) -> None:
        from core.knowledge import workflow_steps as W

        config = getattr(step, "ingest", None)
        if config is None:
            return
        key = W.state_key(config)
        if key in way.unknown:
            return
        try:
            W.records_for(config, way.state.get(key, W._MISSING), step_name=step.name)
        except W.StepFailed as exc:
            self._find(plan, step, f"on made-up answers, {exc}", way, {key})
        except Exception as exc:  # noqa: BLE001 — a shape this reads oddly is the run's to say
            _log.debug("practice: ingest %s raised %s", step.id, exc)


def _what_is_there(path: str, state: dict) -> str:
    """What the value one step short of `path` has: its keys, at most eight."""
    parts = path.removeprefix("state.").split(".")
    cur: Any = state
    for part in parts[:-1]:
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except ValueError:
                return ""
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return ""
    if isinstance(cur, str):
        try:
            cur = json.loads(cur)
        except ValueError:
            return ""
    if isinstance(cur, dict) and cur:
        keys = list(cur)[:8]
        return f": `{'.'.join(parts[:-1])}` has {', '.join(f'`{k}`' for k in keys)}"
    return ""


# ── the plan ──────────────────────────────────────────────────────────────────────────────────────────────────────

def made_up_inputs(orch: Any) -> dict[str, Any]:
    """The run's input made up: its message, and each described `state_schema` key no step writes (the inputs a run
    is given, as an MCP client is asked for them), a value of its type."""
    from core.orchestration.state_reads import writable

    state: dict[str, Any] = {"user_input": "example request", "user_query": "example request"}
    written = writable([s.model_dump() for s in orch.steps])
    for key, entry in (orch.state_schema or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        if key.startswith("_") or key in written:
            if "default" in entry and not key.startswith("_"):
                state.setdefault(key, entry["default"])
            continue
        kind = str(entry.get("type") or "").lower()
        state[key] = {"int": 3, "integer": 3, "float": 3.0, "number": 3, "bool": True, "boolean": True,
                      "list": [], "array": [], "dict": {}, "object": {}}.get(kind, f"example {key}")
        if entry.get("default") is not None:
            state[key] = entry["default"]
    return state


async def practise(docs: list[dict], *, answers: dict[str, dict], readers: set[str] | None = None,
                   vault_text: Callable[[str], Awaitable[str | None]] | None = None,
                   follow: bool = False) -> Practice:
    """Run each workflow document once on made-up answers (`docs`: the plan as it compiles, the outer one first).
    `answers`: what each tool here declares it answers (`step_answers.tool_answers`). `readers`: the steps the plan
    writes, the only ones a finding names (None: all). `follow`: one way only, as a run goes, each way's last state
    kept (a test's, holding the practice to the engine). Never raises: a practice that breaks says nothing."""
    from core.models_orchestration import Orchestration

    async def none(path: str) -> str | None:
        return None

    plans: list[_Plan] = []
    for doc in docs:
        try:
            orch = Orchestration.model_validate(doc)
        except Exception as exc:  # noqa: BLE001 — a document the engine can't read is the validator's to say
            _log.debug("practice: workflow not read: %s", exc)
            continue
        plans.append(_Plan(orch, {s.id: s for s in orch.steps}, readers, answers, vault_text or none))
    run = _Run(plans, follow=follow)
    for plan in plans:
        entry = plan.orch.entry_step_id or (plan.orch.steps[0].id if plan.orch.steps else None)
        way = _Way(made_up_inputs(plan.orch), set(), set(), {}, {})
        try:
            run.out.paths += 1
            await run.go(plan, entry, way)
        except _Stop:
            _log.info("practice: stopped at its caps (%s paths, %s step runs, %s transforms)", run.out.paths,
                      run.out.step_runs, run.out.transforms)
            break
        except Exception as exc:  # noqa: BLE001 — the practice never stops a plan from being shown
            _log.warning("practice: %s", exc, exc_info=True)
    return run.out

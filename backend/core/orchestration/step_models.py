"""What a workflow's steps run on, read from its definition without running it (F313).

A workflow agent has no model of its own: each step that calls a model has its
own. Chat asks two things of it before anyone types, and both used to be
answered by the workspace's default model, which no step may use:

* can its steps answer (the header's readiness dot), and
* will an attached image reach a step that can read it (Attach)?

Both are read here, the way the engine will run the workflow: a step's model,
then its agent's, then the workspace's (`""` and `"default"` are unset); a step
that runs another workflow is followed into it, as deep as the engine allows;
and an image reaches a step only if it is marked "Sees attachments"
(`pass_images`), and so is every step that nests it.

The loaders are the caller's (the engine's resolvers in OSS, cloud's database
in its API), so this module touches no store. Nothing here raises for a broken
definition: a missing agent or workflow, a workflow that runs itself, one
nested too deep, or one that will not load is a sentence in `problems`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: The steps that call a model. The rest (transform, if_else, human, …) call none.
MODEL_STEP_TYPES = frozenset({"agent", "llm", "tool", "evaluator"})


def calls_a_model(step: Any) -> bool:
    """Whether a step calls a model: one of `MODEL_STEP_TYPES`, but not a Tool step whose arguments are set on it
    (E1), which calls its tool with none. Reads a stored dict or a `StepConfig`."""
    get = step.get if isinstance(step, dict) else (lambda key: getattr(step, key, None))
    kind = getattr(get("type"), "value", get("type")) or get("step_type")
    if kind not in MODEL_STEP_TYPES:
        return False
    return not (kind == "tool" and get("tool_arguments") is not None)

Loader = Callable[[str], Awaitable[dict | None]]


@dataclass(frozen=True)
class ModelStep:
    """One step that calls a model. `path` names it, through the steps that nest it."""

    path: str
    model: str
    takes_attachments: bool


@dataclass
class WorkflowModels:
    steps: list[ModelStep] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def models(self) -> list[str]:
        """Each model once, in the order the steps first name it."""
        return list(dict.fromkeys(s.model for s in self.steps))

    def attaching(self) -> list[ModelStep]:
        return [s for s in self.steps if s.takes_attachments]

    def _say(self, problem: str) -> None:
        if problem not in self.problems:
            self.problems.append(problem)


def model_or_none(model: Any) -> str | None:
    """A saved model, or None when it means "the default": unset, blank or the word `default`.

    The engine's precedence reads a model so (`react_engine`, the steps), and
    anything that asks what an agent or a step runs on must read it the same way.
    """
    text = str(model or "").strip()
    return None if not text or text.lower() == "default" else text


def _name(step: Any) -> str:
    return str(getattr(step, "name", None) or getattr(step, "id", "") or "a step")


async def walk_agent(agent: dict, *, load_agent: Loader, load_orchestration: Loader,
                     workspace_model: str) -> WorkflowModels:
    """The steps a workflow agent's workflow runs on."""
    walked = WorkflowModels()
    orch_id = agent.get("orchestration_id")
    if not orch_id:
        walked._say("This workflow agent has no workflow.")
        return walked
    await _walk(orch_id, walked, load_agent=load_agent, load_orchestration=load_orchestration,
                workspace_model=workspace_model, prefix="", chain=(), depth=0, passed=True)
    return walked


async def _walk(orch_id: str, walked: WorkflowModels, *, load_agent: Loader, load_orchestration: Loader,
                workspace_model: str, prefix: str, chain: tuple[str, ...], depth: int, passed: bool) -> None:
    from core.models_orchestration import Orchestration

    data = await load_orchestration(orch_id)
    if not data:
        via = prefix.removesuffix(" › ")
        walked._say(f"Step '{via}' runs a workflow that no longer exists ({orch_id})." if via
                    else f"Its workflow no longer exists ({orch_id}).")
        return
    try:
        orch = Orchestration.model_validate(data)
    except Exception as e:  # noqa: BLE001 — the engine would refuse it the same way
        walked._say(f"The workflow '{data.get('name') or orch_id}' does not load: {str(e).splitlines()[0]}")
        return
    chain = chain + (orch_id,)

    for step in orch.steps:
        kind = getattr(step.type, "value", step.type)
        if not calls_a_model(step):
            continue
        path = f"{prefix}{_name(step)}"
        takes = passed and kind == "agent" and bool(getattr(step, "pass_images", False))
        if kind != "agent":
            walked.steps.append(ModelStep(path, model_or_none(step.model) or workspace_model, False))
            continue
        agent = await load_agent(step.agent_id) if step.agent_id else None
        if agent is None:
            walked._say(f"Step '{path}' names an agent that no longer exists ({step.agent_id or 'none'}).")
            continue
        if agent.get("type") != "orchestrator":
            walked.steps.append(ModelStep(path, model_or_none(step.model) or model_or_none(agent.get("model")) or workspace_model,
                                          takes))
            continue
        child = agent.get("orchestration_id")
        if not child:
            walked._say(f"Step '{path}' runs a workflow agent that has no workflow.")
        elif child in chain:
            walked._say(f"Step '{path}' runs a workflow that is already running it, so it never ends.")
        elif depth + 1 > _max_depth():
            walked._say(f"Step '{path}' nests workflows deeper than {_max_depth()} levels, which no run allows.")
        else:
            await _walk(child, walked, load_agent=load_agent, load_orchestration=load_orchestration,
                        workspace_model=workspace_model, prefix=f"{path} › ", chain=chain, depth=depth + 1,
                        passed=takes)


def _max_depth() -> int:
    from core.orchestration.engine import MAX_NESTED_DEPTH

    return MAX_NESTED_DEPTH


def _steps(paths: list[str]) -> str:
    named = [f"'{p}'" for p in paths]
    if len(named) == 1:
        return f"Step {named[0]}"
    return f"Steps {', '.join(named[:-1])} and {named[-1]}"


def attach_verdict(walked: WorkflowModels, sees: dict[str, bool]) -> str | None:
    """None when chat may attach images: a step takes them, and every one that does can read them."""
    taking = walked.attaching()
    if not taking:
        return ("No step of this workflow takes attachments. Turn on Sees attachments on the agent step "
                "that should read them.")
    blind = [s for s in taking if not sees.get(s.model, False)]
    if not blind:
        return None
    by_model: dict[str, list[str]] = {}
    for s in blind:
        by_model.setdefault(s.model, []).append(s.path)
    model, paths = next(iter(by_model.items()))
    verb = "takes" if len(paths) == 1 else "take"
    more = f" ({len(blind) - len(paths)} more such steps)" if len(blind) > len(paths) else ""
    return f"{_steps(paths)} {verb} attachments, but {model} cannot read images{more}."


def readiness_problem(walked: WorkflowModels, problems: dict[str, str | None], *, cap: int = 3) -> str | None:
    """Why some step cannot answer, in sentences naming the steps; None when nothing is known to stop them."""
    said = list(walked.problems)
    for model in walked.models():
        problem = problems.get(model)
        if problem:
            paths = [s.path for s in walked.steps if s.model == model]
            said.append(f"{_steps(paths)} ({model}): {problem}")
    if not said:
        return None
    if len(said) > cap:
        said = said[:cap] + [f"…and {len(said) - cap} more."]
    return " ".join(said)

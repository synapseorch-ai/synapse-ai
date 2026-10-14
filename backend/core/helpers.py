"""Helpers: work an agent hands to a throwaway agent of its own (`spawn_subtask`, SUB1a, D279).

An agent that can send helpers (its `tools` name `spawn_subtask`) may hand a focused
task to one: an agent built for that one task from the agent's own tools, which
does no more than the agent may (F266), asks nobody, sends no helpers of its own and
is gone when it ends.

It used to run invisibly — only its answer was read, on the workspace's default
model (F538). Now:

* **It is seen.** Its events go out wrapped, as `helper_event` {helper_id,
  helper_name, event}, between a `helper_started` and a `helper_finished` that
  carries its record. A wrapper and not a tag on the events themselves, because
  everything that streams or speaks an agent's events (a phone call, the
  OpenAI-compatible endpoint, `/api/v1`, a widget) reads `answer_delta` and
  `tool_execution` as the agent's own, and ignores a type it does not know. Only
  an allowlist of its events is wrapped (`SHOWN`): never its prompt.
* **It is kept.** Its record (the brief, its tools, its model, its calls and its
  answer) is saved with the turn that sent it, under `helpers`.
* **It runs on its parent's model,** or the agent's "Helper model": a model, or
  `role:<role>` — the run's own map of roles (BWA1's `model_role`) and then the
  workspace's setting for the role ("Model for quick steps" is `quick`).
* **It is bounded in code:** `MAX_TURNS` turns, a time limit (`WAIT_SECONDS`),
  and at most `MAX_LIVE` at once in a conversation; past that the tool says no
  and not to try again. It starts as marked as its parent is (F279).
* **It can be stopped** on its own, and stops with its parent.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from core.log import get_logger
from core.tenancy import get_tenant

_log = get_logger(__name__)

#: A helper's turns: its own model calls, as an agent's `max_turns` counts them.
MAX_TURNS = 10
#: How long a helper its parent waits for may work (Naveen, SUB1a: 5 minutes).
WAIT_SECONDS = 300
#: How many helpers may work at once in one conversation, or in one workflow agent step (S6).
MAX_LIVE = 3
#: An answer kept with the turn, and a call's result, as a saved turn clips them.
ANSWER_CHARS = 4_000

#: A helper's events that are shown, wrapped: what its lane draws. Nothing else of its turn leaves the engine —
#: never `_log_llm_call` (its whole prompt), `_step_progress` (its state) or `final` (which would end its parent's
#: stream).
SHOWN = frozenset({
    "status", "thinking", "tool_execution", "tool_result", "llm_reasoning", "llm_thought",
    "answer_delta", "answer_reset", "truncated_output", "context_trimmed", "context_compact",
    "tool_call_unparsed", "reasoning_only", "repeat_stopped", "knowledge_search", "knowledge_read",
})

#: What a spawn past the cap reads. Said once: the model is told not to retry, and the call is refused in code anyway.
AT_CAP = ("No helper was started: {n} helpers are already working here, the most at once. Don't call "
          "spawn_subtask again for this now; wait for their results or do it yourself.")

#: What a helper the parent asked to work in the background reads where background is not allowed (S4, S6).
RAN_AS_A_WAIT = "(It ran while you waited: helpers work in the background only in the app's chat.)"

#: What `role:` names in an agent's `helper_model`.
ROLE_PREFIX = "role:"
#: The workspace setting each role reads when the run maps none (BWA1's builder role is `quick`).
ROLE_SETTINGS = {"quick": "builder_quick_model"}

_STATUS_WORDS = {
    "completed": "finished",
    "cancelled": "was stopped before it finished",
    "timed_out": "ran out of time before it finished",
    "failed": "failed",
}


def resolve_model(parent_agent: dict | None, parent_model: str, *, roles: dict | None = None,
                  settings: dict | None = None) -> str:
    """The model a helper runs on (S5, F538): the agent's "Helper model", else its parent's own.

    `helper_model` is a model, or `role:<role>` — mapped by the run's roles first
    (`OrchestrationEngine.MODEL_ROLES_KEY`, set by the host), then by the
    workspace's setting for the role. A role neither maps runs on the parent's
    model, as an unmapped `model_role` step does.
    """
    chosen = str((parent_agent or {}).get("helper_model") or "").strip()
    if not chosen or chosen.lower() == "default":
        return parent_model
    if not chosen.startswith(ROLE_PREFIX):
        return chosen
    role = chosen[len(ROLE_PREFIX):].strip()
    mapped = (roles or {}).get(role) if isinstance(roles, dict) else None
    if not (isinstance(mapped, str) and mapped.strip()):
        setting = ROLE_SETTINGS.get(role)
        mapped = str((settings or {}).get(setting) or "") if setting else ""
    return mapped.strip() or parent_model


# ── one helper ───────────────────────────────────────────────────────────────

@dataclass
class Helper:
    """One helper, from the call that asked for it to its record."""

    id: str
    name: str
    task: str
    tools: list[str]
    withheld: list[str]
    model: str
    agent: dict
    mode: str = "wait"
    #: Set when a person stops it: it ends at its next turn.
    stop_requested: bool = False
    limit_seconds: float = WAIT_SECONDS
    status: str = "running"
    started_at: float = 0.0
    ended_at: float | None = None
    calls: list[dict] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    #: A note for the parent alongside the answer (background asked where it cannot run).
    note: str = ""
    _timed_out: bool = False

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.ended_at or time.time()) - self.started_at, 1)

    def tag(self, event: dict) -> dict:
        return {"type": "helper_event", "helper_id": self.id, "helper_name": self.name, "event": event}

    def started_event(self) -> dict:
        return {"type": "helper_started", "helper_id": self.id, "helper_name": self.name, "task": self.task,
                "tools": list(self.tools), "withheld": list(self.withheld), "model": self.model, "mode": self.mode}

    def record(self) -> dict:
        """What the turn that sent it keeps: its brief, tools, model, calls and answer."""
        from core.session import _clip

        kept = {"id": self.id, "name": self.name, "task": self.task, "tools": list(self.tools),
                "withheld": list(self.withheld), "model": self.model, "mode": self.mode,
                "status": self.status, "seconds": self.seconds, "calls": list(self.calls),
                "answer": _clip(self.answer, ANSWER_CHARS)}
        if self.error:
            kept["error"] = _clip(self.error, 500)
        return kept

    def finished_event(self) -> dict:
        return {"type": "helper_finished", "helper_id": self.id, "helper_name": self.name,
                "status": self.status, "seconds": self.seconds, "record": self.record()}

    def result_for_parent(self) -> str:
        """What the agent that sent it reads back: its answer, or what it got done before it stopped."""
        parts: list[str] = []
        if self.withheld:
            parts.append(f"Not given to the sub-agent: {'; '.join(self.withheld)}.")
        if self.status == "completed":
            parts.append(self.answer or "(It finished without an answer.)")
        else:
            said = f"The helper {_STATUS_WORDS.get(self.status, self.status)}"
            if self.status == "timed_out":
                said += f" ({int(self.limit_seconds // 60)} min)"
            if self.status == "failed" and self.error:
                said += f": {self.error}"
            parts.append(said + ".")
            if self.calls:
                done = "\n".join(f"- {c.get('tool')}: {str(c.get('result') or '')[:300]}" for c in self.calls[-10:])
                parts.append(f"Its calls before it stopped:\n{done}")
        if self.note:
            parts.append(self.note)
        return "\n\n".join(parts)


def build(args: dict, *, parent_agent: dict | None, parent_model: str, available_tools: list[str],
          policy: dict | None, declarations: dict | None, roles: dict | None = None,
          settings: dict | None = None, mode: str = "wait") -> Helper:
    """A helper for one `spawn_subtask` call, given no more than its parent may do (F266).

    `policy` is the parent's *resolved* tool policy and `declarations` its tools
    by name: a tool the parent would be refused, or would have to ask a person
    about, is not given — a helper cannot show a card — and the parent is told,
    so it can call it itself, where the person is asked.
    """
    from core import tool_policy as _tool_policy
    from core.react_engine import _UndeclaredTool

    policy = dict(policy) if policy else dict(_tool_policy.DEFAULT_POLICY)
    declarations = declarations or {}

    def decision(tool_name: str) -> tuple[str, str]:
        declared = declarations.get(tool_name)
        return _tool_policy.decide(declared if declared is not None else _UndeclaredTool(tool_name), policy)

    task = str(args.get("task") or "")
    requested = args.get("tools") or []
    if not isinstance(requested, list):
        requested = [requested]
    name = str(args.get("name") or "").strip() or task[:40] or "Helper"
    valid = [t for t in requested if t in available_tools]
    withheld: list[str] = []
    if valid:
        granted = []
        for tool_name in valid:
            verdict, category = decision(tool_name)
            if verdict == "allow":
                granted.append(tool_name)
            elif verdict == "deny":
                withheld.append(f"{tool_name} (your tool policy blocks it: {_tool_policy.explain(category)})")
            else:
                withheld.append(f"{tool_name} (it needs a person's approval — call it yourself "
                                "and they will be asked)")
        valid = granted
    else:
        # Nothing it asked for exists: the first few it may run outright.
        valid = [t for t in available_tools if decision(t)[0] == "allow"][:5]

    model = resolve_model(parent_agent, parent_model, roles=roles, settings=settings)
    helper_id = f"subtask_{uuid.uuid4().hex[:8]}"
    agent = {
        "id": helper_id,
        "name": name,
        "type": "conversational",
        "model": model,
        "system_prompt": (
            "You are a focused sub-agent. Complete the given task using only "
            "the provided tools. Return a concise, structured result."
        ),
        "tools": valid,
        "max_turns": MAX_TURNS,
        # The parent's resolved policy, with nobody to ask: a backstop for a
        # name the helper is offered that was never judged above.
        "tool_policy": {c: ("deny" if d == "confirm" else d) for c, d in policy.items()},
        # And the parent's scoping: a `run_sql_query` the parent may run
        # against `sales` only must not reach `hr` through a helper (an
        # empty `db_configs` is no restriction), nor may a parent that
        # declined the default file tools hand them out.
        "db_configs": list((parent_agent or {}).get("db_configs") or []),
        "repos": list((parent_agent or {}).get("repos") or []),
        "skip_default_tools": bool((parent_agent or {}).get("skip_default_tools")),
    }
    if model == parent_model and (parent_agent or {}).get("reasoning_effort"):
        # On its parent's model it thinks as hard as its parent; on a model of
        # its own (a helper model) the workspace's effort, as any agent's.
        agent["reasoning_effort"] = parent_agent["reasoning_effort"]
    return Helper(id=helper_id, name=name, task=task, tools=valid, withheld=withheld, model=model,
                  agent=agent, mode=mode, limit_seconds=WAIT_SECONDS)


async def run(helper: Helper, *, server_module, session_id: str, source: str, run_id: str | None,
              untrusted: bool, parent_should_stop: Callable[[], Awaitable[bool]] | None = None,
              ) -> AsyncIterator[dict]:
    """Run one helper: `helper_started`, its shown events wrapped, then `helper_finished`.

    Advance and close it from one task (an anyio scope inside the engine must be
    left by the task that entered it, `react_engine.py`'s generator rule). Its
    own Stop and its time limit end it between its turns, as a turn's Stop does;
    so does its parent stopping. Cancelling the task that drives it cancels it.
    """
    from core.react_engine import run_agent_step
    from core.session import call_record, _clip, CALL_RESULT_CHARS

    helper.started_at = time.time()
    helper.status = "running"
    yield helper.started_event()
    deadline = time.monotonic() + helper.limit_seconds

    async def should_stop() -> bool:
        if helper.stop_requested:
            return True
        if time.monotonic() >= deadline:
            helper._timed_out = True
            return True
        return bool(parent_should_stop is not None and await parent_should_stop())

    calls: list[dict] = []
    answer = ""
    try:
        async for event in run_agent_step(
            message=helper.task,
            agent_id=helper.id,
            session_id=session_id,
            server_module=server_module,
            max_turns=MAX_TURNS,
            source=source,
            run_id=run_id,
            agent_override=helper.agent,
            # A hand-off, like a delegate's: it reads none of the parent's
            # conversation and saves none of its own (F261).
            history_override=[],
            persist_turn=False,
            # Written by an agent that may have read a file or a tool's result:
            # the helper starts as marked as its parent is (F279, S6).
            untrusted_message=untrusted,
            should_stop=should_stop,
            stream_answer=True,
        ):
            kind = event.get("type")
            if kind == "final":
                answer = event.get("response") or ""
                continue
            if kind == "tool_execution":
                calls.append(call_record(event.get("tool_name"), event.get("args") or {}, ""))
            elif kind == "tool_result" and calls and not calls[-1]["result"]:
                calls[-1]["result"] = _clip(event.get("preview") or "", CALL_RESULT_CHARS)
                if event.get("refused"):
                    calls[-1].update(refused=True, reason=event.get("reason") or "")
            if kind in SHOWN:
                yield helper.tag(event)
        helper.status = "completed"
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Cancelled from outside — its parent's turn stopped, or the task
            # driving it was cancelled. Recorded, and the cancel goes on.
            helper.status, helper.calls, helper.ended_at = "cancelled", calls, time.time()
            raise
        # Its own Stop or its time limit, raised by the engine between turns.
        helper.status = "timed_out" if helper._timed_out else "cancelled"
    except Exception as exc:  # noqa: BLE001 — told to the parent, which carries on
        helper.status, helper.error = "failed", str(exc)
        _log.warning(f"helper '{helper.name}' failed: {exc}")
    helper.calls, helper.answer, helper.ended_at = calls, answer, time.time()
    yield helper.finished_event()


# ── how many at once ─────────────────────────────────────────────────────────

#: Live helpers by where they count (a conversation, or one workflow agent step), each by id.
_LIVE: dict[tuple, dict[str, Helper]] = {}


def cap_key(session_id: str | None, *, workflow_step: bool) -> tuple:
    """Where a helper counts against `MAX_LIVE`: its conversation; in a workflow, its agent step alone.

    A workflow's steps and a Parallel's branches share one session id
    (`orch_<run>`), and must not refuse each other's helpers.
    """
    if workflow_step or not session_id:
        return ("step", uuid.uuid4().hex)
    return ("conversation", get_tenant() or "", session_id)


def live(key: tuple) -> dict[str, Helper]:
    return _LIVE.get(key, {})


def reserve(key: tuple, helper: Helper) -> bool:
    """Count the helper in; False (nothing counted) at the cap."""
    held = _LIVE.setdefault(key, {})
    if len(held) >= MAX_LIVE:
        return False
    held[helper.id] = helper
    return True


def release(key: tuple, helper_id: str) -> None:
    held = _LIVE.get(key)
    if held is not None:
        held.pop(helper_id, None)
        if not held:
            _LIVE.pop(key, None)


def find(helper_id: str, session_id: str | None = None) -> Helper | None:
    """A live helper by id, for its Stop: within one conversation when `session_id` is given."""
    for key, held in _LIVE.items():
        if session_id is not None and key != ("conversation", get_tenant() or "", session_id):
            continue
        if helper_id in held:
            return held[helper_id]
    return None


# ── several at once, waited for ──────────────────────────────────────────────

async def batch(helpers: list[Helper], *, key: tuple, **run_kw) -> AsyncIterator[dict]:
    """Run reserved helpers side by side; their events in the order they happen.

    One task per helper drives it (and closes it); this generator only reads
    their queue, so it can be closed or cancelled at any point, and its
    `finally` cancels and waits for whatever is still running. Each helper is
    released from the cap when its task ends, however it ends.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump(helper: Helper) -> None:
        gen = run(helper, **run_kw)
        try:
            async for event in gen:
                queue.put_nowait(event)
        finally:
            with contextlib.suppress(BaseException):
                await gen.aclose()
            queue.put_nowait(done)

    tasks = []
    for helper in helpers:
        task = asyncio.create_task(pump(helper), name=f"helper-{helper.id}")
        task.add_done_callback(lambda _t, hid=helper.id: release(key, hid))
        tasks.append(task)
    left = len(tasks)
    try:
        while left:
            item = await queue.get()
            if item is done:
                left -= 1
                continue
            yield item
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

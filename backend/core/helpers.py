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

**In the background** (`background: true`, the app's chat only — never a visitor's turn, a
workflow step, OAI1 or a channel, where it runs while its parent waits, S4, S6) a helper is
a chat turn of its own on a session `helperc_…` (`chat_turns`: a task the server holds, a
log its lane reads, the turn's Stop), saved as a row of that session linked to its parent's
conversation. Its parent's turn goes on, and may end. When it ends, its result goes back
**exactly once** (`deliver`):

1. into the inbox of that agent's live turn in that conversation (`Inbox`), which reads it
   between model calls as one automated block, never as the person's words;
2. else into the turn a parked question stopped there, which reads it when the question is
   answered — or hands it to the next turn if the question is moved past;
3. else into **one follow-up turn** of the agent (`follow_up`), whose question is the
   result, marked automated; it waits for any turn running there first.

A server restart leaves it failed, and says so in its parent's conversation
(`sweep_interrupted`, at boot).

**On a fleet** (cloud, SUB1b) a turn runs in any worker, so a host installs itself
(`install`): it starts a background helper where it runs it — a chat job of its own,
`run_agent_chat_job(helper=…)` — counts a conversation's helpers wherever they work
(`elsewhere`), and takes each result back (`deliver`: one follow-up job, which waits
for any turn running there). With no host, all of it is in-process, as above.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
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
#: How long a helper working in the background may work (Naveen, SUB1a: 15 minutes).
BACKGROUND_SECONDS = 900
#: A background helper's own conversation: `helperc_<hex>`, its row keyed by the helper's id.
SESSION_PREFIX = "helperc_"
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
    #: A background helper's own conversation (`helperc_…`), and the one it reports back to.
    session_id: str = ""
    conversation: "Conversation | None" = None
    #: Whether it starts marked (F279): its parent's flag, carried to wherever it runs.
    untrusted: bool = False
    _timed_out: bool = False

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.ended_at or time.time()) - self.started_at, 1)

    def spec(self, *, untrusted: bool) -> dict:
        """What a host needs to run it elsewhere (cloud: a job's kwarg), as JSON."""
        return {"id": self.id, "name": self.name, "task": self.task, "tools": list(self.tools),
                "withheld": list(self.withheld), "model": self.model, "agent": dict(self.agent),
                "mode": self.mode, "limit_seconds": self.limit_seconds, "session_id": self.session_id,
                "conversation": self.conversation.link() if self.conversation is not None else None,
                "untrusted": bool(untrusted)}

    @classmethod
    def from_spec(cls, spec: dict) -> "Helper":
        link = spec.get("conversation")
        return cls(id=str(spec["id"]), name=str(spec.get("name") or "Helper"), task=str(spec.get("task") or ""),
                   tools=list(spec.get("tools") or []), withheld=list(spec.get("withheld") or []),
                   model=str(spec.get("model") or ""), agent=dict(spec.get("agent") or {}),
                   mode=str(spec.get("mode") or "background"),
                   limit_seconds=float(spec.get("limit_seconds") or BACKGROUND_SECONDS),
                   session_id=str(spec.get("session_id") or ""),
                   conversation=Conversation.from_link(link) if isinstance(link, dict) else None,
                   untrusted=bool(spec.get("untrusted")))

    def tag(self, event: dict) -> dict:
        return {"type": "helper_event", "helper_id": self.id, "helper_name": self.name, "event": event}

    def started_event(self) -> dict:
        event = {"type": "helper_started", "helper_id": self.id, "helper_name": self.name, "task": self.task,
                 "tools": list(self.tools), "withheld": list(self.withheld), "model": self.model, "mode": self.mode}
        if self.session_id:
            event["session_id"] = self.session_id
        return event

    def record(self) -> dict:
        """What the turn that sent it keeps: its brief, tools, model, calls and answer."""
        from core.session import _clip

        kept = {"id": self.id, "name": self.name, "task": self.task, "tools": list(self.tools),
                "withheld": list(self.withheld), "model": self.model, "mode": self.mode,
                "status": self.status, "seconds": self.seconds, "calls": list(self.calls),
                "answer": _clip(self.answer, ANSWER_CHARS)}
        if self.error:
            kept["error"] = _clip(self.error, 500)
        if self.session_id:
            kept["session_id"] = self.session_id
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


def reserve(key: tuple, helper: Helper, *, elsewhere: int = 0) -> bool:
    """Count the helper in; False (nothing counted) at the cap. `elsewhere` is how many of this conversation's
    helpers work in other processes (a host's, `elsewhere()`): they count too."""
    held = _LIVE.setdefault(key, {})
    if len(held) + max(0, elsewhere) >= MAX_LIVE:
        if not held:
            _LIVE.pop(key, None)
        return False
    held[helper.id] = helper
    return True


def release(key: tuple, helper_id: str) -> None:
    held = _LIVE.get(key)
    if held is not None:
        held.pop(helper_id, None)
        if not held:
            _LIVE.pop(key, None)


def live_in(session_id: str) -> bool:
    """Whether a helper works in this process for this conversation (its Stop can be asked from elsewhere)."""
    return any(key[0] == "conversation" and key[2] == session_id and held for key, held in _LIVE.items())


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


# ── where a result goes back to ──────────────────────────────────────────────

@dataclass(frozen=True)
class Conversation:
    """The conversation a background helper reports back to."""

    session_id: str
    #: The key its turns and parked question are held under (`request.agent_id or "default"`).
    agent_key: str
    #: The id its turns are saved under: the agent the turn resolved.
    agent_id: str
    tenant: str = ""
    #: Where its follow-up's answer is sent, when the conversation is a member's messaging channel (SUB1b):
    #: OSS `{"channel_id", "chat_id"}`, a host's own shape otherwise. None for the app's chat.
    reply_to: dict | None = field(default=None, compare=False, hash=False)

    @property
    def key(self) -> tuple:
        return (self.tenant, self.session_id, self.agent_key)

    def link(self) -> dict:
        link = {"parent_session_id": self.session_id, "agent_key": self.agent_key,
                "parent_agent_id": self.agent_id, "tenant": self.tenant}
        if self.reply_to:
            link["reply_to"] = dict(self.reply_to)
        return link

    @classmethod
    def from_link(cls, link: dict) -> "Conversation":
        reply_to = link.get("reply_to")
        return cls(session_id=str(link.get("parent_session_id") or ""), agent_key=str(link.get("agent_key") or
                   "default"), agent_id=str(link.get("parent_agent_id") or "default"),
                   tenant=str(link.get("tenant") or ""), reply_to=dict(reply_to) if isinstance(reply_to, dict)
                   else None)


INBOX_HEADING = "Helper results (since your last step)"

#: Each live root chat turn's inbox, by conversation (`Conversation.key`).
_OPEN: dict[tuple, "Inbox"] = {}
#: Who waits to deliver into a conversation, woken when an inbox opens or closes there.
_CHANGED: dict[tuple, asyncio.Event] = {}


def _changed(key: tuple) -> None:
    event = _CHANGED.pop(key, None)
    if event is not None:
        event.set()


class Inbox:
    """What a running root chat turn's helpers send back, read between its model calls.

    One per turn, open from the turn's start until it answers, stops or parks.
    `drain` hands the engine what arrived since it last looked, which stays "in
    flight" until the answer that read it is saved (`settle`); `close` returns
    whatever is not settled — arrived late, or read by a call that failed or was
    stopped — so it goes back another way, and `park` keeps it with a parked
    question. No method awaits: each is whole on the event loop, so an item is
    taken by exactly one of them.
    """

    def __init__(self, conversation: Conversation, state: dict | None = None):
        self.conversation = conversation
        state = state if isinstance(state, dict) else {}
        self._pending: list[dict] = [dict(i) for i in state.get("pending") or [] if isinstance(i, dict)]
        self._in_flight: list[dict] = [dict(i) for i in state.get("in_flight") or [] if isinstance(i, dict)]
        self._open = False

    def open(self) -> "Inbox":
        self._open = True
        _OPEN[self.conversation.key] = self
        _changed(self.conversation.key)
        return self

    @property
    def is_open(self) -> bool:
        return self._open

    def put(self, item: dict) -> bool:
        if not self._open:
            return False
        self._pending.append(dict(item))
        return True

    def add(self, items: list[dict]) -> None:
        """Items the turn starts with (moved past with a question), read at its first look."""
        self._pending.extend(dict(i) for i in items)

    def drain(self) -> list[dict]:
        items, self._pending = self._pending, []
        self._in_flight.extend(items)
        return items

    def in_flight_ids(self) -> list[str]:
        return [str(i.get("helper_id")) for i in self._in_flight if i.get("kind") == "helper_result"]

    def settle(self) -> None:
        """The answer that read what is in flight is saved: it has arrived."""
        self._in_flight = []

    def _shut(self) -> None:
        if self._open:
            self._open = False
            if _OPEN.get(self.conversation.key) is self:
                _OPEN.pop(self.conversation.key, None)
            _changed(self.conversation.key)

    def close(self) -> list[dict]:
        """Shut, and hand back what did not arrive: the unread, and the read but never answered."""
        self._shut()
        left, self._pending, self._in_flight = [*self._in_flight, *self._pending], [], []
        return [i for i in left if i.get("kind") == "helper_result"]

    def park(self) -> dict:
        """Shut, keeping what is not settled for the turn a parked question stopped."""
        self._shut()
        state = {"pending": list(self._pending), "in_flight": list(self._in_flight)}
        self._pending, self._in_flight = [], []
        return state


def render_inbox(items: list[dict]) -> str:
    """The automated block a turn reads its helpers' results in, in the order they arrived."""
    lines = [f"\n[{INBOX_HEADING} — automated, not from the person]"]
    for item in items:
        if item.get("kind") == "helper_stopped":
            lines.append(f"Your helper '{item.get('name')}' was stopped by the person before it finished.")
            continue
        status = _STATUS_WORDS.get(str(item.get("status")), str(item.get("status") or "finished"))
        lines.append(f"From your helper '{item.get('name')}' (it {status}):\n{item.get('text') or ''}")
    return "\n".join(lines) + "\n"


def result_item(helper: Helper) -> dict:
    return {"kind": "helper_result", "helper_id": helper.id, "name": helper.name, "status": helper.status,
            "text": helper.result_for_parent(), "session_id": helper.session_id, "at": time.time()}


def automated_mark(item: dict) -> dict:
    """What a follow-up's question carries: whose result it is, never the person's words."""
    return {"kind": "helper_result", "helper_id": item.get("helper_id"), "name": item.get("name"),
            "status": item.get("status"), "session_id": item.get("session_id")}


def automated_opening(mark: dict, text: str) -> str:
    """How a follow-up's transcript starts, in place of "User Request:"."""
    status = _STATUS_WORDS.get(str(mark.get("status")), "finished")
    return (f"Automated input, not from the person: your helper '{mark.get('name')}' {status}. "
            f"Tell the person what it found, as part of this conversation.\nIts result:\n{text}\n")


def automated_history(mark: dict, text: str) -> str:
    """An automated question as a later turn's history reads it."""
    return f"[Automated: the result of your helper '{mark.get('name')}', not something the person said]\n{text}"


# ── in the background ────────────────────────────────────────────────────────

#: Live background helpers by id.
_BACKGROUND: dict[str, Helper] = {}
#: Deliveries in flight (held, so none is collected before it ends).
_DELIVERING: set[asyncio.Task] = set()
#: Results handed on durably in this process (parked, or a follow-up started), by helper id.
_DELIVERED: set[str] = set()
#: One delivery at a time per conversation.
_LOCKS: dict[tuple, asyncio.Lock] = {}


def _detached() -> contextvars.Context:
    """A copy of this context without the turn's meters, for work that outlives the turn."""
    from core import chat_approval, turn_usage, visitor
    from core.orchestration import step_usage

    ctx = contextvars.copy_context()

    def reset() -> None:
        turn_usage._meter.set(None)
        step_usage._STEP.set(None)
        visitor._current.set(None)
        chat_approval._nobody_to_ask.set(False)

    ctx.run(reset)
    return ctx


# ── a host that runs them elsewhere (SUB1b) ──────────────────────────────────
#
# In one process (OSS) a background helper is a task the server holds, and its
# result is handed to a turn in the same process. A fleet (cloud) runs a turn in
# any worker, so a host installs itself here: it starts a background helper where
# it runs it (cloud: a chat job of its own), says how many of a conversation's
# helpers work elsewhere, and takes a result back (cloud: one follow-up job). With
# none installed, everything here is in-process, as it was.


class Refused(Exception):
    """A background helper the host would not start (a cap, no credits): its words are what the agent reads."""


_HOST = None


def install(host) -> None:
    """Install the host: `start_background(helper, *, conversation, live_here, untrusted)`, `elsewhere(key)`
    and `deliver(conversation, items)`, all async."""
    global _HOST
    _HOST = host


def uninstall() -> None:
    global _HOST
    _HOST = None


async def elsewhere(key: tuple) -> int:
    """How many of this conversation's helpers work in other processes (0 with no host, or in a workflow step)."""
    if _HOST is None or not key or key[0] != "conversation":
        return 0
    try:
        return max(0, int(await _HOST.elsewhere(key)))
    except Exception as exc:  # noqa: BLE001 — the host's own start claim is the hard cap
        _log.warning(f"counting helpers elsewhere: {exc}")
        return 0


def prepare_background(helper: Helper, conversation: Conversation) -> None:
    """Make a built helper a background one: its own conversation, its time limit, where it reports."""
    helper.mode = "background"
    helper.limit_seconds = BACKGROUND_SECONDS
    helper.session_id = helper.session_id or f"{SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
    helper.conversation = conversation


async def open_row(helper: Helper, conversation: Conversation) -> None:
    """Its row, linked to its parent's conversation, `running`: a restart from here on is found and told."""
    from core.store import sessions as store

    await store.append_turn(helper.session_id, helper.id, [{
        "role": "user", "content": helper.task,
        "automated": {"kind": "helper_brief", "helper_id": helper.id, "name": helper.name,
                      "tools": list(helper.tools), "model": helper.model, **conversation.link()},
    }])
    await store.set_status(helper.session_id, helper.id, "running")


async def start_background(helper: Helper, *, conversation: Conversation, key: tuple, server_module,
                           untrusted: bool) -> None:
    """Start a reserved helper on a chat turn of its own, in the background; it reports back when it ends.

    With a host installed the host starts it where it runs it, and counts it there: it leaves this process's
    count. A host that will not start it raises `Refused`.
    """
    from core import chat_turns

    prepare_background(helper, conversation)
    helper.untrusted = bool(untrusted)
    if _HOST is not None:
        await _HOST.start_background(helper, conversation=conversation,
                                     live_here=max(0, len(live(key)) - 1), untrusted=bool(untrusted))
        release(key, helper.id)
        return
    await open_row(helper, conversation)
    with chat_turns.claim(helper.session_id, message=helper.task, agent_key=helper.id) as turn:
        turn.start(_background(helper, server_module=server_module, untrusted=untrusted), context=_detached())
    _BACKGROUND[helper.id] = helper

    def ended(_task: asyncio.Task) -> None:
        release(key, helper.id)
        _BACKGROUND.pop(helper.id, None)

    turn.task.add_done_callback(ended)


async def _background(helper: Helper, *, server_module, untrusted: bool) -> AsyncIterator[dict]:
    from core.scale import run_lease

    conversation = helper.conversation
    try:
        async with run_lease.hold(f"helper_{helper.id}", kind="helper"):
            async for event in run(helper, server_module=server_module, session_id=conversation.session_id,
                                   source="chat", run_id=None, untrusted=untrusted):
                yield event
    finally:
        await _finish(helper)


async def run_detached(helper: Helper, *, server_module) -> AsyncIterator[dict]:
    """A background helper's run where its host drives it (a fleet job): its events, as `run`'s. The host saves
    how it ended (`save_ending`) and reports it (`report`)."""
    async for event in run(helper, server_module=server_module, session_id=helper.conversation.session_id,
                           source="chat", run_id=None, untrusted=helper.untrusted):
        yield event


async def _finish(helper: Helper) -> None:
    """Save how a background helper ended, then send its result back (in a task of its own)."""
    await save_ending(helper)
    await report(helper)


async def save_ending(helper: Helper) -> None:
    """Save how a background helper ended, on its own row: its record and its status."""
    from core.session import _now
    from core.store import sessions as store

    if helper.status == "running":
        helper.status, helper.error = "failed", helper.error or "it stopped unexpectedly"
    helper.ended_at = helper.ended_at or time.time()
    try:
        await store.append_turn(helper.session_id, helper.id, [{
            "role": "assistant", "content": helper.answer or "", "timestamp": _now(), "helper": helper.record()}])
        await store.set_status(helper.session_id, helper.id, helper.status)
    except Exception as exc:  # noqa: BLE001 — the result still goes back
        _log.warning(f"saving background helper {helper.id}: {exc}")


async def report(helper: Helper, *, said: str | None = None) -> None:
    """Send a background helper's result back to the agent that sent it — never one a person stopped.

    `said` is a reply posted with it, in place of a model call (a helper a restart cut short). With a host
    installed the host takes it (cloud: one follow-up job), and this waits until it has.
    """
    conversation = helper.conversation
    if helper.status == "cancelled":
        # Stopped by the person, who knows: no follow-up. A turn still reading there is told.
        inbox = _OPEN.get(conversation.key)
        if inbox is not None:
            inbox.put({"kind": "helper_stopped", "helper_id": helper.id, "name": helper.name})
        return
    item = result_item(helper)
    if said:
        item["said"] = said
    if _HOST is not None:
        await _HOST.deliver(conversation, [item])
        return
    redeliver(conversation, [item])


def redeliver(conversation: Conversation, items: list[dict]) -> None:
    """Send results back, each exactly once, in tasks of their own (the caller may be ending)."""
    if not items:
        return
    if _HOST is not None:
        task = asyncio.create_task(_HOST.deliver(conversation, [dict(i) for i in items]),
                                   name="helper-deliver-host", context=_detached())
        _DELIVERING.add(task)
        task.add_done_callback(_delivered)
        return
    for item in items:
        task = asyncio.create_task(deliver(conversation, item), name=f"helper-deliver-{item.get('helper_id')}",
                                   context=_detached())
        _DELIVERING.add(task)
        task.add_done_callback(_delivered)


def _delivered(task: asyncio.Task) -> None:
    _DELIVERING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _log.error(f"delivering a helper's result failed: {task.exception()!r}")


async def already_delivered(conversation: Conversation, helper_id: str) -> bool:
    """Whether this conversation already holds the result: as a follow-up's question, as read by a turn, or kept
    with its parked question."""
    from core.store import sessions as store

    if helper_id in _DELIVERED:
        return True
    row = await store.get(conversation.session_id, conversation.agent_id)
    for message in (row or {}).get("messages") or []:
        if (message.get("automated") or {}).get("helper_id") == helper_id:
            return True
        if helper_id in (message.get("inbox_read") or []):
            return True
    pending = await store.get_pending_approval(conversation.session_id, conversation.agent_key)
    inbox = ((pending or {}).get("progress") or {}).get("inbox") or {}
    return any(i.get("helper_id") == helper_id for i in [*(inbox.get("pending") or []),
                                                          *(inbox.get("in_flight") or [])])


async def _until_changed(conversation: Conversation, timeout: float = 5.0) -> None:
    """Until the conversation comes free or an inbox opens or closes there (the timeout is a backstop)."""
    from core import chat_turns

    event = _CHANGED.get(conversation.key)
    if event is None:
        event = _CHANGED[conversation.key] = asyncio.Event()
    waits = {asyncio.ensure_future(chat_turns.until_free(conversation.session_id)),
             asyncio.ensure_future(event.wait())}
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()


async def deliver(conversation: Conversation, item: dict) -> str:
    """Send one result back, exactly once: an open inbox, else a parked question, else one follow-up turn.

    Returns where it went (`inbox`, `parked`, `follow_up`), or `already`.
    """
    from core import chat_turns
    from core.store import sessions as store

    helper_id = str(item.get("helper_id"))
    lock = _LOCKS.setdefault(conversation.key, asyncio.Lock())
    async with lock:
        while True:
            if await already_delivered(conversation, helper_id):
                return "already"
            inbox = _OPEN.get(conversation.key)
            if inbox is not None and inbox.put(item):
                return "inbox"
            if chat_turns.busy(conversation.session_id) is not None:
                # A turn holds it: one that opens an inbox takes the result, else wait until it ends.
                await _until_changed(conversation)
                continue
            if await store.add_to_parked(conversation.session_id, conversation.agent_key, item):
                _DELIVERED.add(helper_id)
                return "parked"
            try:
                start_follow_up(conversation, item)
            except chat_turns.TurnAlreadyRunning:
                continue  # a turn started meanwhile: look again
            _DELIVERED.add(helper_id)
            return "follow_up"


def start_follow_up(conversation: Conversation, item: dict) -> None:
    """Start the agent's one follow-up turn on the conversation, its question the result. Raises
    `TurnAlreadyRunning`, from the claim. In a member's messaging channel, its answer is sent to the same chat."""
    from core import chat_turns

    mark = automated_mark(item)
    if conversation.reply_to:
        with chat_turns.claim(conversation.session_id, message=str(item.get("text") or ""),
                              agent_key=conversation.agent_key) as turn:
            turn.automated = mark
            turn.start(_in_the_channel(conversation, item, mark), context=_detached())
        return

    import core.server as _server
    from core.models import ChatRequest
    from core.routes.chat import _event_stream

    request = ChatRequest(message=str(item.get("text") or ""), session_id=conversation.session_id,
                          agent_id=None if conversation.agent_key == "default" else conversation.agent_key)
    request._app_chat = True
    request._automated = mark
    with chat_turns.claim(conversation.session_id, message=request.message, agent_key=conversation.agent_key) as turn:
        turn.automated = mark
        turn.start(_event_stream(request, _server, None, expand_mentions=False))


async def _in_the_channel(conversation: Conversation, item: dict, mark: dict) -> AsyncIterator[str]:
    """A follow-up in a member's messaging channel: the agent's turn, its answer sent to the chat (SUB1b).

    Held by the conversation's claim, so a second result waits for it; yields nothing for the turn's log.
    """
    from core.messaging.manager import follow_up_in_channel

    await follow_up_in_channel(conversation, str(item.get("text") or ""), mark)
    return
    yield  # an async generator: what `chat_turns` drives


async def stop(session_id: str, helper_id: str) -> bool:
    """Stop one helper in this conversation: one its parent waits for ends at its next turn, one in the
    background as its turn's Stop ends it. False when no such helper is working there."""
    from core import chat_turns

    helper = find(helper_id, session_id)
    if helper is None:
        return False
    if helper.mode == "background" and helper.session_id:
        return await chat_turns.stop(helper.session_id)
    helper.stop_requested = True
    return True


# ── a restart ────────────────────────────────────────────────────────────────

RESTARTED = ("My helper '{name}' stopped when the server restarted, so its work was lost. "
             "Ask me again if you still need it.")
FINISHED_DURING_RESTART = "My helper '{name}' finished while the server was restarting; its result is above."


async def sweep_interrupted(redis=None) -> int:
    """At boot: tell each conversation about the background helpers a restart cut short (S3).

    A helper still `running` with no live lease (scale mode: another process
    may hold it) is failed, and its parent's conversation gets one automated
    turn saying so, with no model call. One that had ended but whose result
    never arrived gets it the same way. Each is looked at once (`settled`).
    Reads this tenant's rows: a self-hosted install has one.
    """
    from core.scale import run_lease
    from core.session import _now
    from core.store import sessions as store

    told = 0
    for row in await store.with_status(SESSION_PREFIX, ("running", "completed", "failed", "timed_out")):
        messages = row.get("messages") or []
        brief = next((m.get("automated") for m in messages
                      if (m.get("automated") or {}).get("kind") == "helper_brief"), None)
        if not brief:
            continue
        helper_id, name = row["agent_id"], brief.get("name") or "a helper"
        conversation = Conversation.from_link(brief)
        if row["status"] == "running":
            if redis is not None and await run_lease.held(f"helper_{helper_id}", redis):
                continue
            item = {"helper_id": helper_id, "name": name, "status": "failed",
                    "text": "It stopped when the server restarted, before it finished."}
            said = RESTARTED.format(name=name)
        else:
            record = next((m.get("helper") for m in reversed(messages) if m.get("helper")), None) or {}
            helper = Helper(id=helper_id, name=name, task=str(record.get("task") or ""),
                            tools=list(record.get("tools") or []), withheld=list(record.get("withheld") or []),
                            model=str(record.get("model") or ""), agent={}, status=row["status"],
                            calls=list(record.get("calls") or []), answer=str(record.get("answer") or ""),
                            error=str(record.get("error") or ""))
            item = {"helper_id": helper_id, "name": name, "status": row["status"],
                    "text": helper.result_for_parent()}
            said = FINISHED_DURING_RESTART.format(name=name)
        try:
            if not await already_delivered(conversation, helper_id):
                await store.append_turn(conversation.session_id, conversation.agent_id, [
                    {"role": "user", "content": item["text"], "automated": automated_mark(item)},
                    {"role": "assistant", "content": said, "tools": [], "timestamp": _now()},
                ])
                told += 1
            await store.set_status(row["session_id"], helper_id, "settled")
        except Exception as exc:  # noqa: BLE001 — the next one still gets its turn
            _log.warning(f"telling the conversation about helper {helper_id}: {exc}")
    return told

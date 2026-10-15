"""Teams: a lead and members working in parallel (TEAM1a, D283; 20-agent-teams.md Part A).

A Team agent is a `delegate` whose `team` block says `mode: "team"` (Q1). Without the block it is
today's delegate, one member at a time through `delegate_to_agent` (D204). In team mode, in the app's
chat, a member's channel and OAI1 (a workflow step stays one at a time until TEAM1e; a visitor's turn
and a phone call run the lead alone, Q9):

* **The lead** gets `assign`, `message`, `wait` and `forward` in place of `delegate_to_agent`, and keeps
  its own tools (Q1b). `assign(member, task)` returns at once: the task becomes a card on the team's
  run board (BOARD1a's internal `run` scope, which works with Boards off), claimed by that member, and
  the member starts — or waits for a slot while `at_once` are working (Q2, Q2a).
* **Members** run inside the lead's turn, each a nested `run_agent_step` driven by a task of its own
  (a generator is closed by the task that entered it, as a helper's is): their own tools and policy,
  the lead's taint (F279), the lead's model when they have none (F538), a Stop of their own. They get
  `message`, `wait` and `board_update` on their own card only (a code check: the store lets any holder
  update an unclaimed card).
* **The inbox.** Members' results, questions and messages, the person's messages and the guards' notices
  reach the lead in its own `TeamInbox` — SUB1a's `Inbox`, never registered as a conversation's — read
  between model calls as one automated block (Q3). An item marks the reader as having read untrusted
  content only when its writer had (Naveen, TEAM1a: per item). Nothing in it ever goes back through a
  helper's follow-up.
* **Seen and kept.** A member's events go out wrapped (`member_event`), between `member_started` and
  `member_finished` (its record), as a helper's do, so nothing that streams an agent's own events (OAI1,
  a call, a widget) reads them as the lead's. The task list goes out as `team_tasks`.
* **Approvals (TEAM1c, Q6).** Each member asks on its own: a member that needs a person's approval waits
  alone and the others keep working. Its question is one of a list, each with its own id
  (`chat_approval.question_id`), answered in its lane while the turn runs (`Team.answer`) or after the
  turn has paused, which it does only once nobody is left working. An answer resumes only that member.
* **At its end** the open cards close as failed, the holders are released, the list is kept with the
  turn (`team`), and the run board is deleted (Naveen, TEAM1a). A parked turn keeps its board.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from core.log import get_logger
from core.tenancy import get_tenant

_log = get_logger(__name__)

#: The keys and their shipped defaults. `core.config._DEFAULTS` carries the same values; a test holds them equal.
DEFAULTS: dict[str, Any] = {
    # The most members at once, whatever a Team agent asks for (Q2a: default 10, the owner's to change).
    "team_at_once_ceiling": 10,
}

#: The lead's tools in team mode, and a member's.
ASSIGN, MESSAGE, WAIT, FORWARD, BOARD_UPDATE = "assign", "message", "wait", "forward", "board_update"
LEAD_TOOLS = (ASSIGN, MESSAGE, WAIT, FORWARD)
MEMBER_TOOLS = (MESSAGE, WAIT, BOARD_UPDATE)

#: A member's turns when its agent sets none (as a delegate's).
MEMBER_MAX_TURNS = 15
#: The longest one `wait` idles, and its default.
WAIT_MAX_SECONDS = 120
WAIT_DEFAULT_SECONDS = 30
#: An answer and a result kept with the turn, as a saved turn clips them.
ANSWER_CHARS = 4_000

# ── the guards, in code (Q4, Q8) ──
#: Messages one member may send in one assignment.
MESSAGE_BUDGET = 6
#: Messages two members may exchange while the task list does not move; then "take it to the lead".
PAIR_CAP = 3
#: Lead steps with no task moving before it is told to re-plan or finish, and before `assign` is refused.
STALL_NOTICE_AT, STALL_STOP_AT = 3, 6
#: How long everyone must stay waiting before the lead is told: a reply assigning several starts them one by one.
EVERYONE_WAITING_SETTLE = 1.0
#: A model call's output allowance when its agent sets no maximum (CE1's high prior, Q8), and the
#: characters a token is reckoned at for the call's known input.
OUTPUT_ALLOWANCE, CHARS_PER_TOKEN = 1_600, 4
#: What `assign` (and a helper) reads at the task budget (Q8's words).
BUDGET_USED = "the team's budget of {n} tasks is used; finish with what you have."

#: What a member's lane shows of its turn, wrapped (the same as a helper's, plus its own helpers).
SHOWN = frozenset({
    "status", "thinking", "tool_execution", "tool_result", "llm_reasoning", "llm_thought",
    "answer_delta", "answer_reset", "truncated_output", "context_trimmed", "context_compact",
    "tool_call_unparsed", "reasoning_only", "repeat_stopped", "knowledge_search", "knowledge_read",
    "helper_started", "helper_event", "helper_finished", "human_input_required",
})

_STATUS_WORDS = {
    "completed": "finished",
    "cancelled": "was stopped before it finished",
    "failed": "failed",
    "waiting_for_person": "is waiting for a person's approval",
}


# ── settings ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TeamConfig:
    """A Team agent's settings as this install allows them."""

    at_once: int
    task_budget: int
    spend_limit_usd: float | None


def config_of(agent: Mapping[str, Any], *, settings: Mapping[str, Any] | None = None) -> TeamConfig | None:
    """The team settings of an agent whose members work as a team; None for every other agent."""
    if (agent or {}).get("type") != "delegate":
        return None
    block = agent.get("team")
    if not isinstance(block, Mapping) or block.get("mode") != "team":
        return None
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    from core.models import AgentTeam
    knobs = AgentTeam(**block)
    ceiling = settings.get("team_at_once_ceiling")
    ceiling = DEFAULTS["team_at_once_ceiling"] if ceiling is None else max(1, int(ceiling))
    return TeamConfig(at_once=min(knobs.at_once, ceiling), task_budget=knobs.task_budget,
                      spend_limit_usd=knobs.spend_limit_usd)


# ── the inbox ────────────────────────────────────────────────────────────────

def _inbox_class():
    from core.helpers import Inbox

    class TeamInbox(Inbox):
        """SUB1a's inbox, for one lead or one member of one team: never a conversation's, so nothing in it is
        counted as a helper's result arriving, and nothing goes back through a helper's follow-up."""

        def __init__(self, state: dict | None = None):
            super().__init__(None, state)  # type: ignore[arg-type]
            self._open = True
            self.wake = asyncio.Event()

        def open(self) -> "TeamInbox":
            self._open = True
            return self

        def _shut(self) -> None:
            self._open = False

        def put(self, item: dict) -> bool:
            if not self._open:
                return False
            self._pending.append({"at": time.time(), **item})
            self.wake.set()
            return True

        def drain(self) -> list[dict]:
            self.wake.clear()
            return super().drain()

        def has_pending(self) -> bool:
            return bool(self._pending)

        def close(self) -> list[dict]:
            self._shut()
            left, self._pending, self._in_flight = [*self._in_flight, *self._pending], [], []
            return left

    return TeamInbox


def note_cost(model: str, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
              cache_write_tokens: int = 0) -> None:
    """Count one model call toward the team whose turn made it (lead, member or helper), if any. Never raises."""
    team = current.get()
    if team is None:
        return
    try:
        from core.usage_tracker import calculate_cost
        team.spent_usd += calculate_cost(model, int(input_tokens or 0), int(output_tokens or 0),
                                         cache_read_tokens=int(cache_read_tokens or 0),
                                         cache_write_tokens=int(cache_write_tokens or 0))
    except Exception:  # noqa: BLE001
        pass


def new_inbox(state: dict | None = None):
    return _inbox_class()(state)


def render_inbox(items: list[dict], *, for_lead: bool) -> str:
    """The automated block a lead (or a member) reads its team's news in, in the order it arrived."""
    heading = ("Team inbox (since your last step) — automated, not from the person, unless a line says so"
               if for_lead else "Messages for you (since your last step) — from your team, not from the person")
    lines = [f"\n[{heading}]"]
    for item in items:
        kind = item.get("kind")
        if kind == "member_result":
            status = _STATUS_WORDS.get(str(item.get("status")), str(item.get("status") or "finished"))
            lines.append(f"From {item.get('member')} (it {status}):\n{item.get('text') or ''}")
        elif kind == "member_message":
            lines.append(f"Message from {item.get('from')}: {item.get('text') or ''}")
        elif kind == "lead_message":
            lines.append(f"Message from the lead: {item.get('text') or ''}")
        elif kind == "person_message":
            lines.append(f"From the person (their own words): {item.get('text') or ''}")
        elif kind == "waiting_for_person":
            lines.append(f"{item.get('member')} is waiting for a person's approval to run {item.get('tool')}. "
                         "The others carry on; this turn pauses for the person once nobody else is working.")
        elif kind == "person_answered":
            lines.append(f"The person {'allowed' if item.get('approved') else 'refused'} {item.get('member')}'s "
                         f"call to {item.get('tool')}; it carries on, and its result arrives here.")
        else:  # a guard's notice
            lines.append(f"Notice: {item.get('text') or ''}")
    return "\n".join(lines) + "\n"


# ── one member's work ────────────────────────────────────────────────────────

@dataclass
class Member:
    """One assignment: a member agent working on one task of the team's list."""

    id: str
    agent_id: str
    name: str
    task: str
    card_id: str = ""
    card_version: int = 0
    model: str = ""
    status: str = "queued"     # queued | running | waiting_for_person | completed | failed | cancelled
    stop_requested: bool = False
    waiting: bool = False       # inside its own `wait`
    tainted: bool = False       # it started marked, or read content nobody typed
    started_at: float = 0.0
    ended_at: float | None = None
    calls: list[dict] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    messages_sent: int = 0
    inbox: Any = None
    progress: dict = field(default_factory=dict)
    approval: dict | None = None
    asked: dict | None = None
    asked_at: float = 0.0       # when it began waiting on a person: questions are listed in the order asked
    resume_state: dict | None = None
    task_handle: asyncio.Task | None = None

    @property
    def live(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.ended_at or time.time()) - self.started_at, 1)

    def tag(self, event: dict) -> dict:
        return {"type": "member_event", "member_id": self.id, "member_name": self.name, "event": event}

    @property
    def question_id(self) -> str | None:
        """Its question's own id while it waits on a person (TEAM1c), else None."""
        if self.status != "waiting_for_person" or not self.approval:
            return None
        from core.chat_approval import question_id
        return question_id(self.id, self.approval.get("approval_id"))

    def question(self) -> dict:
        """Its question as a parked turn lists it (TEAM1c)."""
        approval = self.approval or {}
        return {"question_id": self.question_id, "approval_id": approval.get("approval_id"),
                "member_id": self.id, "member_name": self.name, "tool_name": approval.get("tool_name"),
                "tool_args": approval.get("tool_args") or {}, "category": approval.get("category")}

    def record(self) -> dict:
        from core.session import _clip

        kept = {"id": self.id, "agent_id": self.agent_id, "name": self.name, "task": self.task,
                "card_id": self.card_id, "model": self.model, "status": self.status, "seconds": self.seconds,
                "calls": list(self.calls), "answer": _clip(self.answer, ANSWER_CHARS)}
        if self.error:
            kept["error"] = _clip(self.error, 500)
        return kept

    def state(self) -> dict:
        kept = self.record()
        kept.update(card_version=self.card_version, tainted=self.tainted, messages_sent=self.messages_sent,
                    progress=self.progress, approval=self.approval, asked=self.asked, asked_at=self.asked_at,
                    inbox=self.inbox.park() if self.inbox is not None and self.status == "waiting_for_person"
                    else None)
        return kept

    def result_for_lead(self) -> str:
        if self.status == "completed":
            return self.answer or "(It finished without an answer.)"
        said = f"It {_STATUS_WORDS.get(self.status, self.status)}"
        if self.status == "failed" and self.error:
            said += f": {self.error}"
        parts = [said + "."]
        if self.calls:
            done = "\n".join(f"- {c.get('tool')}: {str(c.get('result') or '')[:300]}" for c in self.calls[-10:])
            parts.append(f"Its calls before it stopped:\n{done}")
        if self.answer:
            parts.append(self.answer)
        return "\n\n".join(parts)


@dataclass
class Seat:
    """What a member's own turn knows of its team: handed to its `run_agent_step`."""

    team: "Team"
    member: Member


# ── the team ─────────────────────────────────────────────────────────────────

#: The teams working in this process, by (tenant, conversation): a person's Stop or message reaches a member.
_LIVE: dict[tuple, "Team"] = {}

#: The team whose turn is running (the lead's and every member's context): a helper counts against it.
current: contextvars.ContextVar["Team | None"] = contextvars.ContextVar("synapse_team", default=None)


def find(session_id: str) -> "Team | None":
    return _LIVE.get((get_tenant() or "", session_id))


class Refused(Exception):
    """A team tool's refusal: its text is what the model reads."""


class Team:
    """One Team agent's turn: its members, its task list, its inbox."""

    def __init__(self, *, lead: dict, members: dict[str, dict], config: TeamConfig, session_id: str,
                 source: str, run_id: str | None, server_module, lead_model: str,
                 should_stop: Callable[[], Awaitable[bool]] | None, can_park: bool, knowledge_turn=None,
                 state: dict | None = None, agent_key: str = "default"):
        self.lead = lead
        self.roster = members
        self.config = config
        self.session_id = session_id
        #: The conversation's agent key (its turns' row): answers reach this team by it (TEAM1c).
        self.agent_key = agent_key
        self.source = source
        self.run_id = run_id
        self.server_module = server_module
        self.lead_model = lead_model
        self.lead_should_stop = should_stop
        self.can_park = can_park
        self.knowledge_turn = knowledge_turn
        state = state or {}
        self.key = str(state.get("key") or f"team:{uuid.uuid4().hex[:16]}")
        self.board_id: str = str(state.get("board_id") or "")
        self.tasks_used: int = int(state.get("tasks_used") or 0)
        self.members: dict[str, Member] = {}
        for kept in state.get("members") or []:
            m = Member(id=kept["id"], agent_id=kept.get("agent_id") or "", name=kept.get("name") or "",
                       task=kept.get("task") or "", card_id=kept.get("card_id") or "",
                       card_version=int(kept.get("card_version") or 0), model=kept.get("model") or "",
                       status=kept.get("status") or "completed", calls=list(kept.get("calls") or []),
                       answer=kept.get("answer") or "", error=kept.get("error") or "",
                       tainted=bool(kept.get("tainted")), messages_sent=int(kept.get("messages_sent") or 0),
                       progress=dict(kept.get("progress") or {}), approval=kept.get("approval"),
                       asked=kept.get("asked"), asked_at=float(kept.get("asked_at") or 0.0))
            m.inbox = new_inbox(kept.get("inbox"))
            self.members[m.id] = m
        # A turn parked before TEAM1c named its one waiting member `parked_id`: its status says the same.
        self.inbox = new_inbox(state.get("inbox"))
        self.tasks: dict[str, dict] = {t["card_id"]: dict(t) for t in state.get("tasks") or []}
        self.progress_mark: int = int(state.get("progress_mark") or 0)
        #: The questions an earlier pause of this turn already asked (TEAM1c): a host asks again only the new ones.
        self.paused_on: set[str] = set(state.get("paused_on") or [])
        self._events: list[dict] = []
        self.events_wake = asyncio.Event()
        self.ended = False
        #: Set by the lead's turn when it parks on a member's question: the board and that member are kept.
        self.parking = False
        #: A finished member's card being closed: the end waits for it before the board goes.
        self._closing: set[asyncio.Future] = set()
        # The guards (Q4, Q8).
        self.stall: int = int(state.get("stall") or 0)
        self._stall_mark: int = self.progress_mark
        self._pairs: dict[frozenset, tuple[int, int]] = {}
        self._all_waiting_said = False
        #: What the team's model calls cost so far (lead, members, helpers), and what one more of the
        #: lead's would: kept back so the lead can always finish (Q8).
        self.spent_usd: float = float(state.get("spent_usd") or 0.0)
        self.lead_reserve: float = 0.0

    # ── what the lead's turn reads ──
    @property
    def wake(self) -> asyncio.Event:
        return self.events_wake

    def drain(self) -> list[dict]:
        """The members' events since the last look, for the lead's turn to yield (ModelWait's side channel)."""
        self.events_wake.clear()
        out, self._events = self._events, []
        return out

    def _emit(self, event: dict) -> None:
        self._events.append(event)
        self.events_wake.set()

    def live_members(self) -> list[Member]:
        return [m for m in self.members.values() if m.live]

    def running(self) -> list[Member]:
        return [m for m in self.members.values() if m.status == "running"]

    def waiting(self) -> list[Member]:
        """The members waiting on a person, in the order they asked (TEAM1c)."""
        return sorted((m for m in self.members.values() if m.status == "waiting_for_person"),
                      key=lambda m: m.asked_at)

    def questions(self) -> list[dict]:
        """The waiting members' questions, as a pause lists them: one an earlier pause asked is `asked_before`."""
        return [{**q, "asked_before": q["question_id"] in self.paused_on} for q in (m.question() for m in self.waiting())]

    def note_paused(self) -> None:
        self.paused_on |= {m.question_id for m in self.waiting() if m.question_id}

    def register(self) -> "Team":
        _LIVE[(get_tenant() or "", self.session_id)] = self
        # Answers that arrived while this turn was starting, or while its last one paused (TEAM1c).
        from core.chat_approval import take_answers
        for verdict in take_answers(self.session_id, self.agent_key):
            self.answer(verdict.get("question_id") or "", approve=bool(verdict.get("approve")),
                        reason=str(verdict.get("reason") or ""))
        return self

    def state(self) -> dict:
        """What a parked turn keeps, to pick the team up again."""
        return {"key": self.key, "board_id": self.board_id, "tasks_used": self.tasks_used,
                "members": [m.state() for m in self.members.values()],
                "inbox": {"pending": list(self.inbox._pending), "in_flight": list(self.inbox._in_flight)},
                "tasks": list(self.tasks.values()), "progress_mark": self.progress_mark, "stall": self.stall,
                "paused_on": sorted(self.paused_on),
                "spent_usd": self.spent_usd}

    def records(self) -> list[dict]:
        return [m.record() for m in self.members.values() if m.status != "queued" or m.started_at]

    def task_list(self) -> list[dict]:
        return list(self.tasks.values())

    def render_tasks(self) -> str:
        if not self.tasks:
            return ""
        lines = ["The team's task list (yours to plan; each member updates only its own):"]
        for t in self.tasks.values():
            lines.append(f"- [{t['status']}] {t['title']} — {t['member']}")
        return "\n".join(lines)

    def summary(self) -> dict:
        return {"tasks": self.task_list(), "tasks_used": self.tasks_used,
                "answered_alone": self.tasks_used == 0, "spent_usd": round(self.spent_usd, 6)}

    # ── the guards ──
    def lead_step(self) -> None:
        """Before each of the lead's model calls: the stall counter (Q4, Magentic-One's)."""
        if not self.members:
            return
        if self.progress_mark == self._stall_mark:
            self.stall += 1
        else:
            self.stall, self._stall_mark = 0, self.progress_mark
        if self.stall == STALL_NOTICE_AT:
            self.inbox.put({"kind": "guard_notice", "text": (
                f"No task has moved in your last {STALL_NOTICE_AT} steps. Re-plan (message a member, or assign the "
                "work differently) or finish with what you have.")})
        elif self.stall == STALL_STOP_AT:
            self.inbox.put({"kind": "guard_notice", "text": (
                f"No task has moved in your last {STALL_STOP_AT} steps, so no more work can be assigned. Finish now "
                "with what you have.")})

    def take_helper(self) -> str | None:
        """A helper sent from inside the team counts as a task, and as one at work (Q2a, Q8). Its refusal, or None."""
        if self.tasks_used >= self.config.task_budget:
            return "No helper was started: " + BUDGET_USED.format(n=self.config.task_budget)
        if not self.room_for_helper():
            return (f"No helper was started: {self.config.at_once} of the team are already working, the most at "
                    "once. Wait for their results or do it yourself.")
        self.tasks_used += 1
        return None

    def _all_waiting(self) -> list[Member]:
        live = self.live_members()
        return live if live and all(m.waiting for m in live) else []

    def _everyone_waiting(self) -> None:
        """When every working member waits for a message, the lead is woken to say something (Q4) — once
        they have stayed so a moment, since a reply that assigns several starts them one after another."""
        if not self._all_waiting():
            self._all_waiting_said = False
            return
        if not self._all_waiting_said:
            asyncio.get_running_loop().call_later(EVERYONE_WAITING_SETTLE, self._tell_everyone_waiting)

    def _tell_everyone_waiting(self) -> None:
        live = self._all_waiting()
        if not live or self._all_waiting_said or self.ended:
            return
        self._all_waiting_said = True
        self.inbox.put({"kind": "guard_notice", "text": (
            f"Everyone is waiting: {', '.join(m.name for m in live)} "
            f"{'is' if len(live) == 1 else 'are all'} waiting for a message. Answer their questions, tell "
            "them what to do, or give your answer.")})

    # ── money, before every model call (Q8) ──
    def allowance(self, model: str, input_chars: int, max_output: int | None) -> float:
        from core.usage_tracker import calculate_cost
        try:
            return calculate_cost(model, max(0, int(input_chars)) // CHARS_PER_TOKEN,
                                  int(max_output or OUTPUT_ALLOWANCE))
        except Exception:  # noqa: BLE001 — an unpriced model costs nothing here
            return 0.0

    def may_call(self, model: str, input_chars: int, max_output: int | None, *, lead: bool) -> bool:
        """Whether the next call fits the team's limit: spent so far plus its allowance (and, for anyone but the
        lead, one more of the lead's, so the lead can always finish)."""
        limit = self.config.spend_limit_usd
        need = self.allowance(model, input_chars, max_output)
        if lead:
            self.lead_reserve = need
        if not limit:
            return True
        return self.spent_usd + need + (0.0 if lead else self.lead_reserve) <= limit

    def spend_stop_answer(self, *, lead: bool) -> str:
        limit = f"${self.config.spend_limit_usd:g}"
        if not lead:
            return (f"I stopped before finishing: another step would pass the team's spend limit ({limit}). "
                    "What I found so far is in my calls above.")
        found = [m for m in self.members.values() if m.status == "completed" and m.answer]
        lines = [f"I stopped here: another step would pass this team's spend limit ({limit})."]
        if found:
            lines.append("What the members found:")
            lines += [f"**{m.name}**: {m.answer}" for m in found]
        return "\n\n".join(lines)

    def _task_changed(self, card) -> None:
        entry = self.tasks.get(card.id)
        if entry is None:
            return
        status = card.status if card.status != "done" else ("failed" if card.outcome == "failed" else "done")
        entry.update(status=status, result=(card.result or "")[:ANSWER_CHARS])
        self.progress_mark += 1
        self._emit({"type": "team_tasks", "tasks": self.task_list()})

    # ── who is who ──
    def member_names(self) -> list[str]:
        return [a.get("name") or aid for aid, a in self.roster.items()]

    def _agent_named(self, name: str) -> tuple[str, dict] | None:
        wanted = str(name or "").strip()
        for aid, a in self.roster.items():
            if wanted in (aid, a.get("name")):
                return aid, a
        lowered = wanted.lower()
        for aid, a in self.roster.items():
            if lowered == str(a.get("name") or "").lower():
                return aid, a
        return None

    def _member_by_name(self, name: str) -> Member | None:
        """The member agent with this name (or id): the task it works on now, else one waiting on a person, else the
        next it has queued, else its latest; or a run by its own id."""
        if name in self.members:
            return self.members[name]
        found = self._agent_named(name)
        if found is None:
            return None
        runs = [m for m in self.members.values() if m.agent_id == found[0]]
        for status in ("running", "waiting_for_person", "queued"):
            for m in runs:
                if m.status == status:
                    return m
        return runs[-1] if runs else None

    def _busy(self, agent_id: str) -> Member | None:
        """The task a member agent works on, or waits on a person for: its next one queues behind it (F591)."""
        return next((m for m in self.members.values()
                     if m.agent_id == agent_id and m.status in ("running", "waiting_for_person")), None)

    def _name_one(self) -> Refused:
        return Refused(f"Name the member: one of {', '.join(self.member_names())}.")

    # ── the run board ──
    async def _board(self) -> str:
        if not self.board_id:
            from core.store import boards as store
            with contextlib.suppress(Exception):
                await store.sweep_run_boards()   # a crashed team's board, past a day idle
            board = await store.get_or_create_scoped_board("run", self.key, by=self._lead_actor())
            self.board_id = board.id
        return self.board_id

    def _lead_actor(self):
        from core.store.boards import Actor
        return Actor(agent=str(self.lead.get("id") or ""), agent_name=str(self.lead.get("name") or ""),
                     run=self.run_id or "", session=self.session_id or "", source="team")

    @staticmethod
    def _actor(member: Member):
        from core.store.boards import Actor
        return Actor(agent=member.agent_id, agent_name=member.name, source="team")

    def holder(self, member: Member):
        from core.store.boards import Holder
        return Holder(agent=member.agent_id, key=self.key, label=member.name)

    # ── the lead's tools ──
    async def assign(self, args: dict, *, tainted: bool) -> str:
        if not str(args.get("member") or "").strip():
            raise self._name_one()
        found = self._agent_named(str(args.get("member") or ""))
        task = str(args.get("task") or "").strip()
        if found is None:
            raise Refused(f"There is no member called '{args.get('member')}'. Members: "
                          f"{', '.join(self.member_names())}.")
        if self.tasks_used >= self.config.task_budget:
            raise Refused(BUDGET_USED.format(n=self.config.task_budget))
        if self.stall >= STALL_STOP_AT:
            raise Refused("No task has moved for a while, so no more work can be assigned. Finish now with what "
                          "you have.")
        if not task:
            raise Refused("Say what the member should do: `task` is its brief (the objective, what to return, "
                          "and its bounds).")
        agent_id, agent = found
        name = agent.get("name") or agent_id
        from core.store import boards as store
        board_id = await self._board()
        card, _ = await store.add_card(board_id, title=task[:200], body=task, kind="task",
                                       by=self._lead_actor(), tainted=tainted)
        member = Member(id=f"m_{uuid.uuid4().hex[:8]}", agent_id=agent_id, name=name, task=task, card_id=card.id,
                        tainted=tainted)
        member.inbox = new_inbox()
        claimed, _ = await store.claim(card.id, self.holder(member), by=self._lead_actor())
        member.card_version = claimed.version
        self.members[member.id] = member
        self.tasks[card.id] = {"card_id": card.id, "title": card.title, "member": name, "member_id": member.id,
                               "status": "claimed", "result": ""}
        self.tasks_used += 1
        self.progress_mark += 1
        self._emit({"type": "team_tasks", "tasks": self.task_list()})
        busy = self._busy(agent_id)
        started = self._admit()
        if busy is not None and member not in started:
            # One task at a time for one member, in the order given (F591; Naveen: queue it behind).
            return (f"Queued for {name} (task {card.id}): it starts this after its current task "
                    f"('{busy.task[:80]}'). Its result arrives in your team inbox; carry on, or call wait.")
        when = ("It has started." if member in started else
                f"It starts when a slot frees ({self.config.at_once} members work at once).")
        return (f"Assigned to {name} (task {card.id}). {when} Its result arrives in your team inbox; carry on, "
                "or call wait to idle until something arrives.")

    def message_from_lead(self, args: dict, *, tainted: bool) -> str:
        to = str(args.get("to") or "")
        if not to.strip():
            raise self._name_one()
        text = str(args.get("text") or "").strip()
        if not text:
            raise Refused("Write the message in `text`.")
        member = self._member_by_name(to)
        if member is None:
            raise Refused(f"There is no member called '{to}'. Members: {', '.join(self.member_names())}.")
        if not member.live:
            raise Refused(f"{member.name} is not working now ({_STATUS_WORDS.get(member.status, member.status)}): "
                          "nothing will read the message. Assign it a new task instead.")
        member.inbox.put({"kind": "lead_message", "text": text, "tainted": tainted})
        return f"Sent to {member.name}."

    def message_from_member(self, member: Member, args: dict, *, tainted: bool) -> str:
        to = str(args.get("to") or "").strip()
        text = str(args.get("text") or "").strip()
        if not text:
            raise Refused("Write the message in `text`.")
        if member.messages_sent >= MESSAGE_BUDGET:
            raise Refused(f"You have sent your {MESSAGE_BUDGET} messages. Finish your task, and put what you have "
                          "and what you still need in your final answer.")
        if to.lower() in ("lead", "the lead", str(self.lead.get("name") or "").lower(), str(self.lead.get("id"))):
            self.inbox.put({"kind": "member_message", "from": member.name, "member_id": member.id, "text": text,
                            "tainted": tainted})
            member.messages_sent += 1
            return "Sent to the lead. Its answer, if any, arrives as a message for you; call wait to idle for it."
        other = self._member_by_name(to)
        if other is None or other.id == member.id:
            raise Refused(f"Send it to 'lead' or to another member: {', '.join(self.member_names())}.")
        if not other.live:
            raise Refused(f"{other.name} is not working now: nothing will read it. Take it to the lead.")
        pair = frozenset((member.agent_id, other.agent_id))
        mark, count = self._pairs.get(pair, (self.progress_mark, 0))
        if mark != self.progress_mark:
            count = 0
        if count >= PAIR_CAP:
            raise Refused(f"You and {other.name} have exchanged {PAIR_CAP} messages while the task list has not "
                          "moved: take it to the lead.")
        self._pairs[pair] = (self.progress_mark, count + 1)
        other.inbox.put({"kind": "member_message", "from": member.name, "member_id": member.id, "text": text,
                         "tainted": tainted})
        member.messages_sent += 1
        return f"Sent to {other.name}."

    def message_from_person(self, member_id: str, text: str) -> bool:
        member = self.members.get(member_id)
        if member is None or not member.live or not text.strip():
            return False
        return member.inbox.put({"kind": "person_message", "text": text.strip()})

    async def wait_for_lead(self, seconds: Any) -> AsyncIterator[dict | str]:
        """Idle until something reaches the lead's inbox, or the seconds pass; members' events meanwhile. Ends
        with what the lead reads (a str)."""
        try:
            limit = float(seconds) if seconds is not None else WAIT_DEFAULT_SECONDS
        except (TypeError, ValueError):
            limit = WAIT_DEFAULT_SECONDS
        limit = max(1.0, min(limit, WAIT_MAX_SECONDS))
        started = time.monotonic()
        while True:
            for event in self.drain():
                yield event
            if self.inbox.has_pending():
                yield "Something arrived: it is in your team inbox below."
                return
            if not self.live_members():
                waiting = [m.name for m in self.waiting()]
                yield (f"No member is working, so nothing more will arrive. "
                       + (f"{', '.join(waiting)} {'waits' if len(waiting) == 1 else 'wait'} for a person's "
                          "approval; this turn pauses for it after your next step. " if waiting else "")
                       + "Assign more work, or give your final answer.")
                return
            elapsed = time.monotonic() - started
            if elapsed >= limit:
                # How long each has worked, which a patient lead's waits differ by: the repeat guard (F476) is
                # for a call that learns nothing new, and the stall counter is the one for a team (Q4).
                working = ", ".join(f"{m.name} ({int(m.seconds)}s in)" if m.started_at else f"{m.name} (queued)"
                                    for m in self.live_members())
                yield f"Nothing arrived after {int(elapsed)}s. Still working: {working}."
                return
            self._admit()
            with contextlib.suppress(asyncio.TimeoutError):
                waiters = [asyncio.ensure_future(self.inbox.wake.wait()),
                           asyncio.ensure_future(self.events_wake.wait())]
                try:
                    await asyncio.wait(waiters, timeout=min(1.0, limit - elapsed),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waiters:
                        w.cancel()

    async def wait_for_member(self, member: Member, seconds: Any) -> str:
        try:
            limit = float(seconds) if seconds is not None else WAIT_DEFAULT_SECONDS
        except (TypeError, ValueError):
            limit = WAIT_DEFAULT_SECONDS
        limit = max(1.0, min(limit, WAIT_MAX_SECONDS))
        if member.inbox.has_pending():
            return "Something arrived: it is below."
        member.waiting = True
        self._everyone_waiting()
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(member.inbox.wake.wait(), timeout=limit)
        finally:
            member.waiting = False
            self._everyone_waiting()
        if member.inbox.has_pending():
            return "Something arrived: it is below."
        return f"Nothing arrived after {int(limit)}s. Finish with what you have, or message the lead."

    def forward(self, args: dict) -> str:
        if not str(args.get("member") or "").strip():
            raise self._name_one()
        member = self._member_by_name(str(args.get("member") or ""))
        if member is None:
            raise Refused(f"There is no member called '{args.get('member')}'.")
        # Its latest finished answer: the member may be on its next task by now (F591).
        done = [m for m in self.members.values() if m.agent_id == member.agent_id and m.status == "completed"
                and m.answer]
        member = done[-1] if done else member
        if member.status != "completed" or not member.answer:
            raise Refused(f"{member.name} has no finished answer to forward "
                          f"({_STATUS_WORDS.get(member.status, member.status)}).")
        note = str(args.get("note") or "").strip()
        return f"{note}\n\n{member.answer}" if note else member.answer

    # ── a member's own tool ──
    async def board_update(self, member: Member, args: dict, *, tainted: bool) -> str:
        from core.store import boards as store
        card_id = str(args.get("card_id") or member.card_id)
        card = await store.get_card(card_id) if card_id else None
        if card is None or card_id != member.card_id or not card.held_by(self.holder(member)):
            # The store lets any holder update an unclaimed card: on a team's list, only your own (Q1).
            raise Refused(f"You can update only your own task ({member.card_id}).")
        status = args.get("status")
        result = args.get("result")
        try:
            fresh = await store.update_card(card_id, version=card.version, by=self._actor(member),
                                            holder=self.holder(member), tainted=tainted,
                                            status=status if status in ("done", "failed") else None,
                                            result=str(result) if result is not None else None)
        except store.BoardError as exc:
            raise Refused(str(exc)) from exc
        member.card_version = fresh.version
        self._task_changed(fresh)
        return f"Your task is now {self.tasks.get(card_id, {}).get('status', fresh.status)}."

    # ── running members ──
    def _admit(self) -> list[Member]:
        """Start queued members while fewer than `at_once` work (helpers working here count, Q2a)."""
        started: list[Member] = []
        if self.ended:
            return started
        for member in list(self.members.values()):
            if member.status != "queued":
                continue
            if self._busy(member.agent_id) is not None:
                continue   # its member is on an earlier task: this one waits behind it (F591)
            if len(self.running()) + self.helpers_working() >= self.config.at_once:
                break
            member.status = "running"
            member.started_at = time.time()
            member.task_handle = asyncio.create_task(self._drive(member), name=f"member-{member.id}")
            started.append(member)
        return started

    def start_queued(self) -> None:
        """Start whoever may start now, from the lead's own task (its next step)."""
        self._admit()

    def helpers_working(self) -> int:
        from core import helpers as _helpers
        return len(_helpers.live(_helpers.cap_key(self.session_id, workflow_step=False)))

    def room_for_helper(self) -> bool:
        return len(self.running()) + self.helpers_working() < self.config.at_once

    async def _should_stop(self, member: Member) -> bool:
        if member.stop_requested or self.ended:
            return True
        return bool(self.lead_should_stop is not None and await self.lead_should_stop())

    async def _drive(self, member: Member) -> None:
        """One member's turn, from its own task: its events to the lead's stream, its result to the lead."""
        from core.chat_approval import resume_state_for
        from core.react_engine import _own_model, run_agent_step
        from core.session import CALL_RESULT_CHARS, _clip, call_record

        agent = self.roster.get(member.agent_id) or {}
        member.model = agent.get("model") if _own_model(agent) else self.lead_model
        self._emit({"type": "member_started", "member_id": member.id, "member_name": member.name,
                    "agent_id": member.agent_id, "task": member.task, "card_id": member.card_id,
                    "model": member.model})
        resume, member.resume_state = member.resume_state, None
        token = current.set(self)
        if resume is not None:
            # Picked up after its question was answered: its claim may have lapsed while the turn was parked.
            with contextlib.suppress(Exception):
                from core.store import boards as store
                card, _ = await store.claim(member.card_id, self.holder(member), by=self._lead_actor())
                member.card_version = card.version
        try:
            while True:
                asked = None
                async for event in run_agent_step(
                    message=member.task,
                    agent_id=member.agent_id,
                    session_id=self.session_id,
                    server_module=self.server_module,
                    max_turns=agent.get("max_turns") or MEMBER_MAX_TURNS,
                    source=self.source,
                    run_id=self.run_id,
                    # A hand-off: none of the conversation, nothing saved of its own (F286).
                    history_override=[],
                    persist_turn=False,
                    # Written by a lead that may have read content nobody typed (F279).
                    untrusted_message=member.tainted,
                    progress_events=True,
                    resume_state=resume,
                    knowledge_turn=self.knowledge_turn,
                    # Its lead's model when it has none (F538).
                    model_override=None if _own_model(agent) else self.lead_model,
                    should_stop=lambda m=member: self._should_stop(m),
                    stream_answer=True,
                    team_seat=Seat(self, member),
                ):
                    kind = event.get("type")
                    if kind == "_step_progress":
                        member.progress = {k: v for k, v in event.items() if k != "type"}
                        continue
                    if kind == "human_input_required" and event.get("kind") == "tool_approval":
                        from core.chat_approval import question_id
                        # Its own id goes with the question its lane draws (TEAM1c).
                        event = asked = {**event, "question_id": question_id(member.id, event.get("approval_id"))}
                    elif kind == "final":
                        member.answer = event.get("response") or ""
                        member.tainted = member.tainted or bool(event.get("tainted"))
                        continue
                    elif kind == "tool_execution":
                        member.calls.append(call_record(event.get("tool_name"), event.get("args") or {}, ""))
                    elif kind == "tool_result" and member.calls and not member.calls[-1]["result"]:
                        member.calls[-1]["result"] = _clip(event.get("preview") or "", CALL_RESULT_CHARS)
                        if event.get("refused"):
                            member.calls[-1].update(refused=True, reason=event.get("reason") or "")
                    if kind in SHOWN:
                        self._emit(member.tag(event))
                if asked is None:
                    member.status = "completed"
                    break
                approval = {"approval_id": asked.get("approval_id"), "tool_name": asked.get("tool_name"),
                            "tool_args": asked.get("tool_args") or {}, "category": asked.get("category")}
                if self.can_park:
                    # It waits alone, its question one of the team's list; the others carry on, and the turn
                    # parks on every waiting question once nobody else is working (Q6, TEAM1c).
                    member.status, member.approval, member.asked = "waiting_for_person", approval, asked
                    member.asked_at = time.time()
                    self.inbox.put({"kind": "waiting_for_person", "member": member.name, "member_id": member.id,
                                    "tool": approval["tool_name"]})
                    return
                resume = resume_state_for({**approval, "progress": member.progress}, approve=False,
                                          reason="nobody can give an approval here")
        except asyncio.CancelledError:
            member.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — told to the lead, which carries on
            member.status, member.error = "failed", str(exc)
            _log.warning(f"team member '{member.name}' failed: {exc}")
        finally:
            current.reset(token)
            if member.status != "waiting_for_person":
                member.ended_at = time.time()
                if member.status == "running":
                    member.status = "cancelled"
                # Its result reaches the lead in the same step that ends it: a `wait` never reads
                # "nobody is working" with a result still on its way.
                if not self.ended:
                    self.inbox.put({"kind": "member_result", "member": member.name, "member_id": member.id,
                                    "status": member.status, "text": member.result_for_lead(),
                                    "tainted": member.tainted})
                if not self.ended:
                    closing = asyncio.ensure_future(self._close_card(member))
                    self._closing.add(closing)
                    closing.add_done_callback(self._closing.discard)
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(closing)
                self._emit({"type": "member_finished", "member_id": member.id, "member_name": member.name,
                            "status": member.status, "seconds": member.seconds, "record": member.record()})
                if not self.ended:
                    self._admit()

    async def _close_card(self, member: Member) -> None:
        """A member's end writes its card's outcome, unless it wrote one itself."""
        from core.store import boards as store
        if not member.card_id:
            return
        card = await store.get_card(member.card_id)
        if card is None or not card.held_by(self.holder(member)):
            return
        done = member.status == "completed"
        result = (member.answer or "(no answer)") if done else (
            member.error or f"It {_STATUS_WORDS.get(member.status, member.status)}.")
        try:
            fresh = await store.update_card(card.id, version=card.version, by=self._actor(member),
                                            holder=self.holder(member), tainted=member.tainted,
                                            status="done" if done else "failed", result=result[:ANSWER_CHARS])
        except Exception as exc:  # noqa: BLE001 — a card never fails a member
            _log.warning(f"team card {card.id} not closed: {exc}")
            return
        self._task_changed(fresh)

    async def settle(self) -> None:
        """Wait for finished members' cards to be written (their `member_finished` follows)."""
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)

    # ── a waiting member picked up again ──
    def resume_member(self, member_id: str | None, agent_id: str, task: str, inner: dict | None, *,
                      approved: bool | None = None, live: bool = False) -> Member | None:
        """A waiting member carries on with its own resume state: after its parked turn was answered (the lead's
        replay, F286's shape) or while the turn runs (`answer`). None when it waits no longer."""
        member = self.members.get(member_id or "")
        if member is None and not member_id:
            # A turn parked before TEAM1c named no member: the one this agent and task asked for.
            member = next((m for m in self.members.values() if m.status == "waiting_for_person"
                           and m.agent_id == agent_id and m.task == task), None)
        if member is None or member.status != "waiting_for_person":
            return None
        asked_id, tool = member.question_id, (member.approval or {}).get("tool_name")
        member.status, member.approval, member.asked, member.asked_at = "queued", None, None, 0.0
        member.resume_state = inner
        member.started_at = 0.0
        if approved is None:
            approved = bool(((inner or {}).get("pending_call") or {}).get("approved"))
        self._emit({"type": "member_answered", "member_id": member.id, "member_name": member.name,
                    "question_id": asked_id, "approved": approved})
        if live:
            # Answered while the lead works: it reads so between its steps (a parked turn's replay says it
            # itself). The member is started from the lead's own task — its `wait`, or its next step — never
            # from the answer's caller (a route, a job's watcher), whose context is not the turn's.
            self.inbox.put({"kind": "person_answered", "member": member.name, "member_id": member.id,
                            "tool": tool, "approved": approved})
        else:
            self._admit()
        return member

    def answer(self, question_id: str, *, approve: bool, reason: str = "") -> bool:
        """A person's answer to one waiting member's question while this turn runs (TEAM1c, Q6): that member
        carries on — its call run, or refused, as itself — and the others are untouched. False when no member
        waits on that question here, or the turn is pausing or over: the answer then waits for whoever takes
        the paused turn up (`chat_approval.post_answer`)."""
        if self.parking or self.ended:
            return False
        member = next((m for m in self.waiting() if m.question_id == question_id), None)
        if member is None:
            return False
        from core.chat_approval import member_resume_state
        inner = member_resume_state({"approval": member.approval, "progress": member.progress},
                                    approve=approve, reason=reason)
        return self.resume_member(member.id, member.agent_id, member.task, inner, approved=approve,
                                  live=True) is not None

    def still_asking(self) -> None:
        """Each member still waiting goes out with its question again, for a page following a resumed turn."""
        for member in self.waiting():
            if member.asked:
                self._emit(member.tag(member.asked))

    def stop_member(self, member_id: str) -> bool:
        member = self.members.get(member_id)
        if member is None or not member.live:
            return False
        member.stop_requested = True
        if member.status == "queued":
            member.status = "cancelled"
            member.ended_at = time.time()
            self.inbox.put({"kind": "member_result", "member": member.name, "member_id": member.id,
                            "status": "cancelled", "text": "It was stopped by the person before it started.",
                            "tainted": False})
        return True

    # ── the end ──
    async def finish(self, *, parked: bool = False) -> None:
        """The lead's turn is over: stop what still works, close its open tasks, release and delete the board.
        A parked turn keeps its board and its waiting member."""
        self.ended = not parked
        handles = [m.task_handle for m in self.members.values() if m.task_handle is not None
                   and not m.task_handle.done()]
        for handle in handles:
            handle.cancel()
        for handle in handles:
            with contextlib.suppress(BaseException):
                await handle
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)
        self.ended = True
        for member in self.members.values():
            if member.status == "queued":
                member.status = "cancelled"
            elif member.status == "waiting_for_person" and not parked:
                # Its question goes unanswered with the turn (TEAM1c): never run, and its record says so.
                member.status, member.ended_at = "cancelled", time.time()
                member.error = "Not run: the team's turn ended before a person answered."
                self._emit({"type": "member_finished", "member_id": member.id, "member_name": member.name,
                            "status": member.status, "seconds": member.seconds, "record": member.record()})
        if _LIVE.get((get_tenant() or "", self.session_id)) is self:
            _LIVE.pop((get_tenant() or "", self.session_id), None)
        if parked or not self.board_id:
            return
        from core.store import boards as store
        try:
            for entry in self.tasks.values():
                if entry["status"] in ("open", "claimed"):
                    entry.update(status="failed", result="The team ended before this was done.")
            await store.release_holder(self.key, why="the team's turn ended")
            await store.delete_board(self.board_id)
        except Exception as exc:  # noqa: BLE001 — a board never fails a turn
            _log.warning(f"team board {self.board_id} not cleaned up: {exc}")


@contextlib.asynccontextmanager
async def guard(team: "Team | None"):
    """Around the lead's loop: however it ends — an answer, a park, a Stop, an error, its stream closed — its
    members stop and its board is cleaned up (a parked turn's kept). Never yields an event: the lead's turn
    reads `drain()` after it."""
    if team is None:
        yield
        return
    team.register()
    token = current.set(team)
    try:
        yield
    finally:
        current.reset(token)
        if not team.ended:
            await team.finish(parked=team.parking)


# ── the tools' declarations ──────────────────────────────────────────────────

def lead_tools(team: Team) -> list:
    from core.tools import VirtualTool

    names = team.member_names()
    member = {"type": "string", "description": "The member's name", "enum": names}
    return [
        VirtualTool(name=ASSIGN, description=(
            "Start a member on a task. Returns at once; the member works while you go on, and its result arrives "
            "in your team inbox. Assign independent parts in one reply so they run at the same time. The task is "
            "a brief: the objective, what to return, and its bounds."),
            inputSchema={"type": "object", "properties": {"member": member, "task": {"type": "string"}},
                         "required": ["member", "task"]}),
        VirtualTool(name=MESSAGE, description=(
            "Send a working member a note: answer its question, or redirect it. It reads it between its steps."),
            inputSchema={"type": "object", "properties": {"to": member, "text": {"type": "string"}},
                         "required": ["to", "text"]}),
        VirtualTool(name=WAIT, description=(
            "Idle until a member's result, question or message arrives, or the seconds pass (at most "
            f"{WAIT_MAX_SECONDS}). The only way to wait: never poll."),
            inputSchema={"type": "object", "properties": {"seconds": {"type": "number"}}, "required": []}),
        VirtualTool(name=FORWARD, description=(
            "End your turn with a member's finished answer, word for word (optionally with a short note before "
            "it). Use it when that answer is already the answer: rewriting it loses facts."),
            inputSchema={"type": "object", "properties": {"member": member, "note": {"type": "string"}},
                         "required": ["member"]}),
    ]


def member_tools(team: Team, member: Member) -> list:
    from core.tools import VirtualTool

    others = [n for n in team.member_names() if n != member.name]
    return [
        VirtualTool(name=MESSAGE, description=(
            "Message the lead (a specific question, or something it must know) or another member (only when you "
            "need something it is doing). Keep it short; you have a few. A message never approves anything."),
            inputSchema={"type": "object", "properties": {
                "to": {"type": "string", "enum": ["lead", *others]}, "text": {"type": "string"}},
                "required": ["to", "text"]}),
        VirtualTool(name=WAIT, description=(
            "Idle until a message for you arrives, or the seconds pass (at most "
            f"{WAIT_MAX_SECONDS}). Use it after asking the lead something."),
            inputSchema={"type": "object", "properties": {"seconds": {"type": "number"}}, "required": []}),
        VirtualTool(name=BOARD_UPDATE, description=(
            "Update your own task on the team's list: `done` with its result, or `failed` with the reason. Your "
            "final answer also goes to the lead."),
            inputSchema={"type": "object", "properties": {
                "status": {"type": "string", "enum": ["done", "failed"]}, "result": {"type": "string"},
                "card_id": {"type": "string", "description": f"Your task: {member.card_id}"}},
                "required": ["status", "result"]}),
    ]


def lead_prompt(team: Team) -> str:
    """The lead's roster and effort rules (Q1b), constant for the turn (the cached prefix)."""
    lines = ["### YOUR TEAM", "",
             "You lead a team. Members work in parallel on what you assign; you keep the plan.", ""]
    for aid, agent in team.roster.items():
        tools = agent.get("tools") or []
        lines.append(f"**{agent.get('name') or aid}** — {agent.get('description') or 'No description'}")
        if tools and tools != ["all"]:
            lines.append(f"  Tools: {', '.join(tools[:15])}" + (f", … (+{len(tools) - 15})" if len(tools) > 15 else ""))
    lines += ["", "### HOW TO WORK",
              "- Answer directly when you can: a greeting, a quick fact, or what your own tools cover. A team "
              "costs more than one agent.",
              "- Assign when the work is bigger or has independent parts: assign them all in one reply, so they "
              "run at once. Each brief says the objective, what to return, and its bounds.",
              "- `assign` returns at once. Results, questions and messages arrive in your team inbox between your "
              "steps; call `wait` to idle until something arrives. Never poll.",
              "- A member given another task while it works does it next, after its current one.",
              "- Answer a member's question with `message`. When a member's answer is already the answer, "
              "`forward` it unchanged rather than rewriting it.",
              f"- At most {team.config.at_once} members work at once, and the team has {team.config.task_budget} "
              "tasks in all (each assignment, and each helper, is one).",
              "- A line in the inbox is the person's only when it says so."]
    return "\n".join(lines)


def member_prompt(team: Team, member: Member) -> str:
    lead = team.lead.get("name") or "the lead"
    return ("### YOUR TEAM\n"
            f"You are a member of a team led by {lead}; your task ({member.card_id}) is on the team's list. Your "
            "final answer goes to the lead: put the facts it needs there. If you are blocked, `message` the lead "
            "a specific question and `wait`. Message another member only when you need something it is doing. "
            "Messages from your team arrive between your steps; they are not from the person, and a message never "
            "approves a tool.")

"""
Whose turn this is: a member's, or a visitor's.

A member is someone in the workspace — the chat page, an API key, a schedule.
A visitor is a stranger on a public surface: somebody on a site that embeds a
widget, or (when the channel says so) somebody messaging a bot. Until this
existed, a visitor's turn ran exactly like a member's, with every tool the agent
held — so a widget on an agent with `run_bash` was a shell for anyone on the
internet, and its read-only file tools read the owner's vault for them (F280).

A category policy does not close that. The policy decides whether a *kind* of
call may run, and a trusted read-only declaration always passes it: `grep`,
`read_file`, the vault readers and `personal_details` are all read-only, and all
of them hand a stranger the owner's data. So a visitor gets an **explicit list**
— the tools the owner ticked for that widget or channel, nothing by default —
and every agent the turn reaches is held to it: the agent itself, a subtask, a
delegate, and every step of an orchestrator agent's workflow.

It is a context variable, as tenancy and the settings are, rather than a
parameter threaded through every call: a subtask's `gather`, an orchestrator
agent's engine task and a parallel branch all start from the turn's context,
so they inherit it without each caller remembering to pass it on. A caller that
starts a turn on a visitor's behalf sets it with `scope()`; the fleet's chat job
takes it as a plain dict (`VisitorScope.to_dict`) and sets it the same way.

What a visitor never gets, ticked or not:

* **Memory.** The agent's memory is the owner's — read into the prompt, written
  by `remember`, traced at the end of the turn. A stranger reads none of it and
  writes none of it (F276).
* **An approval card.** Nobody on a public surface can approve a call, so a
  `confirm` is a `deny` for a visitor.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

#: The engine's own tools that reach no data: the task list and loading one of
#: the agent's selected skills (which is its author's prompt text, the same
#: author as the system prompt a visitor's turn already runs on).
ENGINE_TOOLS: frozenset[str] = frozenset(("update_tasks", "load_skill"))

#: Never on a visitor's list, whatever the owner ticks: the agent's memory.
NEVER_FOR_VISITORS: frozenset[str] = frozenset(
    ("remember", "read_memory", "search_memory", "forget_memory")
)

#: How many names one list may hold — a bound on a stored document, not a policy.
MAX_TOOLS = 200

SURFACES = ("widget", "channel")


@dataclass(frozen=True)
class VisitorScope:
    """A visitor's turn: which public surface, and the tools its owner allows."""

    surface: str
    tools: frozenset[str] = frozenset()

    def allows(self, tool_name: str) -> bool:
        """Whether a visitor's turn may hold this tool at all."""
        return tool_name in ENGINE_TOOLS or tool_name in self.tools

    def to_dict(self) -> dict:
        return {"surface": self.surface, "tools": sorted(self.tools)}

    @classmethod
    def from_dict(cls, value) -> "VisitorScope | None":
        """The fleet job's kwarg back into a scope. `None` is a member's turn."""
        if not isinstance(value, dict):
            return None
        surface = value.get("surface") if value.get("surface") in SURFACES else "widget"
        return cls(surface=surface, tools=frozenset(clean_tools(value.get("tools"))))


def clean_tools(value) -> list[str]:
    """What a widget or channel may store as its visitor list: names, once each,
    in order, never a memory tool. Anything else is dropped, not obeyed."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for name in value:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or len(name) > 200 or name in NEVER_FOR_VISITORS or name in out:
            continue
        out.append(name)
        if len(out) >= MAX_TOOLS:
            break
    return out


_current: ContextVar[VisitorScope | None] = ContextVar("synapse_visitor", default=None)


def current() -> VisitorScope | None:
    """The visitor this turn runs for, or `None` for a member's turn."""
    return _current.get()


@contextmanager
def scope(visitor: VisitorScope | None) -> Iterator[VisitorScope | None]:
    """Run what starts inside this block as `visitor`'s turn.

    Tasks created inside the block copy the context, so a turn started here
    keeps the scope after the block has closed; the block's own context is
    restored on the way out.
    """
    token = _current.set(visitor)
    try:
        yield visitor
    finally:
        _current.reset(token)

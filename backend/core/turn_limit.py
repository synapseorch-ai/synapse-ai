"""What an agent does at its turn limit (F1219; Naveen 10-29).

An agent's last turn is an answer turn (F1015's, F1219 part A): only its finish tools are offered, so it answers from
what it has. When even that turn did not finish, the agent's `turn_limit_mode` says what happens next:

* **ask**: the turn pauses as a tool approval does — the same parked turn, card and notification — with "I've used N
  turns on this; keep going?": [Keep going] gives it N more turns from where it stands, and [Answer with what you
  have] gives it one more answer turn.
* **continue**: it gives itself N more turns, as long as the run's cost cap allows (`run_cap`), or, with no cap, while
  it has spent under `CEILING_USD` and gone on fewer than `MAX_EXTENSIONS` times.
* **stop**: it ends as out of turns, saying where the limit is raised (today's behaviour).

The agent's own `turn_limit_mode` beats the workspace's (`SETTING`, Settings › General); with neither set, a turn a
person is in asks and a run nobody watches stops (`DEFAULT`). Every extra turn is metered as any turn is, and the
loop's repeat guard (F243, F476: the same call with the same arguments and the same result three times in a row) ends
a turn that goes on without getting anywhere, extension or not.
"""
from __future__ import annotations

from typing import Any

MODES = ("ask", "continue", "stop")
#: The workspace's setting (Settings › General) and an agent's override, by the same name.
SETTING = "turn_limit_mode"
#: With nothing set (Naveen 10-29): a person in the turn is asked; a run nobody watches stops.
DEFAULT = {True: "ask", False: "stop"}
#: What `continue` may spend on its own in a turn or run with no cost cap, and how often it may go on.
CEILING_USD = 1.00
MAX_EXTENSIONS = 5
#: A run a person watches as it goes (Synapse AI's panel): its starter sets this in the run's state.
ATTENDED_KEY = "_attended"
#: The approval's kind, and its id's prefix: an answer to it is a decision about turns, never a tool call to run.
KIND = "turn_limit"


def chosen(value: Any) -> str | None:
    """`value` as a mode, or None for one not set (or not one of `MODES`)."""
    return value if isinstance(value, str) and value in MODES else None


def mode(agent: dict | None, settings: dict | None, *, person: bool) -> str:
    """The mode for this turn: the agent's own, else the workspace's, else `DEFAULT` for whether a person is in it.
    A run nobody watches whose workspace or agent chose `ask` parks as a tool approval does, and is notified as one."""
    picked = chosen((agent or {}).get(SETTING)) or chosen((settings or {}).get(SETTING))
    return picked or DEFAULT[bool(person)]


def asking_words(turns: int) -> str:
    return f"I've used {turns} turns on this; keep going?"


def refusal(*, extensions: int, spent_usd: float, cap_reason: str | None, capped: bool) -> str | None:
    """Why `continue` may not go on again, or None. `cap_reason`: the run's own cap's sentence (`run_cap`)."""
    if cap_reason:
        return cap_reason
    if extensions >= MAX_EXTENSIONS:
        return f"it has already gone on {MAX_EXTENSIONS} times past its turn limit"
    if not capped and spent_usd >= CEILING_USD:
        return f"it has spent ${spent_usd:.2f}, the most it goes on to spend by itself with no cost cap set"
    return None

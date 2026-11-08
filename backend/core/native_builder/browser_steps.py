"""Browser steps, for Synapse AI's planner (F1193; BRW1, D328): the workspace's recordings and how a Browser step
replays one. `routes/builder.py` puts `palette()` in the run's `browser_steps` state key, which the planner's step
reads beside `knowledge_steps`; empty when the workspace has no recording, so a plan never has a Browser step it
could not save. A recording is a person's, made in the app: a plan only names one listed here.

What a plan writes is the engine's own fields in the step's `config` (`compile.py` copies them), checked as every
builder door checks a Browser step (`builder_tools._refuse_browser_steps`, F1189).
"""
from __future__ import annotations

from core.log import get_logger

_log = get_logger(__name__)

#: The most recordings the planner is shown, and the most steps of each.
MAX_RECORDINGS = 20
MAX_STEPS = 25

PALETTE = """\
Browser steps — this workspace has recordings: tasks a person did once in its browser, replayed by code with no model \
call unless a page changed. Each by id, with its inputs and its steps (each step's id in brackets):
{recordings}
- **browser**: replays one of these recordings. Its `config` is `{{"recording_id": "rec_…", "recording_inputs": \
{{"<input>": "{{{{ state.key }}}}"}}}}`: every input the recording lists, filled with a value or a `{{{{ state.key }}}}` \
placeholder. What the person gives when the run starts (the order number in "when I give you an order number", or a \
schedule's message) is the run's own input, `{{{{state.user_input}}}}`: fill the input with it, never a human step \
that asks again for what the run is given. An earlier step's output is `{{{{state.<step id>}}}}`, and inside a Loop's \
body the item is `{{{{state.loop_item}}}}`; a key nothing writes before the step is refused. Optional in it: \
`may_send_steps` (ids of the recording's steps that may send on their own; a run is never watched, so any other send \
waits for the person: leave it out unless they asked for it), `on_miss` (`"pause"`, the default, hands the person the \
wheel when a page changed; `"agent"`, with the step's `agent` naming a Browser agent, has that agent find the way), \
`browser_sessions` (sites it starts signed in to, from the person's kept sessions). What it reads (the recording's outputs) is written as `{{name: value}}` under the step's output key: a later step reads one as `{{{{ state.<step id>.<output> }}}}`. Use it only for a recording listed here: recording is a person's, in the app.
{agents}
- For each of several items (orders, names, addresses), the Browser step goes in a `loop`'s `body`, its input the \
item, `{{{{state.loop_item}}}}`. A Loop goes through a list: a list the person pastes is the run's own input, which is \
text, so a step before the Loop makes it one — a `transform` where this workspace runs code (one item per line, \
bullets, numbers and blank lines taken off), else an `llm` step that answers the items as a JSON list, its id declared \
`{{"type": "list"}}` in the plan's `settings.state_schema` — and the Loop's `loop_over` names that step.
"""

#: The Browser agents, for a step's `on_miss: "agent"`; or what to do without one.
AGENTS = """\
- Browser agents here, by id, for a step's `agent` when a page changed: {listed}. A plan may also make one in its \
`agents`, type `browser`, and name it there."""
NO_AGENTS = """\
- No Browser agent here: where the person wants one to find the way when a page changed, the plan makes one in its \
`agents`, type `browser`, and names it as the step's `agent`; otherwise the step keeps `on_miss` `"pause"`."""
#: The agents could not be read: none is named, none is made in their place.
AGENTS_UNREAD = """\
- This workspace's agents could not be read just now, so none is named or made here: the step keeps `on_miss` \
`"pause"`."""


def _line(doc: dict) -> str:
    from core.browser.recordings import input_names
    steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
    said = "; ".join(f"[{s.get('id')}] {(s.get('title') or s.get('line') or '').strip()}" for s in steps[:MAX_STEPS])
    more = f"; … {len(steps) - MAX_STEPS} more" if len(steps) > MAX_STEPS else ""
    inputs = ", ".join(input_names(doc)) or "none"
    return (f"- `{doc['id']}` \"{doc.get('name') or ''}\" (sites: {', '.join(doc.get('sites') or []) or 'none'}; "
            f"inputs: {inputs}) — steps: {said}{more}")


async def _agents() -> str:
    """The workspace's Browser agents by id (F1233), or what a plan does without one."""
    from core.store.resources import load_agents
    try:
        found = [a for a in await load_agents() if a.get("type") == "browser"][:MAX_RECORDINGS]
    except Exception as exc:  # noqa: BLE001 — said, never read as "none here": a store that cannot list them
        _log.warning("browser_steps: agents not listed: %s", exc)
        return AGENTS_UNREAD
    if not found:
        return NO_AGENTS
    return AGENTS.format(listed=", ".join(f"`{a['id']}` \"{a.get('name') or ''}\"" for a in found))


async def palette() -> str:
    """The palette with the workspace's recordings, newest changed first, and its Browser agents; empty without a
    recording."""
    from core.browser import recordings
    try:
        docs = (await recordings.all_recordings())[:MAX_RECORDINGS]
    except Exception as exc:  # noqa: BLE001 — no recording can be offered; the log says why
        _log.warning("browser_steps: recordings not listed: %s", exc)
        return ""
    if not docs:
        return ""
    return PALETTE.format(recordings="\n".join(_line(d) for d in docs), agents=await _agents())

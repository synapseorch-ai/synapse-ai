"""Browser steps, for Synapse AI's planner (F1193; BRW1, D328): the workspace's recordings and how a Browser step
replays one. `routes/builder.py` puts `palette()` in the run's `browser_steps` state key, which the planner's step
reads beside `knowledge_steps`; empty when the workspace has no recording, so a plan never has a Browser step it
could not save. A recording is a person's, made in the app: a plan only names one listed here.

What a plan writes is the engine's own fields in the step's `config` (`compile.py` copies them), checked as every
builder door checks a Browser step (`builder_tools._refuse_browser_steps`, F1189).
"""
from __future__ import annotations

#: The most recordings the planner is shown, and the most steps of each.
MAX_RECORDINGS = 20
MAX_STEPS = 25

PALETTE = """\
Browser steps — this workspace has recordings: tasks a person did once in its browser, replayed by code with no model \
call unless a page changed. Each by id, with its inputs and its steps (each step's id in brackets):
{recordings}
- **browser**: replays one of these recordings. Its `config` is `{{"recording_id": "rec_…", "recording_inputs": \
{{"<input>": "{{state.key}}"}}}}`: every input the recording lists, filled with a value or a `{{state.key}}` \
placeholder. Optional in it: `may_send_steps` (ids of the recording's steps that may send on their own; a run is never \
watched, so any other send waits for the person: leave it out unless they asked for it), `on_miss` (`"pause"`, the \
default, hands the person the wheel when a page changed; `"agent"`, with the step's `agent` naming one of this \
workspace's Browser agents by id, has that agent find the way), `browser_sessions` (sites it starts signed in to, \
from the person's kept sessions). What it reads (the recording's outputs) is written as `{{name: value}}` under the step's output key: a later step reads one as `{{{{ state.<step id>.<output> }}}}`. Use it only for a recording listed here: recording is a person's, in the app.
"""


def _line(doc: dict) -> str:
    from core.browser.recordings import input_names
    steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
    said = "; ".join(f"[{s.get('id')}] {(s.get('title') or s.get('line') or '').strip()}" for s in steps[:MAX_STEPS])
    more = f"; … {len(steps) - MAX_STEPS} more" if len(steps) > MAX_STEPS else ""
    inputs = ", ".join(input_names(doc)) or "none"
    return (f"- `{doc['id']}` \"{doc.get('name') or ''}\" (sites: {', '.join(doc.get('sites') or []) or 'none'}; "
            f"inputs: {inputs}) — steps: {said}{more}")


async def palette() -> str:
    """The palette with the workspace's recordings, newest changed first; empty without one."""
    from core.browser import recordings
    try:
        docs = (await recordings.all_recordings())[:MAX_RECORDINGS]
    except Exception:
        return ""
    if not docs:
        return ""
    return PALETTE.format(recordings="\n".join(_line(d) for d in docs))

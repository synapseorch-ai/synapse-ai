"""What every door that saves an agent's or a workflow step's browser parts checks (BRW1, D328; F1140, F1145, F1146,
F1167, F1189).

The app's editors (`routes/agents`, `routes/orchestrations`) and the builder's tools ask the same questions here, so a
model can't save what a person couldn't:

* **Sessions** (F1140, F1167): one per site in each pick, and an id new to this save only if it is the person's own or
  shared with them. Ids already saved stay: what loads is decided by the job's person at the browser's start.
* **Recordings** (F1145, F1146): one named now exists. One deleted since it was picked drops out of an agent's pick.

And, for the builder, which a model drives, what the editor's pickers never let a person get wrong: a session named by
its site is the person's own (`resolve_sessions`), and a Browser step's inputs and sends are its recording's
(`recording_problems`).

Refusals are `PickRefused` (answered flat, `{detail, site, ids}`) and `RecordingGone`.
"""
from __future__ import annotations

from typing import Any

from core.browser.picks import PickRefused


class RecordingGone(ValueError):
    """A recording named now that does not exist (and was not already saved there)."""

    def __init__(self, ids: list[str]):
        super().__init__(f"No such recording: {', '.join(ids)}.")
        self.ids = ids


async def check_picks(lists: list[list[str]], stored: list[list[str]], *, for_user: str | None = None) -> None:
    """Each pick one per site, and an id new to it (not in its `stored` twin) the person's own or shared with them."""
    from core.browser.picks import one_per_site, usable_new
    for ids, before in zip(lists, stored):
        await one_per_site(ids)
        await usable_new(ids, before=before, for_user=for_user)


def agent_sessions(agent: dict | None) -> list[str]:
    """An agent's picked sessions as saved, an older agent's one `login_ref` included."""
    browser = (agent or {}).get("browser") or {}
    return [*(browser.get("sessions") or []), *([browser["login_ref"]] if browser.get("login_ref") else [])]


async def existing_recordings(picks: list[str], before: list[str] | None = None) -> list[str]:
    """`picks` less those deleted since they were picked; `RecordingGone` for one new to this save that isn't there."""
    from core.browser import recordings
    known = {d["id"] for d in await recordings.picked(picks)}
    unknown = [r for r in picks if r not in known and r not in set(before or [])]
    if unknown:
        raise RecordingGone(unknown)
    return [r for r in picks if r in known]


async def agent_browser(document: dict, before: dict | None, *, for_user: str | None = None) -> None:
    """An agent's `browser` as the agent editor saves it (F1140, F1145, F1167). `before` is the agent as saved, or
    None for a new one. Its recordings are written back without those deleted since they were picked."""
    browser = document.get("browser") or {}
    if browser.get("sessions"):
        await check_picks([browser["sessions"]], [agent_sessions(before)], for_user=for_user)
    if browser.get("recordings"):
        had = ((before or {}).get("browser") or {}).get("recordings") or []
        browser["recordings"] = await existing_recordings(browser["recordings"], had)


def step_picks(orch: dict, existing: dict | None) -> tuple[list[list[str]], list[list[str]]]:
    """Each step's picked sessions, and what that step had saved: the lists `check_picks` takes."""
    had = {s.get("id"): s.get("browser_sessions") or [] for s in (existing or {}).get("steps") or []}
    picked = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("browser_sessions")]
    return [s["browser_sessions"] for s in picked], [had.get(s.get("id")) or [] for s in picked]


def _recordings_named(orch: dict | None) -> set[str]:
    return {r for s in (orch or {}).get("steps") or [] if isinstance(s, dict)
            for r in [s.get("recording_id") if s.get("type") == "browser" else None,
                      *(s.get("browser_recordings") or [])] if r}


async def step_recordings(orch: dict, existing: dict | None) -> None:
    """A Browser step's recording, and recordings picked for a step's agent, exist (F1146); those the workflow
    already named stay."""
    from core.browser import recordings
    named = _recordings_named(orch)
    if named:
        gone = named - _recordings_named(existing) - {d["id"] for d in await recordings.picked(sorted(named))}
        if gone:
            raise RecordingGone(sorted(gone))


async def step_browser(orch: dict, existing: dict | None, *, for_user: str | None = None) -> None:
    """A workflow's steps as the editor's save checks them: their sessions, then their recordings."""
    await check_picks(*step_picks(orch, existing), for_user=for_user)
    await step_recordings(orch, existing)


def recording_problems(step: dict, doc: dict) -> list[str]:
    """A Browser step against its recording, in sentences: inputs it hasn't got, inputs it leaves unfilled (the run
    would stop there), and sends that aren't the recording's steps."""
    sid, name = step.get("id") or "?", doc.get("name") or doc.get("id")
    from core.browser import recordings
    names = recordings.input_names(doc)
    given = step.get("recording_inputs") or {}
    found = []
    extra = [n for n in given if n not in names]
    if extra:
        found.append(f"Browser step '{sid}': its recording \"{name}\" has no input "
                     f"{', '.join(repr(n) for n in extra)}; its inputs are {', '.join(repr(n) for n in names) or 'none'}.")
    missing = [n for n in names if not str(given.get(n) or "").strip()]
    if missing:
        found.append(f"Browser step '{sid}' must fill its recording's inputs in recording_inputs: "
                     f"{', '.join(repr(n) for n in missing)}, each a value or a {{state.key}} template.")
    own = {s.get("id") for s in doc.get("steps") or []}
    strays = [s for s in step.get("may_send_steps") or [] if s not in own]
    if strays:
        found.append(f"Browser step '{sid}': may_send_steps names {', '.join(repr(s) for s in strays)}, which are not "
                     "steps of its recording (get_recording lists them).")
    return found


#: The run's own input: the message, or a schedule's prompt, it starts with.
RUN_INPUT = "user_input"


def _before(sid: str, by_id: dict[str, dict]) -> set[str]:
    """The steps that run before `sid`: those it is reached from, and a Loop body's earlier steps."""
    from core.builder_tools import step_successors
    into: dict[str, set[str]] = {}
    for s in by_id.values():
        for nxt in step_successors(s, by_id):
            into.setdefault(nxt, set()).add(s["id"])
        body = [b for b in s.get("loop_step_ids") or [] if b in by_id]
        for i, b in enumerate(body):
            into.setdefault(b, set()).update(body[:i])
    seen, todo = set(), [sid]
    while todo:
        for prev in into.get(todo.pop(), ()):
            if prev not in seen:
                seen.add(prev)
                todo.append(prev)
    return seen - {sid}


def run_keys(sid: str, steps: list[dict], schema: dict | None, *, whole: bool) -> tuple[set[str], set[str]]:
    """What step `sid` may read from the run's state, and what is written only after it. `whole`: `steps` is the whole
    workflow, wired, so a step's output counts once written before `sid`, and a Loop's item inside its body; else (a
    door that adds or patches a step mid-build) every step's output and every Loop's item count."""
    from core.orchestration.branch_keys import ENGINE_KEYS
    by_id = {s["id"]: s for s in steps if isinstance(s, dict) and s.get("id")}
    before = _before(sid, by_id) if whole else set(by_id)
    has, later = {*ENGINE_KEYS, *(schema or {})}, set()
    for s in by_id.values():
        if s["id"] != sid and (key := s.get("output_key")):
            (has if s["id"] in before else later).add(key)
        if s.get("type") == "loop" and (not whole or sid in (s.get("loop_step_ids") or [])):
            has |= {s.get("loop_item_key") or "loop_item", s.get("loop_index_key") or "loop_index"}
    return has, later - has


def _state_keys(text: str) -> list[str]:
    """The state keys a template reads, in either form (`{state.x}`, `{{ state.x.y }}`)."""
    from core.orchestration.context import _TASK_REF
    keys = []
    for m in _TASK_REF.finditer(text):
        path = m.group(1).strip() if m.group(1) is not None else f"state.{m.group(2)}"
        head, _, rest = path.partition(".")
        if head == "state" and rest:
            keys.append(rest.split(".")[0])
    return list(dict.fromkeys(keys))


def input_problems(steps: list[dict], schema: dict | None = None, *, whole: bool = True,
                   of: set[str] | None = None) -> list[str]:
    """Each Browser step's `{state.…}` inputs (those in `of`, else all) against what the run has (`run_keys`), in
    sentences naming what is missing and what there is: a key nothing has written reads empty, and the run stops at
    the step (F1232)."""
    from core.orchestration.branch_keys import ENGINE_KEYS
    found = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "browser" or of is not None and step.get("id") not in of:
            continue
        sid = step.get("id") or "?"
        has, later = run_keys(sid, steps, schema, whole=whole)
        read = _state_keys(" ".join(str(v or "") for v in (step.get("recording_inputs") or {}).values()))
        if not (missing := [k for k in read if k not in has]):
            continue
        why = "is written only after it" if all(k in later for k in missing) else "nothing in the run writes"
        others = ", ".join(f"{{state.{k}}}" for k in sorted(has - ENGINE_KEYS))
        found.append(f"Browser step '{sid}' reads {', '.join(f'{{state.{k}}}' for k in missing)}, which {why}. It "
                     f"can read the run's own input, {{state.{RUN_INPUT}}} (what the person gives when it starts)"
                     + (f", or {others}" if others else "") + ".")
    return found


async def resolve_sessions(values: list[Any], *, caller: Any = None) -> list[str]:
    """The sessions a model named, as ids. An id (no dot in it) stays as given, for the pick rule to check; a site, or
    an address on one, is the person's own session for it, else the one shared with them. None, or more than one
    shared and none of theirs, is refused naming what there is."""
    from core import site_sessions
    from core.sites import registrable_domain
    from urllib.parse import urlparse

    rows: list[dict] | None = None
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        if "." not in text:
            out.append(text)
            continue
        host = urlparse(text if "://" in text else f"//{text}").hostname or ""
        site = registrable_domain(host.removeprefix("*.")) or host
        if rows is None:
            rows = [r for r in await site_sessions.listed(caller) if r.get("usable")]
        here = [r for r in rows if r.get("site") == site]
        pick = [r for r in here if r.get("mine")] or here
        if not pick:
            raise PickRefused(f"You have no session for {site} to use: keep one in Sessions first (a person signs "
                              "in there), or have someone share theirs.", site, [])
        if len(pick) > 1:
            whose = "of yours" if pick[0].get("mine") else "shared with you"
            raise PickRefused(f"There is more than one session {whose} for {site}: name one by its id "
                              f"({', '.join(r['id'] for r in pick)}; list_browser_sessions shows whose each is).",
                              site, [r["id"] for r in pick])
        out.append(pick[0]["id"])
    return list(dict.fromkeys(out))


async def sessions_missing(sites: list[Any], *, caller: Any = None) -> list[str]:
    """The sites in `sites` with no kept session to start signed in with (F1223): neither the person's own nor one
    shared with them, by `resolve_sessions`' own rule. A build is still saved without them; signing in is a person's,
    in Sessions."""
    missing: list[str] = []
    for site in sites or []:
        try:
            await resolve_sessions([site], caller=caller)
        except PickRefused as refused:
            if not refused.ids:  # none at all; more than one is a pick, not a missing session
                missing.append(str(site).strip())
    return list(dict.fromkeys(missing))

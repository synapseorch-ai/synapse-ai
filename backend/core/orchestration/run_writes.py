"""What a run changed in the workspace, from its own writes, in words (F490, BWA1, D274).

The AI builder used to end a build or a change with a model's summary of what happened, and the summary said what
the model believed: "No workspace changes were made" after a build that created workflows, agents and tools; "the
schedule has been disabled", "the model was updated" with nothing changed; "it successfully books a train ticket"
from agents with no tools. A model asked to describe a run is asked to remember it.

This is the run's own record instead. Every `resource_saved` event a step yields (a builder tool's write: created,
updated, deleted) is kept in the run's state as it passes the engine, checkpointed with the run, so it survives a
resume on another worker; and the last error a builder tool returned is kept beside it. `render` turns that record
into fixed sentences, so what the person reads is exactly what the run did:

    Built the workflow "Invoice reader". Created the agents "Reader" and "Approver". Created the schedule "Every
    Friday", paused: turn it on under Schedules when you're ready.

A run that changed nothing says so, and what last failed if anything did.
"""
from __future__ import annotations

import json
from typing import Any

#: The run's writes, one entry per `resource_saved` event: {kind, action, id, name, …}.
WRITES_KEY = "_run_writes"
#: The last errors builder tools returned, newest last (a handful).
FAILURES_KEY = "_run_failures"
#: The writes in words, re-rendered after every write: what the AI builder's summary prints.
TEXT_KEY = "run_changes"
#: Why a build saved no workflow, when `finish_build` can say (F768): the tools its plan uses that nothing provides.
STOPPED_KEY = "_run_stopped"

NOTHING_CHANGED = "Nothing in your workspace changed."

_NOUN = {"orchestration": "workflow", "agent": "agent", "tool": "tool", "skill": "skill", "schedule": "schedule",
         "mcp_server": "app", "knowledge_base": "knowledge base", "document": "document", "memory": "memory item"}


def record(state: dict, event: dict) -> None:
    """Keep one `resource_saved` event in the run's state, and re-render the words."""
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    entry: dict[str, Any] = {"kind": event.get("kind"), "action": event.get("action"),
                             "id": event.get("id"), "name": event.get("name") or event.get("id")}
    if event.get("reason"):
        # Room for a rollback's three validation issues (F867).
        entry["reason"] = str(event["reason"])[:600]
    if entry["kind"] == "schedule" and resource:
        entry["enabled"] = bool(resource.get("enabled"))
        if resource.get("cron_expression"):
            entry["when"] = f'on the cron "{resource["cron_expression"]}" (UTC)'
        elif resource.get("interval_value"):
            entry["when"] = f'every {resource["interval_value"]} {resource.get("interval_unit") or "minutes"}'

    writes = state.setdefault(WRITES_KEY, [])
    if isinstance(writes, list):
        writes.append(entry)
    state[TEXT_KEY] = render(state.get(WRITES_KEY), state.get(FAILURES_KEY), state.get(STOPPED_KEY))


def note_stopped(state: dict, event: dict) -> None:
    """Keep why the build saved nothing (`finish_build`'s `build_stopped` event, F768), and re-render the words."""
    reason = str(event.get("reason") or "").strip()
    if reason:
        state[STOPPED_KEY] = reason[:600]
        state[TEXT_KEY] = render(state.get(WRITES_KEY), state.get(FAILURES_KEY), state.get(STOPPED_KEY))


def note_failure(state: dict, event: dict) -> None:
    """Keep the error a builder tool returned (its result is `{"error": …}`), and re-render the words."""
    from core.builder_tools import BUILDER_TOOL_NAMES

    if event.get("tool_name") not in BUILDER_TOOL_NAMES:
        return  # an ordinary workflow's tools are its own business, not this record's
    preview = str(event.get("preview") or "")
    if not preview.startswith('{"error"'):
        return
    try:
        error = json.loads(preview).get("error")
    except (ValueError, AttributeError):
        error = preview[len('{"error": "'):].rstrip('"}')[:300]
    if not error:
        return
    failures = state.setdefault(FAILURES_KEY, [])
    if isinstance(failures, list):
        failures.append({"tool": event.get("tool_name"), "error": str(error)[:300]})
        del failures[:-5]
    state[TEXT_KEY] = render(state.get(WRITES_KEY), state.get(FAILURES_KEY), state.get(STOPPED_KEY))


def _quoted(names: list[str]) -> str:
    names = [f'"{n}"' for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _net(writes: list[dict]) -> dict[tuple[str, str], dict]:
    """One line per resource: what it ended as. Created then updated is created; created then deleted is gone with
    the reason; updated then deleted is deleted."""
    net: dict[tuple[str, str], dict] = {}
    for w in writes:
        if not isinstance(w, dict) or not w.get("kind"):
            continue
        key = (str(w["kind"]), str(w.get("id") or w.get("name")))
        before = net.get(key)
        action = w.get("action")
        if before is None:
            net[key] = dict(w)
            continue
        if action == "updated" and before.get("action") in ("created", "updated"):
            before.update({k: v for k, v in w.items() if k != "action" and v is not None})
        elif action == "deleted":
            net[key] = {**w, "was_created": before.get("action") == "created"}
        else:
            net[key] = {**before, **w}
    return net


def render(writes: Any, failures: Any = None, stopped: Any = None) -> str:
    """The run's writes as fixed sentences, or `NOTHING_CHANGED` (and what last failed); a build that stopped says why
    instead (F768)."""
    net = _net(writes if isinstance(writes, list) else [])
    groups: dict[tuple[str, str], list[str]] = {}
    schedules: list[str] = []
    removed: list[str] = []
    settings_changed = False
    for (kind, _), w in net.items():
        name = str(w.get("name") or w.get("id") or kind)
        action = w.get("action")
        if kind == "schedule" and action in ("created", "updated"):
            state = "paused" if w.get("enabled") is False else ("on" if w.get("enabled") else None)
            verb = "Created" if action == "created" else "Changed"
            when = f" to run {w['when']}" if w.get("when") else ""
            if state == "paused":
                schedules.append(f'{verb} the schedule "{name}"{when}, paused: turn it on under Schedules when '
                                 f"you're ready.")
            else:
                schedules.append(f'{verb} the schedule "{name}"{when}' + (", and it is on." if state == "on" else "."))
            continue
        if kind == "settings":
            # The workspace's own settings (settings_set, F1285): one thing, said as the person reads it.
            settings_changed = True
            continue
        if action == "deleted" and w.get("was_created"):
            reason = w.get("reason") or "it never passed validation"
            removed.append(f'Removed the {_NOUN.get(kind, kind)} "{name}" again: {reason}.')
            continue
        groups.setdefault((kind, str(action)), []).append(name)

    lines: list[str] = []
    verbs = {"created": "Created", "updated": "Changed", "deleted": "Deleted"}
    for kind in ("orchestration", "agent", "tool", "skill", "mcp_server", "knowledge_base"):
        for action in ("created", "updated", "deleted"):
            names = groups.pop((kind, action), [])
            if not names:
                continue
            noun = _NOUN.get(kind, kind)
            if kind == "orchestration" and action == "created":
                lines.append(f"Built the {noun}{'s' if len(names) > 1 else ''} {_quoted(names)}.")
            else:
                lines.append(f"{verbs[action]} the {noun}{'s' if len(names) > 1 else ''} {_quoted(names)}.")
    for (kind, action), names in groups.items():  # a kind not listed above, said plainly
        noun = _NOUN.get(kind, kind.replace("_", " "))
        lines.append(f"{verbs.get(action, action.title())} the {noun} {_quoted(names)}.")
    if settings_changed:
        lines.append("Changed this workspace's settings.")
    lines += schedules + removed
    if isinstance(stopped, str) and stopped.strip():
        lines.append(stopped.strip())

    if lines:
        return " ".join(lines)
    last = (failures or [])[-1] if isinstance(failures, list) and failures else None
    if last:
        return f"{NOTHING_CHANGED} The last thing that failed: {last.get('error')}"
    return NOTHING_CHANGED

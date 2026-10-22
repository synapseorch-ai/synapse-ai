"""The change route's code after the person's yes, and what it remembers (SAI2-P2, D308; F796, F797).

* `read_change_context` (E1, before the planner): from the person's cards on Synapse AI's board, the last change
  applied (what "undo that" undoes) and a which-one question still waiting (its pick list).
* `approve_change` (E1, on F754's yes side): checks the yes itself, and that what is approved is what the review
  showed (the digest `check_change` took before the person saw it), then records the person's approval of exactly
  those calls (F778, `review.record_approval`).
* `apply_change` (E1): each call through `registry.invoke(confirm_policy="approved_review")`, as the person, with the
  approval and the approved object both read from the run here, never from a model's arguments (MCP2's point 2).
  Before each, the target is read again: one changed since the review is left as it is, and said. After each, it is
  read once more and compared with what the op was to leave (`change_ops.differs`): a change without effect is said
  plainly and counts as not done. The builder's own `resource_saved` events reach the run (F695's host half), so
  `run_writes` says what changed, as for every other route. The card keeps each applied op's before-image (F797).
"""
from __future__ import annotations

import json
from typing import Any

from core.builder_tools import register_builder_tool
from core.native_builder import change_ops as C
from core.native_builder.change_tools import CHANGED, SET_ASIDE, _caller, _card, _quoted, _run, read, reviewed_of
from core.product_tools.errors import AlreadySpent, ToolRefusal

#: The run-state keys the route reads: the check's answer and the review's.
CHECKED_KEY, REVIEW_ANSWER_KEY = "change_checked", "manage_review"


def checked_in(state: dict) -> dict:
    raw = state.get(CHECKED_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


# ── before the planner ────────────────────────────────────────────────────────────────────────────────────────────

def _newest(cards: list) -> list:
    return sorted(cards, key=lambda c: c.updated_at.timestamp() if c.updated_at else 0.0, reverse=True)


async def read_change_context(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The person's last applied change and a pick list still waiting, from their cards: what the planner needs to
    undo a change or to read "the second one" (P4's `read_turn` takes the pick over)."""
    from core.boards import assistant

    person = _caller().user_id
    mine = (lambda cards: [c for c in cards if c.owner_id == person]) if person else (lambda cards: cards)
    try:
        done = mine(await assistant.cards_for(person, admin=True, closed=True))
        open_ = mine(await assistant.cards_for(person, admin=True))
    except Exception:  # noqa: BLE001 — no board yet, or a store that cannot read it: nothing to remember
        return {"last_change": None, "waiting_pick": None}
    last = next((c for c in _newest(done) if c.stage == CHANGED and (c.data or {}).get("applied")), None)
    waiting = next((c for c in _newest(open_) if ((c.data or {}).get("change") or {}).get("picks")), None)
    return {
        "last_change": None if last is None else {
            "key": last.key, "title": last.title,
            "ops": [{"op": a["op"], "target": a["target"].get("name"), "says": a.get("says", "")}
                    for a in last.data["applied"]],
        },
        "waiting_pick": None if waiting is None else {
            "question": waiting.data["change"].get("question"), "picks": waiting.data["change"]["picks"],
        },
    }


# ── the yes ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def approve_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder import review
    from core.product_tools.approved import digest

    run_id, run = _run()
    if run is None:
        return {"error": "There is no change waiting here to approve."}
    state = run.shared_state
    answer = state.get(REVIEW_ANSWER_KEY)
    answer = answer.get("feedback") if isinstance(answer, dict) else answer
    if not review.is_yes(answer):
        return {"error": "The change was not approved, so nothing is done."}
    checked = checked_in(state)
    reviewed = reviewed_of(checked)
    if not checked.get("calls") or digest(reviewed) != checked.get("digest"):
        return {"error": "What is waiting is not what the review showed, so nothing is done."}
    calls = [(tool, args) for tool, args in checked["calls"]]
    review.record_approval(state, reviewed, _caller(), calls, run_id=run_id)
    return {"approved": len(calls)}


# ── the apply ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _shown(value: Any) -> str:
    return _quoted(value) if isinstance(value, str) else json.dumps(value, default=str)


def _moved_since(before: dict | None, now: dict | None) -> list[str]:
    """The fields of the review's before-image the target no longer reads as."""
    if before is None or now is None:
        return []
    skipped = set(before.get("_not_kept") or [])
    return [k for k, v in before.items() if k != "_not_kept" and k not in skipped and not C._same(v, now.get(k))]


async def _created_id(item: dict, result: Any) -> str | None:
    """What a create made: its id from the tool's answer, else the one thing of its kind with its name."""
    from core.native_builder.change_tools import candidates

    kind = C.OPS[item["op"]].target
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        nested = parsed.get("orchestration" if kind == "workflow" else kind)
        for record in (nested, parsed):
            if isinstance(record, dict):
                for key in ("id", "orch_id", "agent_id", "schedule_id"):
                    if record.get(key):
                        return str(record[key])
    name = (item["target"].get("name") or "").lower()
    found = [i["id"] for i in await candidates(kind) or [] if i["name"].lower() == name]
    return found[0] if len(found) == 1 else None


async def apply_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder.review import approved_review
    from core.product_tools import areas, build, registry

    run_id, run = _run()
    if run is None:
        return {"error": "There is no approved change here to apply."}
    state = run.shared_state
    checked = checked_in(state)
    approval = approved_review(state)
    reviewed = reviewed_of(checked)
    caller = _caller()
    areas.ensure_loaded()
    applied: list[dict] = []
    failed: list[str] = []
    with build.using_module(server_module), build.collecting_events(events):
        for item in checked.get("ops") or []:
            if item.get("status") != "ready":
                continue
            op, target = C.OPS[item["op"]], item["target"]
            label = f"the {op.target} {_quoted(target.get('name') or target.get('id'))}"
            now = None if op.kind == "create" else await read(op.target, target["id"])
            if op.kind == "delete" and now is None:
                # Gone, as the op asked: this apply ran before (a redelivered job, a resumed step) or someone else
                # deleted it since. Either way there is nothing to do and nothing is undone.
                applied.append({"op": op.name, "target": target, "says": item.get("says", ""),
                                "before": item.get("before"), "fields": item.get("fields"), "already": True})
                continue
            if op.kind != "create" and now is None:
                failed.append(f"Not done: {label} is not there any more.")
                continue
            if op.kind not in ("create", "delete") and not C.differs(now, item.get("expected")):
                # Already as the op leaves it (an apply run twice for one approval, MCP2's finding 1): done.
                applied.append({"op": op.name, "target": target, "says": item.get("says", ""),
                                "before": item.get("before"), "fields": item.get("fields"), "already": True})
                continue
            if op.kind != "delete" and (moved := _moved_since(item.get("before"), now)):
                failed.append(f"Not done: {label} changed since you approved this ({', '.join(moved)}), so I left "
                              "it as it is now. Ask again to change it.")
                continue
            call = item["call"]
            try:
                result = await registry.invoke(call["tool"], caller, call["args"], confirm_policy="approved_review",
                                               review=approval, reviewed=reviewed)
            except AlreadySpent:
                result = None  # made by an earlier pass of this apply: found and checked below, not made again
            except ToolRefusal as why:
                failed.append(f"Not done: {label}: {why}")
                continue
            if isinstance(result, dict) and result.get("status") == "needs_confirmation":
                failed.append(f"Not done: {label}: your approval does not cover this call.")
                continue
            target_id = target.get("id") or (await _created_id(item, result) if op.kind == "create" else None)
            after = await read(op.target, target_id) if target_id else None
            off = C.differs(after, item.get("expected"))
            if off:
                failed.append(_no_effect(label, off))
                continue
            applied.append({"op": op.name, "target": {**target, "id": target_id}, "says": item.get("says", ""),
                            "before": item.get("before"), "fields": item.get("fields")})
    effect = " ".join(failed)
    if applied or failed:
        await _card({**checked, "ops": checked.get("ops") or []}, stage=CHANGED if applied else SET_ASIDE,
                    data={"change": checked, "applied": applied, "failed": failed})
    return {"object": "change_applied", "applied": [a["op"] for a in applied], "failed": failed,
            "effect": (" " + effect) if effect else ""}


def _no_effect(label: str, off: dict) -> str:
    if "exists" in off:
        wanted, _ = off["exists"]
        return (f"Not done: I asked for {label} to be made, but it is not there." if wanted
                else f"Not done: I deleted {label}, but it is still there.")
    parts = ", ".join(f"{k} should read {_shown(want)} but reads {_shown(got)}" for k, (want, got) in off.items())
    return f"Not done as asked: after the change, {label}'s {parts}."


for _schema, _handler in (
    ({"type": "function", "function": {"name": "read_change_context", "description": (
        "Synapse AI's change route (code): the person's last applied change and a pick list still waiting."),
        "parameters": {"type": "object", "properties": {}}}}, read_change_context),
    ({"type": "function", "function": {"name": "approve_change", "description": (
        "Synapse AI's change route (code): record the person's yes to the change the review showed."),
        "parameters": {"type": "object", "properties": {}}}}, approve_change),
    ({"type": "function", "function": {"name": "apply_change", "description": (
        "Synapse AI's change route (code): make the approved change and check it took effect."),
        "parameters": {"type": "object", "properties": {}}}}, apply_change),
):
    register_builder_tool(_schema, _handler)

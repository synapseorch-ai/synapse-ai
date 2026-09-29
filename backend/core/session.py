"""
Session and conversation state management.

Backed by the `chat_sessions` table, which the standalone server and the scale
worker now share. They used to disagree: the worker held the canonical history
in Postgres and then *wrote a session file onto its own local disk* so that
`run_react_loop` could find it here — which meant, in a shared fleet, that a
conversation's history materialised on whichever worker happened to win the
job. That shim is gone; both paths read and write the same rows.

A conversation is keyed by `(session_id, agent_id)`, not by `session_id` alone.
The UI keeps one session id per browser and switches agents underneath it, so
the same id genuinely names several conversations — see `ChatSessionDB`.

Two shapes, one column
----------------------
`messages` is stored as `[{role, content}]`, which is what the worker writes
and what `GET /v2/chat/{id}/status` returns. The per-turn extras this module
has always kept — the tools used, the timestamp — ride along as additional keys
on the assistant entry, so a reader that only knows about role and content is
unaffected. `turns` is the paired view of the same list, rebuilt on read.

What a turn did (F242)
----------------------
`calls` on the assistant entry is the turn's record of each tool call: the
tool, its arguments and what the model was shown back, clipped. The next turn
is sent a budgeted digest of it (`tool_digest=True`): a follow-up used to see
the user's words and the answer only, so "continue where you left off" started
over and read paths that did not exist. Anything older or longer is one
`session_history` call away. Providers are only ever sent role and content.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any

from core.models import ChatRequest

# ---------------------------------------------------------------------------
# Session-scoped in-memory state (non-persistent by design)
# ---------------------------------------------------------------------------
session_state: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _agent(agent_id: str | None) -> str:
    return agent_id or "default"


def _turns(messages: list[dict]) -> list[dict]:
    """The message list as user/assistant turns.

    Every writer appends the two together, so the pairing is exact. A message
    that does not pair — a tool or system entry written by something else —
    ends the reconstruction rather than being silently folded into a turn.
    """
    turns = []
    i = 0
    while i + 1 < len(messages):
        user, assistant = messages[i], messages[i + 1]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            break
        turn = {
            "user": user.get("content", ""),
            "images": user.get("images", []),
            "assistant": assistant.get("content", ""),
            "tools": assistant.get("tools", []),
            "calls": assistant.get("calls", []),
            # What the turn thought (U60): the chat page's two panels, reopened.
            "reasoning": assistant.get("reasoning", []),
            "thoughts": assistant.get("thoughts", []),
            "timestamp": assistant.get("timestamp"),
        }
        if assistant.get("approval"):
            # A question nobody answered: the card, marked why (F247).
            turn["approval"] = assistant["approval"]
        turns.append(turn)
        i += 2
    return turns


# ---------------------------------------------------------------------------
# Images a turn carried (F245)
# ---------------------------------------------------------------------------

def image_notes(images: list[str] | None) -> list[dict]:
    """What a turn keeps of its attached images: their type, never the image.

    The image itself goes to the model on every call of the turn it was
    attached to and nowhere after; this is how a later turn is told it existed.
    """
    notes = []
    for image in images or []:
        mime = "image"
        if isinstance(image, str) and image.startswith("data:"):
            mime = image[len("data:"):].split(",", 1)[0].split(";", 1)[0].strip().lower() or "image"
        notes.append({"type": mime})
    return notes


def _image_placeholder(notes: list[dict]) -> str:
    kinds = sorted({str(n.get("type") or "image").split("/")[-1].upper() for n in notes})
    count = len(notes)
    return (f"[{count} image{'s' if count != 1 else ''} ({', '.join(kinds)}) "
            f"{'were' if count != 1 else 'was'} attached to this message. "
            f"{'They are' if count != 1 else 'It is'} no longer attached — you cannot see "
            f"{'them' if count != 1 else 'it'} now; ask for {'them' if count != 1 else 'it'} "
            "again if you need to.]")


# ---------------------------------------------------------------------------
# What a turn did — its tool calls (F242)
# ---------------------------------------------------------------------------

CALL_ARGS_CHARS = 1_000      # a call's arguments, as JSON, when stored
CALL_RESULT_CHARS = 4_000    # what the call returned, when stored
DIGEST_DETAILED_TURNS = 3    # the newest turns sent with arguments and results
DIGEST_ARGS_CHARS = 200
DIGEST_RESULT_CHARS = 300
DIGEST_MAX_CHARS = 6_000     # the whole digest, across every turn sent
PANEL_KEEP = 20              # a saved turn's newest reasoning / thoughts (U60)
PANEL_CHARS = 4_000          # each of them, when stored

_OBSERVATION_TAG = re.compile(r"^\s*\[Turn \d+ — Observation\]\s*(Tool '[^']*' returned:\s*)?")


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit:,} more characters]"


def _clip_args(args: Any) -> Any:
    """Arguments as they were, unless their JSON is too long to keep whole."""
    try:
        encoded = json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = str(args)
    if len(encoded) <= CALL_ARGS_CHARS:
        return args if isinstance(args, dict) else {"value": args}
    return {"_clipped": encoded[:CALL_ARGS_CHARS]}


def call_record(tool: str, args: Any, observation: str) -> dict:
    """One call as a turn keeps it. `observation` is what the model was shown
    back — a result, a refusal, an error — with the transcript's turn tag
    taken off, so two turns' records of the same result compare equal."""
    result = _OBSERVATION_TAG.sub("", str(observation or "")).strip()
    return {"tool": str(tool or ""), "args": _clip_args(args or {}), "result": _clip(result, CALL_RESULT_CHARS)}


def calls_from_events(events: list[dict]) -> list[dict]:
    """A turn's calls from its events, for the paths that only see events: a
    stopped turn and the fleet's chat job. A delegate's and a workflow step's
    events are theirs, not this turn's, and are left out."""
    calls: list[dict] = []
    for event in events:
        if event.get("delegate_from") or event.get("orch_step_id"):
            continue
        if event.get("type") == "tool_execution":
            calls.append(call_record(event.get("tool_name"), event.get("args") or {}, ""))
        elif event.get("type") == "tool_result" and calls and not calls[-1]["result"]:
            calls[-1]["result"] = _clip(event.get("preview") or "", CALL_RESULT_CHARS)
            if event.get("refused"):
                # Not run, and why (U68) — as the engine's own record says.
                calls[-1].update(refused=True, reason=event.get("reason") or "")
    return calls


def panel_text(items: list | None) -> list[str]:
    """A turn's reasoning or thoughts as a saved turn keeps them (U60): the
    newest `PANEL_KEEP`, each clipped, none empty."""
    kept = [str(t) for t in (items or []) if str(t or "").strip()]
    return [_clip(t, PANEL_CHARS) for t in kept[-PANEL_KEEP:]]


def thinking_from_events(events: list[dict], resumed: dict | None = None) -> tuple[list[str], list[str]]:
    """A turn's `(reasoning, thoughts)` from its events, for the paths that save
    a turn from outside the engine: the fleet's chat job and a stopped turn.
    `resumed` is the state a resumed turn started from, whose lists come first.
    A delegate's and a workflow step's are theirs, as in `calls_from_events`."""
    reasoning = list((resumed or {}).get("reasoning") or [])
    thoughts = list((resumed or {}).get("thoughts") or [])
    for event in events:
        if event.get("delegate_from") or event.get("orch_step_id"):
            continue
        if event.get("type") == "llm_reasoning":
            reasoning.append(event.get("reasoning") or "")
        elif event.get("type") == "llm_thought":
            thoughts.append(event.get("thought") or "")
    return panel_text(reasoning), panel_text(thoughts)


def turn_calls(progress: dict | None, events_since: list[dict] | None = None,
               resumed: dict | None = None) -> list[dict]:
    """The calls of a turn saved from outside it: those its last turn-boundary
    snapshot holds (the engine's own record), then any made after it.

    `resumed` is the state a resumed turn started from. Until this request has
    seen a snapshot of its own, that state's calls are the turn's so far: a
    resumed turn that answered, or was stopped, before its first turn boundary
    used to keep only what it did after the answer (F311).
    """
    base = progress if progress is not None else (resumed or {})
    return [dict(c) for c in (base.get("calls") or [])] + calls_from_events(events_since or [])


def tools_from_calls(calls: list[dict]) -> list[str]:
    """The older `tools` field — "name: result" — for a turn recorded as calls."""
    return [f"{c.get('tool')}: {c.get('result', '')}" for c in calls]


def _legacy_calls(tools: list) -> list[dict]:
    """A turn saved before `calls` existed: "name: output" strings, no arguments."""
    calls = []
    for entry in tools or []:
        name, _, output = str(entry).partition(":")
        name = name.strip()
        if name:
            calls.append({"tool": name, "args": None, "result": output.strip()})
    return calls


def _digest_line(call: dict) -> str:
    args = call.get("args")
    shown_args = ""
    if args:
        try:
            shown_args = " " + _clip(json.dumps(args, default=str, ensure_ascii=False), DIGEST_ARGS_CHARS)
        except (TypeError, ValueError):
            shown_args = " " + _clip(str(args), DIGEST_ARGS_CHARS)
    result = " ".join(str(call.get("result") or "").split())
    return f"- {call.get('tool')}{shown_args} → {_clip(result, DIGEST_RESULT_CHARS) or '(no output)'}"


def _digest_names(calls: list[dict]) -> str:
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.get("tool") or "?"] = counts.get(call.get("tool") or "?", 0) + 1
    return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in counts.items())


def _digests(turns: list[dict]) -> dict[int, str]:
    """Each turn's note, by its index in `turns`: the newest few in detail, the
    rest by name, newest first until the budget is spent."""
    notes: dict[int, str] = {}
    used = 0
    detailed = 0
    for index in range(len(turns) - 1, -1, -1):
        calls = turns[index].get("calls") or _legacy_calls(turns[index].get("tools"))
        if not calls:
            continue
        if detailed < DIGEST_DETAILED_TURNS:
            detailed += 1
            note = ("[Tool calls you made for this answer, kept by the system for you — not part of "
                    "your reply. Read any earlier turn in full with session_history.]\n"
                    + "\n".join(_digest_line(c) for c in calls))
        else:
            note = f"[Tools you used for this answer: {_digest_names(calls)}]"
        if used + len(note) > DIGEST_MAX_CHARS:
            if used:
                break
            note = note[:DIGEST_MAX_CHARS] + "…]"
        notes[index] = note
        used += len(note)
    return notes


# ---------------------------------------------------------------------------
# Public API — used by react_engine and routes
# ---------------------------------------------------------------------------

def _get_session_id(request: ChatRequest) -> str:
    return request.session_id or "default"


async def _get_conversation_history(session_id: str, agent_id: str | None = None) -> list[dict]:
    """Return the list of conversation turns for this session."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    return _turns(row["messages"]) if row else []


async def _save_conversation_turn(
    session_id: str,
    agent_id: str | None,
    user: str,
    assistant: str,
    tools: list[str] | None = None,
    calls: list[dict] | None = None,
    images: list[dict] | None = None,
    reasoning: list[str] | None = None,
    thoughts: list[str] | None = None,
):
    """Append a turn to the session and update last_message_at.

    `images` is `image_notes(...)` of what the question carried (F245);
    `reasoning` and `thoughts` are what the turn thought (U60), kept for a
    person reading it back and never sent to the model as history.
    """
    from core.store import sessions as store

    now = _now()
    question = {"role": "user", "content": user}
    if images:
        question["images"] = images
    answer = {"role": "assistant", "content": assistant, "tools": tools or [], "timestamp": now}
    if calls:
        answer["calls"] = calls
    for key, items in (("reasoning", reasoning), ("thoughts", thoughts)):
        if kept := panel_text(items):
            answer[key] = kept
    await store.append_turn(
        session_id,
        _agent(agent_id),
        [question, answer],
    )


async def pop_last_conversation_turn(session_id: str, agent_id: str | None = None,
                                     expect_user: str | None = None) -> dict | None:
    """Take the last turn back, so it can be run again.

    The facade half of `store.pop_last_turn`; see there for why it is a pair
    and not a message, and why `expect_user` matters. Retry uses it to drop the
    answer that failed before resending, and edit-and-resend to drop both
    halves before sending the rewritten question.
    """
    from core.store import sessions as store

    return await store.pop_last_turn(session_id, _agent(agent_id), expect_user=expect_user)


async def get_cli_session_id(session_id: str, agent_id: str | None, provider_key: str) -> str | None:
    """Return the stored CLI session ID for the given provider, or None."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    return (row or {}).get("cli_session_ids", {}).get(provider_key)


async def save_cli_session_id(session_id: str, agent_id: str | None, provider_key: str, cli_id: str):
    """Persist a CLI session ID for this agent+session combination and provider."""
    from core.store import sessions as store

    await store.set_cli_session_id(session_id, _agent(agent_id), provider_key, cli_id)


async def get_last_response_snapshot(session_id: str, agent_id: str | None = None) -> dict:
    """Return {last_response, last_updated} for a session."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    if not row:
        return {"last_response": None, "last_updated": None}

    turns = _turns(row["messages"])
    return {
        "last_response": turns[-1]["assistant"] if turns else None,
        "last_updated": row["last_updated"],
    }


async def get_recent_history_messages(session_id: str, agent_id: str | None = None, *,
                                      tool_digest: bool = False) -> list[dict]:
    """Return last N turns as [role/content] message dicts for the LLM API.

    `tool_digest` appends each turn's note of what its tools did (F242) to its
    answer — chat asks for it. A workflow's `state.history` (F260) does not:
    its CONVERSATION SO FAR is the words, not the calls.
    """
    RECENT_TURNS = 10
    turns = await _get_conversation_history(session_id, agent_id)
    recent = turns[-RECENT_TURNS:] if len(turns) > RECENT_TURNS else turns
    notes = _digests(recent) if tool_digest else {}
    messages = []
    for index, turn in enumerate(recent):
        question = turn["user"]
        if turn.get("images"):
            # Told it was there and is gone, so it can say so (F245): with
            # nothing, a follow-up about the screenshot was answered with a
            # test name and a footer that are not in it.
            question = f"{question}\n\n{_image_placeholder(turn['images'])}" if question else \
                _image_placeholder(turn["images"])
        messages.append({"role": "user", "content": question})
        answer = turn["assistant"]
        if index in notes:
            answer = f"{answer}\n\n{notes[index]}"
        messages.append({"role": "assistant", "content": answer})
    return messages


# ---------------------------------------------------------------------------
# session_history — any earlier turn, on demand (F242)
# ---------------------------------------------------------------------------

SESSION_HISTORY_TOOL_NAME = "session_history"
SESSION_HISTORY_MAX_CHARS = 16_000
_LIST_MAX_TURNS = 100


def build_session_history_tool():
    from core.tools import VirtualTool

    return VirtualTool(
        name=SESSION_HISTORY_TOOL_NAME,
        description=(
            "Read this conversation's earlier turns. With no arguments it lists every turn "
            "(number, time, the question, the tools used); with a turn number it returns that "
            "turn's question, answer and each tool call with its arguments and result. Use it "
            "before redoing work an earlier turn may already have done."
        ),
        inputSchema={
            "type": "object",
            "properties": {"turn": {"type": "integer",
                                    "description": "The turn to open, as numbered in the list (1 = the first)"}},
        },
    )


def _when(turn: dict) -> str:
    return str(turn.get("timestamp") or "")[:16].replace("T", " ")


async def render_session_history(session_id: str, agent_id: str | None, turn: Any = None) -> str:
    """What `session_history` returns: the list of turns, or one turn in full."""
    turns = await _get_conversation_history(session_id, agent_id)
    if not turns:
        return "This conversation has no earlier turns."
    if turn in (None, "", 0, "0"):
        shown = turns[-_LIST_MAX_TURNS:]
        first = len(turns) - len(shown) + 1
        lines = [f"This conversation has {len(turns)} earlier turn{'s' if len(turns) != 1 else ''} "
                 "(oldest first):"]
        for number, entry in enumerate(shown, start=first):
            question = " ".join(str(entry.get("user") or "").split())
            calls = entry.get("calls") or _legacy_calls(entry.get("tools"))
            used = f" → tools: {_digest_names(calls)}" if calls else ""
            lines.append(f"Turn {number} · {_when(entry)} · \"{_clip(question, 120)}\"{used}")
        lines.append('Call session_history with {"turn": n} to open one.')
        return _clip("\n".join(lines), SESSION_HISTORY_MAX_CHARS)
    try:
        number = int(turn)
    except (TypeError, ValueError):
        return f"'{turn}' is not a turn number; call session_history with no arguments to list them."
    if not 1 <= number <= len(turns):
        return f"There is no turn {number}; this conversation has {len(turns)}."
    entry = turns[number - 1]
    images = f" {_image_placeholder(entry['images'])}" if entry.get("images") else ""
    parts = [f"Turn {number} · {_when(entry)}",
             f"Question: {_clip(entry.get('user'), 2_000)}{images}",
             f"Answer: {_clip(entry.get('assistant'), 3_000)}"]
    calls = entry.get("calls") or _legacy_calls(entry.get("tools"))
    if calls:
        parts.append("Tool calls:")
        for index, call in enumerate(calls, start=1):
            args = call.get("args")
            shown = json.dumps(args, default=str, ensure_ascii=False) if args else ""
            parts.append(f"{index}. {call.get('tool')} {shown}".rstrip())
            parts.append(f"   → {call.get('result') or '(no output)'}")
    else:
        parts.append("Tool calls: none")
    return _clip("\n".join(parts), SESSION_HISTORY_MAX_CHARS)


async def list_chat_sessions(agent_id: str | None = None) -> list[dict]:
    """
    List all persisted chat sessions, sorted by last_updated descending.
    Optionally filter by agent_id.
    """
    from core.store import sessions as store

    sessions = []
    for row in await store.load(agent_id=agent_id):
        turns = _turns(row["messages"])
        sessions.append({
            "session_id": row["session_id"],
            "agent_id": row["agent_id"],
            "last_response": turns[-1]["assistant"] if turns else None,
            "last_updated": row["last_updated"],
            "turn_count": len(turns),
            "first_user_message": turns[0]["user"] if turns else None,
        })
    return sessions


async def delete_chat_session(session_id: str, agent_id: str | None = None) -> bool:
    """Delete a session. Returns True if deleted."""
    from core.store import sessions as store

    return await store.delete_one(session_id, _agent(agent_id))


async def clear_all_chat_sessions() -> int:
    """Delete every session for this tenant. Returns the number removed."""
    from core.store import sessions as store

    return await store.clear()


# ---------------------------------------------------------------------------
# Shared session state (in-memory, ephemeral)
# ---------------------------------------------------------------------------

def _get_session_state(session_id: str) -> dict[str, Any]:
    if session_id not in session_state:
        session_state[session_id] = {}
    return session_state[session_id]


def _apply_sticky_args(session_id: str, tool_name: str, tool_args: Any, tool_schema: dict | None = None) -> Any:
    """Normalize tool arguments. No session state tracking."""
    if not isinstance(tool_args, dict):
        tool_args = {}
    return tool_args


def _clear_session_embeddings(session_id: str):
    """Clear session-scoped embeddings (used internally by report auto-embed)."""
    from core.server import memory_store
    if memory_store:
        memory_store.clear_session_embeddings(session_id)
        print(f"DEBUG: Cleared session embeddings for {session_id}")

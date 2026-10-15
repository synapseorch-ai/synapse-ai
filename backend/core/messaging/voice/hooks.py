"""`VoiceHost`: what differs between a self-hosted install and cloud (V2's host hooks).

The self-hosted defaults are here. Cloud's `voice` service (VOX1b) installs its own subclass with `install()`:
admission against credits and the org's concurrent-call limit, billing once per call, the bell and parked
approvals through its own notifications, secrets from `org_secrets`, socket claims shared in Redis, and the move
when a task drains (V10).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as _secrets
import time
from typing import Any, AsyncIterator

log = logging.getLogger("voice")

#: How long a self-hosted call may last (cloud's comes from the plan, V23).
DEFAULT_MAX_MINUTES = 30
#: Call records a self-hosted install keeps (the newest).
KEEP_CALLS = 500


class VoiceHost:
    """The self-hosted host: in-process, one server, no billing."""

    def __init__(self) -> None:
        self._key = _secrets.token_bytes(32)
        self._claimed: dict[str, float] = {}
        self._calls_lock = asyncio.Lock()

    # ── Verification ─────────────────────────────────────────────────────────

    def signing_key(self) -> bytes:
        """Signs socket tokens. Per process here: a token outlives no restart, and it lives 60 s anyway."""
        return self._key

    async def claim_socket(self, nonce: str) -> bool:
        """True once per socket token (single use, V6 check 3)."""
        now = time.time()
        for n, at in list(self._claimed.items()):
            if at < now - 300:
                self._claimed.pop(n, None)
        if not nonce or nonce in self._claimed:
            return False
        self._claimed[nonce] = now
        return True

    def org_name(self, channel: dict) -> str:
        """Who the opening notice says the agent answers for (V25: "…an AI assistant for {org}"). Self-hosted
        names no one; cloud names the workspace."""
        return ""

    def public_base(self) -> str:
        """Where Twilio reaches us: `messaging_webhook_base_url`, as Telegram's webhook mode (V11)."""
        from core.config import load_settings
        return str(load_settings().get("messaging_webhook_base_url") or "").rstrip("/")

    async def secrets(self, channel: dict) -> dict:
        """The channel's credentials. Self-hosted keeps them in the channel document."""
        return dict(channel.get("credentials") or {})

    # ── Admission and limits ─────────────────────────────────────────────────

    async def admit(self, channel: dict, caller: str, call_sid: str = "") -> str | None:
        """None to answer the call, or the one sentence a refused caller hears before the hang-up (V22).

        `call_sid` names the call, so a host that counts live calls (cloud's org limit, V23) can free its
        place when the call is settled."""
        return None

    async def before_turn(self, call: Any) -> str | None:
        """Re-checked before every turn; a sentence ends the call with it (V22's empty wallet)."""
        return None

    def max_minutes(self, channel: dict) -> int:
        voice = channel.get("voice") or {}
        try:
            return max(1, int(voice.get("max_minutes") or DEFAULT_MAX_MINUTES))
        except (TypeError, ValueError):
            return DEFAULT_MAX_MINUTES

    # ── A turn ───────────────────────────────────────────────────────────────

    async def run_turn(self, call: Any, text: str, *, caller_system: str,
                       extra_tools: list) -> AsyncIterator[dict]:
        """One agent turn for the caller's words, streamed (U72's hook), as a visitor (V16)."""
        import core.server as _server
        from core import visitor as _visitor
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        request = ChatRequest(message=text, session_id=call.session_id, agent_id=call.agent_id)
        # Closed with this stream, in the call's task (F528): a turn the call ends early closes the engine's too.
        with _visitor.scope(call.visitor):
            async with contextlib.aclosing(run_react_loop(
                request, _server, progress_events=True, stream_answer=True, caller_system=caller_system,
                log_source="voice", extra_tools=extra_tools,
            )) as events:
                async for event in events:
                    yield event

    async def resume_turn(self, call: Any, answer: dict, *, caller_system: str,
                          extra_tools: list) -> AsyncIterator[dict]:
        """The turn a parked question stopped, carried on inside the live call with the person's answer
        (`answer["resume_state"]`, F474): streamed as `run_turn`'s are, still a visitor's, and saved into the
        call's conversation in the order the caller hears it."""
        import core.server as _server
        from core import visitor as _visitor
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        request = ChatRequest(message=str(answer.get("message") or ""), session_id=call.session_id,
                              agent_id=call.agent_id)
        with _visitor.scope(call.visitor):
            async with contextlib.aclosing(run_react_loop(
                request, _server, progress_events=True, stream_answer=True, caller_system=caller_system,
                log_source="voice", extra_tools=extra_tools, resume_state=answer.get("resume_state") or {},
            )) as events:
                async for event in events:
                    yield event

    async def cut_turn(self, call: Any, *, heard: str, fields: dict) -> bool:
        """A turn the caller cut, when the host's own job keeps it (F486): a workflow agent's turn runs on a fleet
        whose job saves it whatever the call does (V14), so the host marks that saved answer as heard (`heard`, the
        clause that was playing) with the turn's notes (`fields`), and the call saves no copy. False: this host's
        turns run in the call, and the call saves the cut itself."""
        return False

    async def resume_after_call(self, call: Any, answer: dict) -> None:
        """An answer the call took but could not say (the caller hung up first, F474): resumed behind, as an
        answer after the call is, and the bell asks for a call-back."""
        from core import api_approvals
        api_approvals.resume_behind(call.session_id, call.agent_id, answer["pending"],
                                    approve=bool(answer.get("approved")), reason=str(answer.get("reason") or ""))

    async def call_live(self, call: Any) -> None:
        """The call is on the line. Self-hosted, its door finds it in this process (`live_for_session`); a host
        whose answers arrive in another process (cloud's api) says where to send them."""

    async def call_over(self, call: Any) -> None:
        """The call is over: no answer reaches it any more (`call_live`'s other half)."""

    async def park(self, call: Any, *, event: dict, message: str, progress: dict | None,
                   caller_system: str) -> str:
        """A caller's `confirm` tool waits for a person (V16): keep the turn's snapshot and ask the bell in the
        person tone. Returns the approval id; raises when it could not be asked. Self-hosted parks through
        `api_approvals`; cloud keeps the same conversation row and asks through its own bell."""
        from core import api_approvals
        return await api_approvals.park(
            session_id=call.session_id, agent={"id": call.agent_id, "name": call.agent_name},
            event=event, message=message, progress=progress,
            resume={"caller_system": caller_system}, hours=24, door="voice",
            caller={"number": call.caller, "visitor": call.visitor.to_dict()})

    # ── After ────────────────────────────────────────────────────────────────

    async def settle(self, record: dict, *, final: bool) -> None:
        """A session of the call ended, with the record's segments summed (V10). `final` is False while the call
        is being moved or picked up again, so a host bills once, when the call is over (cloud, V21);
        self-hosted has nothing to bill."""

    async def notify(self, kind: str, title: str, *, body: str = "", data: dict | None = None) -> None:
        """The bell (V16's approvals are asked by `api_approvals`; this is voicemail and refusals)."""
        from core.notifications import hub
        hub.publish(kind, title, body=body, data=data or {})

    # ── Call records (V31's recent calls, VOX1b's settlement) ────────────────

    async def save_call(self, record: dict) -> None:
        from core.store import collections
        async with self._calls_lock:
            calls = [c for c in await collections.load("voice_calls") if c.get("id") != record["id"]]
            calls.append(record)
            calls.sort(key=lambda c: c.get("started_at") or "")
            await collections.save("voice_calls", calls[-KEEP_CALLS:])

    async def update_call(self, call_sid: str, changes: dict) -> dict | None:
        """Merge into a call's record: `segments`, `approvals` and `first_words_ms` are appended to, the rest
        replaced."""
        from core.store import collections
        async with self._calls_lock:
            calls = await collections.load("voice_calls")
            record = next((c for c in calls if c.get("id") == call_sid), None)
            if record is None:
                return None
            for key, value in changes.items():
                if key in ("segments", "approvals", "first_words_ms"):
                    record[key] = list(record.get(key) or []) + list(value or [])
                else:
                    record[key] = value
            record["seconds"] = sum(int(s.get("seconds") or 0) for s in record.get("segments") or [])
            await collections.save("voice_calls", calls)
            return record

    async def get_call(self, call_sid: str) -> dict | None:
        from core.store import collections
        return next((c for c in await collections.load("voice_calls") if c.get("id") == call_sid), None)

    async def list_calls(self, channel_id: str, limit: int = 50) -> list[dict]:
        from core.store import collections
        calls = [c for c in await collections.load("voice_calls") if c.get("channel_id") == channel_id]
        return sorted(calls, key=lambda c: c.get("started_at") or "", reverse=True)[:limit]


_host: VoiceHost | None = None


def install(host: VoiceHost | None) -> None:
    """Replace the host (cloud's voice service); None puts the self-hosted one back."""
    global _host
    _host = host


def current() -> VoiceHost:
    global _host
    if _host is None:
        _host = VoiceHost()
    return _host

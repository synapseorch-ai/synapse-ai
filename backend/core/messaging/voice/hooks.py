"""`VoiceHost`: what differs between a self-hosted install and cloud (V2's host hooks).

The self-hosted defaults are here. Cloud's `voice` service (VOX1b) installs its own subclass with `install()`:
admission against credits and the org's concurrent-call limit, billing per call, the bell through its own
notifications, secrets from `org_secrets`, socket claims shared in Redis, and the SIGTERM move (V10).
"""
from __future__ import annotations

import asyncio
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

    def public_base(self) -> str:
        """Where Twilio reaches us: `messaging_webhook_base_url`, as Telegram's webhook mode (V11)."""
        from core.config import load_settings
        return str(load_settings().get("messaging_webhook_base_url") or "").rstrip("/")

    async def secrets(self, channel: dict) -> dict:
        """The channel's credentials. Self-hosted keeps them in the channel document."""
        return dict(channel.get("credentials") or {})

    # ── Admission and limits ─────────────────────────────────────────────────

    async def admit(self, channel: dict, caller: str) -> str | None:
        """None to answer the call, or the one sentence a refused caller hears before the hang-up (V22)."""
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
        with _visitor.scope(call.visitor):
            async for event in run_react_loop(
                request, _server, progress_events=True, stream_answer=True, caller_system=caller_system,
                log_source="voice", extra_tools=extra_tools,
            ):
                yield event

    # ── After ────────────────────────────────────────────────────────────────

    async def settle(self, record: dict) -> None:
        """A call ended (each segment, V10). Cloud charges it here (V21); self-hosted has nothing to bill."""

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

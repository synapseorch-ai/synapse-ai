"""The frames on a call's WebSocket (V2's ``TurnTransport``; ``TextRelay`` is ConversationRelay's).

Twilio's ConversationRelay and Telnyx's Conversation Relay share these frames (19 → V1), so one codec serves both.

In, from the provider (twilio.com/docs/voice/conversationrelay/websocket-messages, 2026-10-10):
  ``setup``      callSid, from, to, customParameters, … — once, right after the socket opens
  ``prompt``     voicePrompt, lang, last — the caller's recognised words; ``last`` closes an utterance
  ``interrupt``  utteranceUntilInterrupt, durationUntilInterruptMs — the caller spoke over us (barge-in)
  ``dtmf``       digit — off in VOX1 (V18)
  ``error``      description

Out, to the provider:
  ``text``       token, last[, interruptible] — spoken; ``last`` ends our talk cycle
  ``end``        handoffData (a JSON string) — ends the session; the ``<Connect action>`` callback gets it

Ten consecutive frames the provider cannot read close the socket (error 64105) and the call fails, so only
these two kinds are ever sent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Frame:
    type: str
    raw: dict = field(default_factory=dict)


@dataclass
class Setup(Frame):
    call_sid: str = ""
    caller: str = ""
    called: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class Prompt(Frame):
    text: str = ""
    lang: str = ""
    last: bool = True


@dataclass
class Interrupt(Frame):
    heard: str = ""
    duration_ms: int = 0


@dataclass
class ErrorFrame(Frame):
    description: str = ""


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def parse(raw: str | bytes) -> Frame:
    """One inbound frame. Anything unreadable is a plain `Frame` of type ``unknown``, never an exception."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return Frame("unknown")
    if not isinstance(data, dict):
        return Frame("unknown")
    kind = str(data.get("type") or "unknown")
    if kind == "setup":
        return Setup(kind, data, call_sid=str(data.get("callSid") or ""), caller=str(data.get("from") or ""),
                     called=str(data.get("to") or ""), params=dict(data.get("customParameters") or {}))
    if kind == "prompt":
        return Prompt(kind, data, text=str(data.get("voicePrompt") or ""), lang=str(data.get("lang") or ""),
                      last=_flag(data.get("last"), True))
    if kind == "interrupt":
        try:
            ms = int(data.get("durationUntilInterruptMs") or 0)
        except (TypeError, ValueError):
            ms = 0
        return Interrupt(kind, data, heard=str(data.get("utteranceUntilInterrupt") or ""), duration_ms=ms)
    if kind == "error":
        return ErrorFrame(kind, data, description=str(data.get("description") or ""))
    return Frame(kind, data)


def text_frame(token: str, *, last: bool = False, interruptible: bool | None = None) -> str:
    frame: dict[str, Any] = {"type": "text", "token": token, "last": last}
    if interruptible is not None:
        frame["interruptible"] = interruptible
    return json.dumps(frame)


def end_frame(handoff: dict | None = None) -> str:
    frame: dict[str, Any] = {"type": "end"}
    if handoff:
        frame["handoffData"] = json.dumps(handoff, separators=(",", ":"))
    return json.dumps(frame)


class TurnTransport:
    """What `VoiceCall` talks to: receive the caller's frames, send speech, end the session."""

    async def receive(self) -> Frame:  # pragma: no cover - interface
        raise NotImplementedError

    async def say(self, text: str, *, last: bool, interruptible: bool | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    async def end(self, handoff: dict | None = None) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class TextRelay(TurnTransport):
    """ConversationRelay over any socket with async ``receive_text`` / ``send_text`` (Starlette's WebSocket)."""

    def __init__(self, receive_text: Callable[[], Awaitable[str]], send_text: Callable[[str], Awaitable[None]]):
        self._receive_text = receive_text
        self._send_text = send_text
        self.ended = False

    async def receive(self) -> Frame:
        return parse(await self._receive_text())

    async def say(self, text: str, *, last: bool, interruptible: bool | None = None) -> None:
        if self.ended:
            return
        await self._send_text(text_frame(text, last=last, interruptible=interruptible))

    async def end(self, handoff: dict | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        await self._send_text(end_frame(handoff))

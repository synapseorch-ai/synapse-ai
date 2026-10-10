"""`CallProvider`: what one telephony vendor does for a call (V2). Twilio's is `twilio.TwilioProvider`.

The loop (`VoiceCall`) never knows which vendor it is talking to; everything vendor-shaped is here: verifying an
incoming call, the markup that answers it, the end callback, transfer / voicemail / hang-up markup, and the
account's numbers.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CallInfo:
    """A verified incoming call."""
    call_sid: str
    caller: str
    called: str


@dataclass
class AnswerOptions:
    """How a number answers (the channel's ``voice`` block, resolved)."""
    notice: str = ""
    language: str = "en-US"
    voice: str = ""
    tts_provider: str = ""
    resume: bool = False


@dataclass
class SessionEnd:
    """The ``<Connect action>`` callback: why the relay session ended."""
    call_sid: str
    status: str                 # ended | completed | failed
    duration: int               # seconds the session lasted (SessionDuration)
    call_status: str = ""       # the call's own status (in-progress when it carries on)
    handoff: dict = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""

    @property
    def caller_hung_up(self) -> bool:
        return self.call_status in ("completed", "canceled", "busy", "no-answer", "failed")


@dataclass
class Number:
    sid: str
    number: str
    voice_url: str = ""
    voice_application_sid: str = ""


class CallRefused(Exception):
    """An incoming call we will not answer, and why (logged without the request body)."""

    def __init__(self, reason: str, *, permission: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.permission = permission


class CallProvider(ABC):
    """One vendor's side of a call."""

    #: The `messaging_channels.platform` this provider serves.
    platform: str = ""

    @abstractmethod
    async def verify_incoming(self, form: dict, *, number: str) -> CallInfo:
        """The call this webhook names, if it is real and for `number`. Raises `CallRefused`."""

    @abstractmethod
    def answer(self, *, ws_url: str, action_url: str, opts: AnswerOptions) -> str:
        """Markup that connects the call to our socket."""

    @abstractmethod
    def parse_end(self, form: dict) -> SessionEnd:
        """The end callback, read."""

    @abstractmethod
    def transfer_markup(self, *, number: str, caller_id: str, action_url: str) -> str:
        """Connect the caller to a person's number; `action_url` hears how it went."""

    @abstractmethod
    def voicemail_markup(self, *, action_url: str, language: str) -> str:
        """Ask for a message and record it; `action_url` gets the recording."""

    @abstractmethod
    def say_and_hang_up(self, text: str, *, language: str = "en-US") -> str:
        """One sentence, then the call ends (a full line, a refused call, a goodbye)."""

    @abstractmethod
    def hang_up_markup(self) -> str:
        """Markup that ends the call."""

    @abstractmethod
    async def hangup(self, call_sid: str) -> None:
        """End a call from our side, through the vendor's API."""

    @abstractmethod
    async def list_numbers(self) -> list[Number]:
        """The account's numbers (also how a key is checked when a channel connects)."""

    @abstractmethod
    async def point_number(self, number_sid: str, *, voice_url: str, status_url: str) -> dict:
        """Send the number's calls to us; returns what it pointed at before, for `restore_number`."""

    @abstractmethod
    async def restore_number(self, number_sid: str, previous: dict) -> None:
        """Put back what `point_number` replaced."""

    # Signed webhooks, where the vendor can sign with a key we hold (Twilio's SharedKeys; Telnyx signs with
    # Ed25519 by default). A provider that cannot leaves these as they are: its numbers run on their tokens.

    async def start_signing(self, prefix: str, *, name: str) -> dict | None:
        """Ask the vendor to sign every webhook under `prefix`; returns what `verify_webhook` and
        `stop_signing` need, or None when this vendor does not sign. Raises `CallRefused` when not allowed."""
        return None

    async def stop_signing(self, made: dict) -> None:
        """Undo `start_signing`. Never raises."""

    def verify_webhook(self, made: dict, *, url: str, params: dict, headers) -> bool:
        """Whether this webhook carries the signature `start_signing` set up."""
        return False

    @abstractmethod
    async def start_recording(self, call_sid: str, *, status_url: str) -> str:
        """Record the call in the customer's own account (V26); returns the recording's id."""


def handoff_of(raw: str | None) -> dict:
    """``HandoffData`` as a dict; anything that is not a JSON object reads as empty."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}

"""A phone number as a channel (V2): the manager starts it like any adapter, so the channel list, status,
enable / disable and assignments treat it as one. A call is a session, not a message, so nothing is received or
sent through here: the voice routes carry the call (`core/routes/voice.py`).
"""
from __future__ import annotations

from core.messaging.base import MessagingAdapter


class VoiceAdapter(MessagingAdapter):

    async def start(self) -> None:
        """Check the key and the number and point the number at this server; a refusal is the channel's error."""
        from core.messaging.voice.connect import connect
        await connect(self.channel)

    async def stop(self) -> None:
        """Nothing held. The number keeps pointing here; disconnect (disable, delete) puts it back."""

    async def _send_raw(self, chat_id: str, text: str) -> None:
        raise RuntimeError("A phone number cannot be sent a message.")

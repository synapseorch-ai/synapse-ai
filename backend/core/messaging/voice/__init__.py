"""The voice channel: a phone number that an agent answers (VOX1a, D238; design in pro MEMORY/19-voice.md).

A call is a session, not a message, so it does not go through `inbound.parse` / `senders.send`. It is still a
channel: a `messaging_channels` document with `platform: "twilio_voice"`, agents joined through
`channel_assignments`, and the default agent answers.

Two layers under one loop:

* ``CallProvider`` (``provider.py``), one per telephony vendor (``twilio.py``): verify an incoming call, answer it
  with markup, read the end callback, transfer / voicemail / hang-up markup, list and point numbers.
* ``TurnTransport`` (``relay.py``): the frames on the call's WebSocket. ``TextRelay`` is ConversationRelay's,
  where the provider does the speech; raw audio comes later behind the same base.
* ``VoiceCall`` (``call.py``), the loop above both: the caller's words, an agent turn, the answer streamed back
  at phrase boundaries; filler, barge-in, silence, the length limit, the voice tools, the transcript.

What differs between a self-hosted install and cloud is behind ``VoiceHost`` (``hooks.py``): admission, how a turn
runs, billing, the bell, secrets, socket-token claims and the shutdown that moves live calls.
"""

#: The channel platform a Twilio number is saved under.
PLATFORM = "twilio_voice"
#: A call's conversation: ``voice_{channel_id}_{CallSid}``.
SESSION_PREFIX = "voice_"


def session_id(channel_id: str, call_sid: str) -> str:
    return f"{SESSION_PREFIX}{channel_id}_{call_sid}"


def mask_number(number: str | None) -> str:
    """A caller's number as application logs show it: ``+1•••••1234`` (V27). Members see it whole."""
    number = str(number or "")
    if len(number) <= 6:
        return number
    return number[:2] + "•" * (len(number) - 6) + number[-4:]

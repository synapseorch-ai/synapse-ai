"""`VoiceCall`: one phone call's loop, above any vendor (V2).

The caller's words come in as frames, each finished utterance becomes an agent turn on the host (in-process,
streamed through U72's hook), and the answer goes back as text at phrase boundaries for the provider to speak.
Around that:

* **Filler (V13, as Naveen tuned it after the first real calls):** a fixed phrase, once a turn, only when a
  real tool starts and nothing has been said yet (the model's own "let me look that up" counts); a second,
  different one if that tool is still running 6 s later. Never on a timer alone, never for `end_call` or
  `transfer_call`: on a slow model it played before nearly every answer.
* **Barge-in (V14):** the caller talking over us cancels the turn (a running tool finishes first) and the answer
  in history is cut to what they heard.
* **Approvals (V16):** a `confirm` tool parks in the bell; the caller hears that someone will follow up. A
  person who answers while the caller is still on the line is heard on the call (F474): the call carries the
  turn on at its next quiet moment, after its own lead-in. One it cannot say goes back to be resumed behind.
* **The voice tools (V17):** `transfer_call` and `end_call` act after the agent's last words.
* **Silence and the length limit (V17):** "Are you still there?" after 8 s, twice, then goodbye; a minute's
  warning to the agent, then a goodbye at the limit.
* **Moving the call (V10):** `move()` ends the session between turns with ``{"reconnect": true}`` so the action
  URL can hand the call to a healthy process; the new socket resumes this conversation.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.messaging.markdown import to_speech
from core.messaging.voice import mask_number, tools as voice_tools
from core.messaging.voice.relay import ErrorFrame, Frame, Interrupt, Prompt, Setup, TurnTransport

log = logging.getLogger("voice.call")

DEFAULT_FILLER = "One moment while I check that."
DEFAULT_FILLER_LATE = "Still working on it."
APPROVAL_LINE = "I've asked someone on the team to approve that. They'll follow up with you."
#: Said before an approval's answer that arrives during the call (F474): the caller asked a while ago.
APPROVED_LINE = "Someone on the team approved that."
REFUSED_LINE = "Someone on the team said no to that."
CANNOT_ASK_LINE = "I can't do that on this call, but someone on the team can follow up with you."
STILL_THERE = "Are you still there?"
SILENCE_GOODBYE = "I haven't heard anything for a while, so I'll end the call here. Goodbye."
LIMIT_GOODBYE = "We've reached the time limit for this call. Thank you for calling. Goodbye."
FAILED_LINE = "Sorry, something went wrong on my side. Could you say that again?"
#: The engine stopped a turn that kept calling one tool the same way (its repeat guard). Its own words are
#: for a chat ("I stopped here because I was repeating myself…"); the caller heard them on VOX1b's first
#: real cloud call.
STUCK_LINE = "Sorry, I couldn't get that done. Could you put it another way?"
MOVE_LINE = "One moment."
DEFAULT_GOODBYE = "Thanks for calling. Goodbye."
#: The close is the call's, not the model's (VOX1d): the first end_call asks this instead of ending, so a "No."
#: to some other question ("anything else about this product?") never hangs up on a caller who isn't done.
CLOSING_QUESTION = "Is there anything else I can help you with?"
#: When the caller has just answered a question the agent itself asked (it asks "anything else?" whatever the
#: prompt says, F529), the close leaves the door open instead of asking again, and waits longer (`invite_grace`).
INVITE_GOODBYE = "Thanks for calling. If you think of anything else, just say so. Goodbye."
#: What ends a question in the languages a call speaks (full-width and Arabic too).
_QUESTION_MARKS = ("?", "\uff1f", "\u061f")
DEFAULT_TRANSFER_LINE = "I'm connecting you to someone on the team now."

#: Added to the agent's prompt on every voice turn (V15), after its own.
VOICE_SUFFIX = (
    "You are on a phone call. The caller hears your words through text-to-speech, so write only what should "
    "be said aloud.\n"
    "- Answer in one to three short sentences. Ask one question at a time.\n"
    "- No markdown, lists, headings, links, code, emoji or symbols. Say numbers, dates, times and amounts the "
    "way a person says them.\n"
    "- Do not write out your reasoning, plans or notes: give the caller only your answer.\n"
    "- Before your first tool call in a reply, start with a few words telling the caller what you are checking "
    "(for example: Let me check that for you.), then make the tool call. Say it once: not again before a second "
    "tool call in the same reply.\n"
    "- Never read a web address, link or code aloud. If your answer is a link, say what it is and that it is "
    "ready for them.\n"
    "- Never ask for, or repeat, card numbers, bank details, passwords or one-time codes.\n"
    "- When the caller seems done, use end_call with your goodbye. The call itself asks whether there is "
    "anything else before it ends, so do not ask that yourself; if they then say no, use end_call again; a "
    "thank-you alone is not goodbye."
)
TRANSFER_SUFFIX = "- If the caller asks for a person, or you cannot help, use transfer_call."
WRAP_UP_SUFFIX = "- The call ends in about a minute. Wrap up now."

#: Tools that start no wait the caller should hear about: the goodbye and the hand-off (our own line is
#: spoken), and the engine's bookkeeping (`update_tasks`, `load_skill`), which a planning agent calls before
#: its real tool — "one moment" played for it on VOX1b's first real cloud call.
def _no_filler() -> frozenset[str]:
    from core.visitor import ENGINE_TOOLS
    return frozenset({voice_tools.END_CALL, voice_tools.TRANSFER_CALL, *ENGINE_TOOLS})


_NO_FILLER = _no_filler()

#: Live calls in this process, by CallSid: what `move_all` walks.
_LIVE: dict[str, "VoiceCall"] = {}


def live_calls() -> list["VoiceCall"]:
    return list(_LIVE.values())


def live_for_session(session_id: str) -> "VoiceCall | None":
    """The call in this process still on the line in `session_id`'s conversation, if any (F474)."""
    return next((c for c in _LIVE.values() if c.session_id == session_id), None)


def move_all() -> int:
    """Ask every live call to move to another process at its next turn boundary (V10). Returns how many."""
    for call in live_calls():
        call.move()
    return len(_LIVE)


@dataclass
class Timing:
    #: After the filler, how long a tool may still run before "Still working on it".
    filler_late_after: float = 6.0
    #: After we send `end`, how long we wait for the provider to close the socket. Twilio says what is still
    #: queued first, and closing ourselves cut the goodbye and read as a dropped socket (64105).
    close_wait: float = 15.0
    silence_after: float = 8.0
    silence_prompts: int = 2
    chars_per_second: float = 15.0
    phrase_words: int = 6
    #: Twilio says what was sent before it ends the session (measured, VOX1a P1: the socket closed ~5 s after
    #: `end` while a sentence played), so nothing waits by default; a provider that cut speech would set this.
    longest_wait_for_speech: float = 0.0
    #: After the agent's goodbye has played, how long the caller may still speak and keep the call (VOX1d: a
    #: "No… just add it to my cart" lost its second half to a goodbye that ended the call at once).
    goodbye_grace: float = 1.5
    #: After the goodbye that invites (the caller has just answered the agent's own question, F529), how long they
    #: may still speak and carry on: the door was left open, so it stays open longer.
    invite_grace: float = 3.0


#: Where a phrase may end: punctuation and the space after it (kept with the phrase, so a sentence that follows
#: a reset does not run on), or a line break.
_BOUNDARY = re.compile(r"[.!?;:,]\s+|\n+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Turn:
    """What one turn has done so far."""

    def __init__(self, text: str):
        self.text = text
        self.started = time.monotonic()
        self.sent_any = False          # any answer text sent (not filler)
        self.since_reset = ""          # answer text sent since the last reset
        self.cycle_open = False        # sent with last=false and not yet closed
        self.filler = 0                # filler phrases said
        self.filler_said: list[str] = []  # their words, noted on the saved answer (the call's log, VOX1c)
        self.in_tool = False
        self.stop_after_tool = False
        self.tools: list[str] = []
        self.final: str | None = None
        self.streamed = False
        self.saved = False             # the engine saved it (a final arrived)
        self.parked = False
        self.late: asyncio.Task | None = None  # "Still working on it", while a tool runs
        self.progress: dict | None = None  # the latest turn-boundary snapshot, for parking
        self.events_since: list[dict] = []  # its calls' events since that snapshot: a cut keeps their results (F477)
        self.narrated = False          # the model's "let me check" was spoken this turn
        self.repeat_stopped = False    # the engine's repeat guard ended it
        self.after_closing = False     # the caller's reply to CLOSING_QUESTION: an end_call now ends the call
        self.after_question = False    # the caller's reply to a question the agent asked (F529)
        self.relayed_action: tuple | None = None  # the call's tool, called by a turn run elsewhere (F531)
        self.answer: dict | None = None  # an approval's answer this turn carries on (F474), not the caller's words
        self.refused_tool = ""         # the refused call, which the resumed turn reports and never runs


class VoiceCall:
    def __init__(self, *, transport: TurnTransport, host: Any, provider: Any, channel: dict, agent: dict,
                 call_sid: str, caller: str, called: str, session_id: str, resume: bool = False,
                 notice: str = "", started_at: float | None = None, timing: Timing | None = None):
        from core import visitor as _visitor

        self.transport = transport
        self.host = host
        self.provider = provider
        self.channel = channel
        self.voice = dict(channel.get("voice") or {})
        self.agent_id = agent.get("id") or ""
        self.agent_name = agent.get("name") or self.agent_id
        self.call_sid = call_sid
        self.caller = caller
        self.called = called
        self.session_id = session_id
        self.resume = resume
        self.notice = notice
        self.timing = timing or Timing()
        # Callers are always visitors (V16): caller ID can be spoofed. A confirm tool parks.
        self.visitor = _visitor.VisitorScope(
            "channel", frozenset(_visitor.clean_tools(channel.get("visitor_tools"))), approvals="park")
        self._closing_asked = False  # CLOSING_QUESTION was the last thing said
        self._agent_asked = False  # the last answer the caller heard ended with a question (F529)
        self._closing_at: float | None = None  # the goodbye was said: the session ends then, unless the caller speaks
        self._closing: dict = {}
        self._said_until = 0.0
        self.pending_action: tuple[str, dict] | None = None
        self.outcome: str | None = None
        self.parked_approval = False
        # Approvals answered while the caller is on the line, waiting for a quiet moment (F474).
        self._answers: list[dict] = []
        # What the call said on its own timer since the last turn, noted on that turn's saved answer (F485).
        self._said_after: list[str] = []
        self._over = False
        self._frames: asyncio.Queue = asyncio.Queue()
        self._turn: _Turn | None = None
        self._turn_task: asyncio.Task | None = None
        self._speaking_until = time.monotonic()
        self._last_heard = time.monotonic()
        self._silence_prompts = 0
        self._ended = False
        self._move = False
        self._wrapped_up = False
        self._limit_at = (started_at or time.monotonic()) + 60 * host.max_minutes(channel)
        self.first_text_ms: list[int] = []
        # The call's own writes to the saved conversation (a barge-in's cut, the filler's note) read, change
        # and write the same message list; one at a time, so neither is lost.
        self._history = asyncio.Lock()

    # ── Outside controls ─────────────────────────────────────────────────────

    def move(self) -> None:
        """End this session at the next turn boundary so the call reconnects elsewhere (V10)."""
        self._move = True
        self._frames.put_nowait(Frame("wake"))

    def hand_answer(self, answer: dict) -> bool:
        """A person answered this call's parked question while the caller is still on the line (F474): the call
        says it at its next quiet moment. `answer` is the door's (`approval_id`, `approved`, `tool_name`,
        `message`, `resume_state`, and whatever its host needs to resume it behind). False once the call is
        over: the door resumes it behind instead."""
        if self._over or self._ended:
            return False
        self._answers.append(answer)
        self._frames.put_nowait(Frame("wake"))
        return True

    async def force_end(self, text: str) -> None:
        """End now with one sentence (a host's last resort: cloud's deploy at 110 s, V10)."""
        await self._stop_turn(wait_for_tool=False)
        await self._say(text, last=True)
        await self._end({"ended": "host"}, outcome="failed")

    # ── The loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        _LIVE[self.call_sid] = self
        if self.resume:
            await self._still_parked()
        try:
            await self.host.call_live(self)
        except Exception as exc:  # noqa: BLE001 — answers then wait for the call's end; the call goes on
            log.warning("could not mark a call live: %s", exc)
        reader = asyncio.create_task(self._read())
        try:
            await self._loop()
            if self._ended and not reader.done():
                # The provider closes once the last words are said; we never close first.
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(reader), self.timing.close_wait)
        finally:
            self._over = True
            reader.cancel()
            await self._stop_turn(wait_for_tool=True)
            _LIVE.pop(self.call_sid, None)
            await self._answers_after_the_call()

    async def _still_parked(self) -> None:
        """A call picked up on a new socket (V10) still has the question its first socket parked: the
        conversation keeps one, and a second ask must not take its place."""
        from core.store import sessions as _sessions
        try:
            waiting = await _sessions.pending_approvals(self.session_id)
        except Exception as exc:  # noqa: BLE001 — at worst a second ask is refused by its door
            log.warning("could not read a moved call's parked question: %s", exc)
            return
        self.parked_approval = any(agent == self.agent_id for agent, _ in waiting)

    async def _answers_after_the_call(self) -> None:
        """The call is over: answers it took but could not say are resumed behind, as an answer after the
        call is, and the bell asks for a call-back."""
        try:
            await self.host.call_over(self)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not mark a call over: %s", exc)
        answers, self._answers = self._answers, []
        for answer in answers:
            try:
                await self.host.resume_after_call(self, answer)
            except Exception as exc:  # noqa: BLE001 — the bell still holds it as answered; the log says why
                log.error("could not resume an answer the call could not say: %s", exc)

    async def _read(self) -> None:
        try:
            while True:
                await self._frames.put(await self.transport.receive())
        except Exception:  # noqa: BLE001 — a closed socket, however it closed
            await self._frames.put(Frame("closed"))

    def _deadline(self) -> float:
        if self._closing_at is not None:
            return self._closing_at
        limit = self._limit_at - (0 if self._wrapped_up else 60)
        if self._turn_task is not None and not self._turn_task.done():
            return limit
        if self._answers:
            return min(self._speaking_until, limit)  # an answer waits for our own words to end
        silence = max(self._speaking_until, self._last_heard) + self.timing.silence_after
        return min(silence, limit)

    def _quiet(self) -> bool:
        """Nothing running, nothing of ours still playing, no goodbye under way: an answer may be said."""
        return (not self._turn_busy() and self._closing_at is None
                and time.monotonic() >= self._speaking_until)

    async def _loop(self) -> None:
        if not self.resume:
            # Twilio says the notice (the welcome greeting) before the caller can speak.
            self._speaking_until = time.monotonic() + len(self.notice) / self.timing.chars_per_second
        while not self._ended:
            if self._move and not self._turn_busy():
                if self._closing_at is not None:
                    await self._end(self._closing, outcome="agent_ended")  # it was ending anyway
                    return
                await self._say(MOVE_LINE, last=True)
                await self._end({"reconnect": True}, outcome="moved")
                return
            if self._answers and self._quiet():
                self._start_answer()
            timeout = max(0.0, self._deadline() - time.monotonic())
            try:
                frame = await asyncio.wait_for(self._frames.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._on_timer()
                continue
            if frame.type == "closed":
                if self._closing_at is not None and not self.outcome:
                    # The caller hung up on the goodbye: the agent still ended the call.
                    self.outcome = "agent_ended"
                    with contextlib.suppress(Exception):
                        await self.host.update_call(self.call_sid, {"outcome": "agent_ended",
                                                                    "handoff": self._closing})
                self.outcome = self.outcome or "caller_hung_up"
                return
            if isinstance(frame, Setup):
                await self._on_setup(frame)
            elif isinstance(frame, Prompt):
                self._last_heard = time.monotonic()
                self._silence_prompts = 0
                if frame.last and frame.text.strip():
                    await self._on_prompt(frame.text)
            elif isinstance(frame, Interrupt):
                self._last_heard = time.monotonic()
                await self._on_interrupt(frame.heard)
            elif isinstance(frame, ErrorFrame):
                log.warning("voice provider error on a call: %s", frame.description[:300])

    def _turn_busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    async def _on_setup(self, frame: Setup) -> None:
        if frame.call_sid and frame.call_sid != self.call_sid:
            log.warning("socket setup named another call; ending it")
            await self._end({"ended": "mismatch"}, outcome="failed")
            return
        if self.voice.get("recording") and not self.resume:
            # V26: in the customer's own Twilio account; we keep only its id.
            try:
                sid = await self.provider.start_recording(self.call_sid, status_url=self.voice.get("status_url", ""))
                await self.host.update_call(self.call_sid, {"recording_sid": sid})
            except Exception as exc:  # noqa: BLE001 — the call goes on unrecorded; the log says why
                log.warning("recording did not start: %s", exc)

    async def _on_timer(self) -> None:
        now = time.monotonic()
        if self._closing_at is not None:
            if now >= self._closing_at:
                await self._end(self._closing, outcome="agent_ended")
            return
        if now >= self._limit_at:
            await self._stop_turn(wait_for_tool=True)
            await self._say(LIMIT_GOODBYE, last=True)
            await self._note_said_after(LIMIT_GOODBYE)
            await self._end({"ended": "limit"}, outcome="limit")
            return
        if now >= self._limit_at - 60 and not self._wrapped_up:
            self._wrapped_up = True  # the next turn's prompt says so
            return
        if self._turn_busy() or self._answers:
            return  # an answer waiting is said next, never talked over by a silence prompt
        if self._closing_asked:
            # Asked whether there was anything else, and nothing came: that is the caller's answer.
            self._closing_asked = False
            await self._say(DEFAULT_GOODBYE, last=True)
            await self._note_said_after(DEFAULT_GOODBYE)
            self._close_after_goodbye({"ended": "agent"})
            return
        if self._silence_prompts < self.timing.silence_prompts:
            self._silence_prompts += 1
            await self._say(STILL_THERE, last=True)
            await self._note_said_after(STILL_THERE)
            return
        await self._say(SILENCE_GOODBYE, last=True)
        await self._note_said_after(SILENCE_GOODBYE)
        await self._end({"ended": "silence"}, outcome="silence")

    # ── A turn ───────────────────────────────────────────────────────────────

    async def _on_prompt(self, text: str) -> None:
        self._keep_the_call()
        # The caller's new words start the next turn at once (V14); a running tool finishes first.
        stopped = self._turn if self._turn_busy() else None
        await self._stop_turn(wait_for_tool=True)
        if self._ended:
            return
        if stopped is not None and stopped.answer is None and not (stopped.sent_any or stopped.saved
                                                                   or stopped.parked or stopped.stop_after_tool):
            # Nothing was answered, so nothing was cut and Twilio sent no interrupt: the caller is still asking,
            # and their first words must not be lost (VOX1d: a question, a silence, "No.", and the call ended).
            if any(name not in _NO_FILLER for name in stopped.tools):
                # It already acted (a tool ran): the question and its tools are kept, as a barge-in keeps them,
                # so the next turn knows what was done and does not do it twice.
                await self._save_cut(stopped, heard="")
            else:
                text = f"{stopped.text} {text}"
        turn = _Turn(text)
        turn.after_closing = self._closing_asked or bool(stopped and stopped.after_closing)
        turn.after_question = self._agent_asked or bool(stopped and stopped.after_question)
        self._agent_asked = False
        if stopped is not None and stopped.answer is None and not stopped.sent_any:
            # The caller is still asking the request the stopped turn heard, and has heard its filler: once a
            # request, not once a turn (F477).
            turn.filler = stopped.filler
        self._closing_asked = False
        self._said_after = []
        self._turn = turn
        self._turn_task = asyncio.create_task(self._run_turn(turn))

    def _start_answer(self) -> None:
        """Carry on the turn an approval stopped, with the person's answer (F474)."""
        answer = self._answers.pop(0)
        # Its one question is answered, so another may park.
        self.parked_approval = False
        # What the caller hears next is the answer, not the closing question.
        self._closing_asked = False
        turn = _Turn(str(answer.get("message") or ""))
        turn.answer = answer
        if not answer.get("approved"):
            turn.refused_tool = str(answer.get("tool_name") or "")
        self._said_after = []
        self._turn = turn
        self._turn_task = asyncio.create_task(self._run_turn(turn))

    def _system(self) -> str:
        lines = [VOICE_SUFFIX]
        if self._can_transfer():
            lines.append(TRANSFER_SUFFIX)
        if self._wrapped_up:
            lines.append(WRAP_UP_SUFFIX)
        return "\n".join(lines)

    def _can_transfer(self) -> bool:
        return bool(str(self.voice.get("transfer_number") or "").strip())

    async def _run_turn(self, turn: _Turn) -> None:
        refusal = await self.host.before_turn(self)
        if refusal:
            if turn.answer is not None:
                self._answers.insert(0, turn.answer)  # resumed behind once the call has ended
            await self._say(refusal, last=True)
            await self._end({"ended": "refused"}, outcome="refused")
            return
        buffer = ""
        extra_tools = voice_tools.build(self, can_transfer=self._can_transfer())
        if turn.answer is not None:
            await self._say(APPROVED_LINE if turn.answer.get("approved") else REFUSED_LINE, last=True)
            events = self.host.resume_turn(self, turn.answer, caller_system=self._system(), extra_tools=extra_tools)
        else:
            events = self.host.run_turn(self, turn.text, caller_system=self._system(), extra_tools=extra_tools)
        try:
            async for event in events:
                kind = event.get("type")
                if kind == "_step_progress":
                    turn.progress = {k: v for k, v in event.items() if k != "type"}
                    turn.events_since.clear()
                elif kind == "answer_delta":
                    buffer += event.get("text") or ""
                    buffer = await self._flush(turn, buffer, final=False)
                elif kind == "answer_reset":
                    if event.get("reason") == "tool_call":
                        # What the model said before its tool is meant for the caller ("Let me check that"),
                        # once a turn: a model searching twice says it before each search, and the caller
                        # heard the same sentence again (VOX1b's real calls). A line that ended in a space
                        # went out as a phrase before the reset; it counts as said.
                        if not turn.narrated:
                            if buffer.strip():
                                await self._flush(turn, buffer, final=True)
                            turn.narrated = bool(turn.since_reset.strip())
                        elif buffer.strip():
                            log.info("voice turn: a second pre-tool line was not spoken")
                    buffer = ""  # anything else not yet said was not the answer
                    turn.since_reset = ""
                elif kind == "tool_execution":
                    turn.in_tool = True
                    turn.events_since.append(event)
                    # A call of the call's own tool, relayed from a turn run elsewhere (F531): acted on as its handler
                    # here would act, once its result says it ran.
                    turn.relayed_action = voice_tools.action_from(event) or turn.relayed_action
                    name = str(event.get("tool_name") or "")
                    turn.tools.append(name)
                    if name == turn.refused_tool:
                        turn.refused_tool = ""  # the refused call, reported by the resume: it does not run
                    elif name not in _NO_FILLER:
                        await self._filler(turn)  # a goodbye, a hand-off or bookkeeping needs no "one moment"
                elif kind == "tool_result":
                    turn.in_tool = False
                    turn.events_since.append(event)
                    if turn.late is not None:
                        turn.late.cancel()
                        turn.late = None
                    if turn.stop_after_tool:
                        break
                    if self.pending_action is None and turn.relayed_action is not None:
                        self.pending_action = turn.relayed_action
                    if self.pending_action is not None:
                        break  # a goodbye or a hand-off ends the turn here: no model call to word it
                elif kind == "repeat_stopped":
                    turn.repeat_stopped = True
                elif kind == "human_input_required":
                    await self._ask_a_person(turn, event)
                elif kind == "final" and not event.get("orch_step_id"):
                    turn.final = str(event.get("response") or "")
                    turn.streamed = bool(event.get("streamed"))
                    turn.saved = True
                elif kind == "error":
                    log.warning("a voice turn failed: %s", str(event.get("message") or "")[:300])
            if turn.stop_after_tool:
                if not turn.parked:  # a parked turn saved itself (_ask_a_person); never a second, empty copy
                    await self._save_cut(turn, heard="")
                return
            if self.pending_action is not None:
                await self._last_words(turn)
                return
            if turn.final is not None and turn.repeat_stopped:
                await self._say(STUCK_LINE, last=True)
                turn.sent_any = True
            elif turn.final is not None:
                if turn.streamed and (turn.since_reset + buffer).strip():
                    await self._flush(turn, buffer, final=True)
                    heard = turn.since_reset
                else:
                    heard = to_speech(turn.final)
                    await self._say(heard, last=True)
                    turn.sent_any = True
                self._agent_asked = heard.rstrip().endswith(_QUESTION_MARKS)
            elif turn.parked:
                pass
            else:
                await self._say(FAILED_LINE, last=True)
            if turn.cycle_open:
                await self._say("", last=True)
            if turn.saved:
                await self._note_turn(turn)
            await self._after_turn()
        except asyncio.CancelledError:
            # Barge-in, or the call ending: the caller hears nothing more from this turn.
            if turn.answer is not None and not (turn.sent_any or turn.saved or turn.parked):
                await self._keep_unsaid_answer(turn)
        except Exception as exc:  # noqa: BLE001 — the caller hears one line; the log has it
            log.error("a voice turn raised: %s", exc)
            with contextlib.suppress(Exception):
                await self._say(FAILED_LINE, last=True)
        finally:
            if turn.late is not None:
                turn.late.cancel()
            with contextlib.suppress(Exception):
                await events.aclose()
            # The loop's next deadline (silence, a move) was set while this turn ran.
            self._frames.put_nowait(Frame("wake"))

    async def _flush(self, turn: _Turn, buffer: str, *, final: bool) -> str:
        """Send whole phrases from `buffer`; returns what is left (all of it sent when `final`)."""
        while True:
            cut = None
            m = _BOUNDARY.search(buffer)
            if m:
                cut = m.end()
            elif len(buffer.split()) > self.timing.phrase_words:
                cut = buffer.rstrip().rfind(" ") + 1 or None
            if not cut:
                break
            phrase, buffer = buffer[:cut], buffer[cut:]
            await self._answer_text(turn, phrase, last=False)
        if final:
            await self._answer_text(turn, buffer, last=True)
            buffer = ""
        return buffer

    async def _answer_text(self, turn: _Turn, text: str, *, last: bool) -> None:
        spoken = to_speech(text)
        if not spoken.strip() and not last:
            return
        if not turn.sent_any and turn.answer is None:
            ms = int((time.monotonic() - turn.started) * 1000)
            self.first_text_ms.append(ms)
            # V12's budget, measured per turn: the caller's last word in, our first words out.
            log.info("voice turn: first words after %d ms", ms)
            with contextlib.suppress(Exception):
                await self.host.update_call(self.call_sid, {"first_words_ms": [ms]})
        turn.sent_any = turn.sent_any or bool(spoken.strip())
        turn.since_reset += spoken
        await self._say(spoken, last=last)
        turn.cycle_open = not last

    async def _filler(self, turn: _Turn) -> None:
        """A tool started. Say the filler once a turn, unless the caller has already heard something."""
        if turn.sent_any or turn.parked:
            return
        if not turn.filler:
            turn.filler = 1
            await self._say_filler(turn, str(self.voice.get("filler") or DEFAULT_FILLER))
        if turn.filler == 1 and turn.late is None:
            turn.late = asyncio.create_task(self._late_filler(turn))

    async def _late_filler(self, turn: _Turn) -> None:
        await asyncio.sleep(self.timing.filler_late_after)
        if turn.in_tool and not turn.sent_any and not turn.parked and turn.filler == 1:
            turn.filler = 2
            await self._say_filler(turn, str(self.voice.get("filler_late") or DEFAULT_FILLER_LATE))

    async def _say_filler(self, turn: _Turn, line: str) -> None:
        turn.filler_said.append(line)
        await self._say(line, last=True)

    @staticmethod
    def _turn_notes(turn: _Turn) -> dict:
        """What the call notes on a turn's saved answer: the filler it said (VOX1c) and the approval it answers
        (F474)."""
        fields: dict = {}
        if turn.filler_said:
            fields["filler"] = list(turn.filler_said)
        if turn.answer is not None:
            # Not `approval`: that key is a question nobody answered (F247), drawn as its card.
            fields["answered_approval"] = {"id": turn.answer.get("approval_id"),
                                           "approved": bool(turn.answer.get("approved"))}
        return fields

    async def _note_turn(self, turn: _Turn) -> None:
        """On the turn's saved answer, the filler it said (VOX1c) and the approval it answers (F474): the call's
        log shows the one, and reads the other as the approval's answer, not the caller asking again. Never
        fails a turn."""
        fields = self._turn_notes(turn)
        if not fields:
            return
        # A caller who hangs up as the answer ends stops the turn here; the note is finished either way.
        await asyncio.shield(self._mark_last_answer(turn.text, fields))

    async def _note_said_after(self, line: str) -> None:
        """A line the call said on its own timer, noted on the last turn's saved answer (F485): the log ends where
        the call did, not at the closing question. Nothing before the first turn."""
        if self._turn is None:
            return
        self._said_after.append(line)
        await asyncio.shield(self._mark_last_answer(self._turn.text, {"said_after": list(self._said_after)}))

    async def _mark_last_answer(self, question: str, fields: dict) -> None:
        from core.store import sessions as _sessions
        async with self._history:
            with contextlib.suppress(Exception):
                await _sessions.mark_last_answer(self.session_id, self.agent_id, question=question, **fields)

    async def _keep_unsaid_answer(self, turn: _Turn) -> None:
        """An answer's turn stopped before the caller heard any of it. One that already acted is kept, as any
        turn that acted is; one stopped before it began is said again at the next quiet moment, or resumed
        behind when the call is over. One the host's own job carried on is that job's (F486): never twice."""
        if await self._host_keeps(turn, heard=""):
            return
        if any(name not in _NO_FILLER for name in turn.tools):
            await self._save_cut(turn, heard="")
        else:
            self._answers.insert(0, turn.answer)

    async def _host_keeps(self, turn: _Turn, *, heard: str) -> bool:
        """Whether the host's own job keeps this cut turn (F486: a workflow agent's, on the fleet), and is handed
        it. Never fails a turn: a host that cannot say leaves the cut to the call."""
        try:
            return bool(await self.host.cut_turn(self, heard=heard, fields=self._turn_notes(turn)))
        except Exception as exc:  # noqa: BLE001
            log.warning("a voice host could not take a cut turn: %s", exc)
            return False

    async def _ask_a_person(self, turn: _Turn, event: dict) -> None:
        """V16: a confirm tool parks in the bell; the call carries on without it."""
        turn.parked = True
        if event.get("kind") == "tool_approval" and not self.parked_approval:
            try:
                approval_id = await self.host.park(self, event=event, message=turn.text, progress=turn.progress,
                                                   caller_system=self._system())
                self.parked_approval = True
                await self.host.update_call(self.call_sid, {"approvals": [approval_id]})
                line = APPROVAL_LINE
            except Exception as exc:  # noqa: BLE001
                log.error("could not park a call's approval: %s", exc)
                line = CANNOT_ASK_LINE
        else:
            # One question per call waits in the bell; anything else is followed up by a person.
            line = CANNOT_ASK_LINE
        await self._say(line, last=True)
        turn.sent_any = True
        from core.session import _save_conversation_turn
        with contextlib.suppress(Exception):
            await _save_conversation_turn(self.session_id, self.agent_id, turn.text, line, tools=turn.tools)
        await self._note_turn(turn)

    async def _last_words(self, turn: _Turn) -> None:
        """The goodbye (the tool's argument) or the hand-off line, said by us; then the action."""
        kind, data = self.pending_action
        inviting = kind == "end" and not turn.after_closing and turn.after_question
        if kind == "end" and not turn.after_closing and not inviting:
            await self._ask_before_closing(turn)
            return
        if kind == "transfer":
            line = str(self.voice.get("transfer_line") or DEFAULT_TRANSFER_LINE)
        elif inviting:
            # The caller has just said no to the agent's own question: not asked again (F529), but told the door is
            # open, and given longer to walk back through it (VOX1d's narrow "anything else about this product?").
            line = INVITE_GOODBYE
            self.pending_action = (kind, {**data, "invite": True})
        else:
            line = data.get("goodbye") or DEFAULT_GOODBYE
        if turn.cycle_open or not turn.sent_any:
            # Unless the model already said it before the tool ("Thanks for calling, goodbye!").
            await self._say(to_speech(line) if not turn.sent_any else "", last=True)
        from core.session import _save_conversation_turn
        with contextlib.suppress(Exception):
            await _save_conversation_turn(self.session_id, self.agent_id, turn.text,
                                          line if not turn.sent_any else turn.since_reset.strip() or line,
                                          tools=turn.tools)
        await self._note_turn(turn)
        await self._after_turn()

    async def _ask_before_closing(self, turn: _Turn) -> None:
        """The first end_call asks CLOSING_QUESTION and keeps the call (VOX1d): the caller's reply decides."""
        self.pending_action = None
        self._closing_asked = True
        said = turn.since_reset.strip() if turn.sent_any else ""
        await self._say(CLOSING_QUESTION, last=True)
        turn.sent_any = True
        from core.session import _save_conversation_turn
        async with self._history:
            with contextlib.suppress(Exception):
                await _save_conversation_turn(self.session_id, self.agent_id, turn.text,
                                              f"{said} {CLOSING_QUESTION}".strip(), tools=turn.tools)

    async def _after_turn(self) -> None:
        action, self.pending_action = self.pending_action, None
        if action is None:
            return
        kind, data = action
        if kind == "transfer":
            await self._end({"transfer": data.get("reason") or ""}, outcome="transferred")
        else:
            self._close_after_goodbye({"ended": "agent"},
                                      grace=self.timing.invite_grace if data.get("invite") else None)

    def _close_after_goodbye(self, handoff: dict, *, grace: float | None = None) -> None:
        """The goodbye plays, then a moment in which the caller may still speak (VOX1d). Words in it keep the call;
        silence ends it (the loop's timer). A hand-off is never held: it ends at once."""
        self._closing = handoff
        wait = self.timing.goodbye_grace if grace is None else grace
        self._closing_at = max(self._said_until, time.monotonic()) + wait
        self._frames.put_nowait(Frame("wake"))

    def _keep_the_call(self) -> None:
        if self._closing_at is not None:
            log.info("voice call: the caller spoke after the goodbye; the call goes on")
            self._closing_at, self._closing = None, {}

    # ── Barge-in ─────────────────────────────────────────────────────────────

    async def _on_interrupt(self, heard: str) -> None:
        self._keep_the_call()
        turn = self._turn
        if turn is None:
            return
        if self._turn_busy() and not turn.saved:
            await self._stop_turn(wait_for_tool=True)
            if not (turn.stop_after_tool or turn.parked):
                if turn.answer is None or turn.sent_any:  # an answer not yet begun is kept as it stopped
                    await self._save_cut(turn, heard=heard)
                return
            if not turn.parked:
                return
            # A parked turn saved its line itself; cut that, as below.
        if not (turn.saved or turn.parked):
            return  # nothing of this turn's is the last answer
        # The engine saved the whole answer; the caller heard only part of it (V14).
        from core.store import sessions as _sessions
        async with self._history:
            with contextlib.suppress(Exception):
                await _sessions.cut_last_answer(self.session_id, self.agent_id, heard)

    async def _stop_turn(self, *, wait_for_tool: bool) -> None:
        task, turn = self._turn_task, self._turn
        if task is None or task.done():
            return
        if wait_for_tool and turn is not None and turn.in_tool:
            turn.stop_after_tool = True  # the tool may have side effects: it finishes, unspoken
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _save_cut(self, turn: _Turn, *, heard: str) -> None:
        """A turn stopped before the engine saved it: keep the question, what was heard, and its tools.

        What was heard is what was sent, up to the end of the clause the caller was hearing. Its finished calls
        are kept with their results, as a chat's Stop keeps them: kept by name alone, the next turn's model saw
        no result and ran the tool again (F477).
        """
        if await self._host_keeps(turn, heard=heard):
            return
        from core.session import _save_conversation_turn, turn_calls
        from core.store import sessions as _sessions
        said = _sessions.cut_at_heard(turn.since_reset, heard) if heard.strip() else ""
        resumed = (turn.answer or {}).get("resume_state") if turn.answer is not None else None
        calls = turn_calls(turn.progress, turn.events_since, resumed=resumed)
        async with self._history:
            with contextlib.suppress(Exception):
                await _save_conversation_turn(self.session_id, self.agent_id, turn.text, said.strip(),
                                              tools=turn.tools, calls=calls or None)
                await _sessions.cut_last_answer(self.session_id, self.agent_id, "")
        await self._note_turn(turn)

    # ── Speaking and ending ──────────────────────────────────────────────────

    async def _say(self, text: str, *, last: bool) -> None:
        if self._ended:
            return
        now = time.monotonic()
        self._speaking_until = max(now, self._speaking_until) + len(text) / self.timing.chars_per_second
        # When these words alone are heard: a goodbye follows the caller's words, so nothing plays before it.
        self._said_until = now + len(text) / self.timing.chars_per_second
        try:
            await self.transport.say(text, last=last)
        except Exception as exc:  # noqa: BLE001 — the socket went away; the reader sees it close
            log.info("could not speak on a call: %s", exc)

    async def _end(self, handoff: dict, *, outcome: str) -> None:
        if self._ended:
            return
        # Let the last words be heard before the session ends.
        wait = min(self._speaking_until - time.monotonic(), self.timing.longest_wait_for_speech)
        if wait > 0:
            await asyncio.sleep(wait)
        self.outcome = outcome
        self._ended = True
        await self.host.update_call(self.call_sid, {"outcome": outcome, "handoff": handoff})
        log.info("call from %s ended: %s", mask_number(self.caller), outcome)
        with contextlib.suppress(Exception):
            await self.transport.end(handoff)
        self._frames.put_nowait(Frame("wake"))


def new_record(*, call_sid: str, channel_id: str, agent_id: str, session_id: str, caller: str,
               called: str) -> dict:
    """A call's record (V31's recent calls; VOX1b settles from its segments)."""
    return {"id": call_sid, "channel_id": channel_id, "agent_id": agent_id, "session_id": session_id,
            "caller": caller, "called": called, "started_at": _now_iso(), "ended_at": None,
            "segments": [], "seconds": 0, "outcome": None, "handoff": None, "transfer": None,
            "voicemail": None, "recording_sid": None, "approvals": [], "error": None}

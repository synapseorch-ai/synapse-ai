"""
A phone call's loop (`VoiceCall`, VOX1a), driven by a fake ConversationRelay
socket and a host whose turns are scripted events, with the timings shrunk.

What the caller hears: the answer in phrases (V12), filler while tools run
(V13), barge-in (V14), the parked approval's line (V16), the voice tools,
silence and the length limit (V17), and a move between turns (V10).
"""
import asyncio
import time

import pytest

from core.messaging.voice import call as call_mod
from core.messaging.voice.call import Timing, VoiceCall
from core.messaging.voice.hooks import VoiceHost
from core.messaging.voice.relay import Frame, Interrupt, Prompt, Setup

CALL = "CA" + "3" * 32
FAST = Timing(filler_late_after=0.3, silence_after=0.3, chars_per_second=1e9, longest_wait_for_speech=0,
              close_wait=0.5, goodbye_grace=0.05)


class FakeSocket:
    """ConversationRelay as seen by `VoiceCall`: frames in, what we said out."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.said: list[tuple[str, bool]] = []
        self.ended: list[dict | None] = []

    async def receive(self):
        frame = await self.inbox.get()
        if frame is None:
            raise ConnectionError("closed")
        return frame

    async def say(self, text, *, last, interruptible=None):
        self.said.append((text, last))

    async def end(self, handoff=None):
        self.ended.append(handoff)
        await self.inbox.put(None)

    def words(self) -> str:
        return "".join(t for t, _ in self.said)


class ScriptedHost(VoiceHost):
    """Each turn plays the next script: a list of events, `("wait", event)` or `("tool", name)`."""

    def __init__(self, *scripts, minutes=30, refuse=None):
        super().__init__()
        self.scripts = list(scripts)
        self.minutes = minutes
        self.refuse = refuse
        self.records: dict[str, dict] = {}
        self.turns: list[dict] = []
        self.resumed: list[dict] = []     # answers the live call carried on (F474)
        self.after_call: list[dict] = []  # answers it handed back to be resumed behind

    def max_minutes(self, channel):
        return self.minutes

    async def before_turn(self, call):
        return self.refuse

    async def run_turn(self, call, text, *, caller_system, extra_tools):
        self.turns.append({"text": text, "system": caller_system, "tools": [t.name for t, _ in extra_tools]})
        handlers = {t.name: h for t, h in extra_tools}
        for step in self.scripts.pop(0) if self.scripts else []:
            if isinstance(step, tuple) and step[0] == "wait":
                await step[1].wait()
            elif isinstance(step, tuple) and step[0] == "tool":
                await handlers[step[1]](step[2] if len(step) > 2 else {})
            else:
                yield step

    async def resume_turn(self, call, answer, *, caller_system, extra_tools):
        self.resumed.append(answer)
        async for event in self.run_turn(call, answer.get("message") or "", caller_system=caller_system,
                                         extra_tools=extra_tools):
            yield event

    async def resume_after_call(self, call, answer):
        self.after_call.append(answer)

    async def update_call(self, call_sid, changes):
        rec = self.records.setdefault(call_sid, {})
        for k, v in changes.items():
            rec[k] = (rec.get(k) or []) + list(v) if k in ("approvals", "segments") else v
        return rec


@pytest.fixture
def saved(monkeypatch):
    """The conversation writes `VoiceCall` makes itself (a stopped turn, a parked one)."""
    out = {"turns": [], "cuts": [], "marks": [], "calls": []}

    async def save_turn(session_id, agent_id, user, assistant, tools=None, **kw):
        out["turns"].append((user, assistant, list(tools or [])))
        out["calls"].append(kw.get("calls"))

    async def cut(session_id, agent_id, heard):
        out["cuts"].append(heard)
        return True

    async def mark(session_id, agent_id, *, question, **fields):
        out["marks"].append((question, fields))
        return True

    import core.session as session_mod
    from core.store import sessions as sessions_mod
    monkeypatch.setattr(session_mod, "_save_conversation_turn", save_turn)
    monkeypatch.setattr(sessions_mod, "cut_last_answer", cut)
    monkeypatch.setattr(sessions_mod, "mark_last_answer", mark)
    return out


def _call(host, socket, *, voice=None, started_at=None, timing=FAST, resume=False):
    channel = {"id": "chn_1", "platform": "twilio_voice", "visitor_tools": [], "voice": voice or {}}
    return VoiceCall(transport=socket, host=host, provider=None, channel=channel, agent={"id": "a1", "name": "Ada"},
                     call_sid=CALL, caller="+14155550100", called="+15551230000",
                     session_id=f"voice_chn_1_{CALL}", timing=timing, started_at=started_at, resume=resume)


async def _until(happened, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if happened():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never happened")


def _delta(text):
    return {"type": "answer_delta", "text": text, "turn": 1}


def _final(text, streamed=True):
    return {"type": "final", "response": text, "streamed": streamed}


async def _drive(call, socket, *frames, settle=0.15, hang_up=True):
    task = asyncio.create_task(call.run())
    for frame in frames:
        if isinstance(frame, (int, float)):
            await asyncio.sleep(frame)
        else:
            await socket.inbox.put(frame)
    await asyncio.sleep(settle)
    if hang_up and not socket.ended:
        await socket.inbox.put(None)
    await asyncio.wait_for(task, 5)


def _prompt(text):
    return Prompt("prompt", text=text, last=True)


class TestTheAnswer:
    async def test_it_goes_out_in_phrases_and_the_last_one_closes_the_cycle(self, saved):
        host = ScriptedHost([_delta("It's sunny"), _delta(" today, 21 degrees."), _delta(" Anything else"),
                             _delta("?"), _final("It's sunny today, 21 degrees. Anything else?")])
        socket = FakeSocket()
        call = _call(host, socket)
        await _drive(call, socket, Setup("setup", call_sid=CALL), _prompt("weather?"))
        assert socket.words() == "It's sunny today, 21 degrees. Anything else?"
        assert socket.said[0] == ("It's sunny today, ", False), "a phrase goes as soon as it ends, with its space"
        assert socket.said[-1][1] is True and all(not last for _, last in socket.said[:-1])
        assert len(call.first_text_ms) == 1
        assert host.turns[0]["text"] == "weather?" and "phone call" in host.turns[0]["system"]

    async def test_markdown_is_never_read_out(self, saved):
        host = ScriptedHost([_delta("**Yes**, see [the docs](https://x.y)."), _final("…")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert "*" not in socket.words() and "Yes, see the docs." in socket.words()

    async def test_an_answer_that_did_not_stream_is_said_whole(self, saved):
        host = ScriptedHost([_final("# Done\nAll **set**.", streamed=False)])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert socket.said == [("Done All set.", True)]

    async def test_a_reset_drops_what_was_not_yet_said(self, saved):
        host = ScriptedHost([_delta("Let me look"), {"type": "answer_reset", "turn": 1, "reason": "truncated"},
                             _delta("Found it."), _final("Found it.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert "Let me look" not in socket.words()
        assert call_mod.DEFAULT_FILLER not in socket.words(), "a reset alone is not a tool"
        assert socket.words().endswith("Found it.")

    async def test_a_turn_with_no_answer_says_sorry(self, saved):
        host = ScriptedHost([{"type": "error", "message": "boom"}])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert socket.said[-1] == (call_mod.FAILED_LINE, True)


class TestFiller:
    async def test_said_once_when_a_tool_runs(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "lookup"}, {"type": "tool_result"},
                             {"type": "tool_execution", "tool_name": "lookup"}, {"type": "tool_result"},
                             _delta("Done."), _final("Done.")])
        socket = FakeSocket()
        await _drive(_call(host, socket, voice={"filler": "Hold on."}), socket, _prompt("q"))
        assert [t for t, _ in socket.said].count("Hold on.") == 1

    async def test_a_slow_model_alone_brings_no_filler(self, saved):
        """Naveen, on the first real calls: "one moment" before every answer was annoying. Only a tool earns it."""
        gate = asyncio.Event()
        host = ScriptedHost([("wait", gate), _delta("Here."), _final("Here.")])
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.sleep(0.5)
        assert socket.said == []
        gate.set()
        await asyncio.sleep(0.05)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert socket.said == [("Here.", True)]

    async def test_a_long_tool_gets_the_second_phrase_once(self, saved):
        done = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "search"}, ("wait", done),
                             {"type": "tool_result", "tool_name": "search"}, _delta("Found."), _final("Found.")])
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.sleep(0.1)
        assert [t for t, _ in socket.said] == [call_mod.DEFAULT_FILLER]
        await asyncio.sleep(0.35)
        assert [t for t, _ in socket.said] == [call_mod.DEFAULT_FILLER, call_mod.DEFAULT_FILLER_LATE]
        await asyncio.sleep(0.4)
        assert len(socket.said) == 2, "never a third"
        done.set()
        await asyncio.sleep(0.05)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert socket.words().endswith("Found.")

    async def test_what_the_model_says_before_its_tool_replaces_the_filler(self, saved):
        host = ScriptedHost([_delta("Let me look that up. "), {"type": "answer_reset", "turn": 1, "reason": "tool_call"},
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("Sunny."), _final("Sunny.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("weather"))
        assert call_mod.DEFAULT_FILLER not in socket.words()
        assert socket.words() == "Let me look that up. Sunny."

    @pytest.mark.parametrize("bookkeeping", ["update_tasks", "load_skill"])
    async def test_no_filler_for_the_engines_bookkeeping(self, saved, bookkeeping):
        """A planning agent calls update_tasks before its real tool; "one moment" played for it on
        VOX1b's first real cloud call, with nothing for the caller to wait for."""
        host = ScriptedHost([{"type": "tool_execution", "tool_name": bookkeeping}, {"type": "tool_result"},
                             _delta("Sure."), _final("Sure.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("hi"))
        assert call_mod.DEFAULT_FILLER not in socket.words() and socket.words() == "Sure."

    async def test_the_real_tool_after_bookkeeping_still_gets_its_filler(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "update_tasks"}, {"type": "tool_result"},
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("Found."), _final("Found.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("find"))
        assert socket.words() == call_mod.DEFAULT_FILLER + "Found."

    async def test_the_engines_repeat_guard_is_not_read_to_the_caller(self, saved):
        """VOX1b's first real cloud call: the caller heard "I stopped here because I was repeating
        myself: I called update_tasks…", the engine's sentence for a chat."""
        engine_words = ("I stopped here because I was repeating myself: I called search with the same "
                        "arguments 3 times in a row and got the same result each time.")
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             {"type": "repeat_stopped", "tool_name": "search", "count": 3, "turn": 3},
                             _final(engine_words, streamed=False)])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("find it"))
        assert "repeating myself" not in socket.words()
        assert socket.words().endswith(call_mod.STUCK_LINE)

    async def test_the_pre_tool_line_is_said_once_a_turn(self, saved):
        """A model that searches twice says "let me check" before each search; the caller
        heard the same sentence again on VOX1b's first real cloud calls."""
        reset = {"type": "answer_reset", "turn": 1, "reason": "tool_call"}
        host = ScriptedHost([_delta("Let me check that for you."), reset,
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("Let me check that for you."), reset,
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("We have two phones."), _final("We have two phones.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("phones?"))
        assert socket.words() == "Let me check that for you.We have two phones."

    async def test_a_pre_tool_line_already_spoken_as_a_phrase_counts(self, saved):
        reset = {"type": "answer_reset", "turn": 1, "reason": "tool_call"}
        host = ScriptedHost([_delta("Sure, let me look. "), reset,
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("One more look"), reset,
                             {"type": "tool_execution", "tool_name": "search"}, {"type": "tool_result"},
                             _delta("Found it."), _final("Found it.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("find it"))
        assert "One more look" not in socket.words() and socket.words().endswith("Found it.")

    async def test_each_turn_may_say_it_again(self, saved):
        reset = {"type": "answer_reset", "turn": 1, "reason": "tool_call"}
        turn = [_delta("Let me check."), reset, {"type": "tool_execution", "tool_name": "search"},
                {"type": "tool_result"}, _delta("Done."), _final("Done.")]
        host = ScriptedHost(list(turn), list(turn))
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("one"), 0.2, _prompt("two"))
        assert socket.words().count("Let me check.") == 2

    async def test_no_filler_once_the_answer_has_started(self, saved):
        gate = asyncio.Event()
        host = ScriptedHost([_delta("Sure, "), ("wait", gate), _final("Sure, ok")])
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.sleep(0.4)
        gate.set()
        await asyncio.sleep(0.05)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert call_mod.DEFAULT_FILLER not in [t for t, _ in socket.said]


class TestTheFillerIsKept:
    """The call's log shows what the caller heard while a tool ran (VOX1c): the saved answer carries it."""

    async def test_the_filler_said_is_noted_on_the_saved_answer(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "lookup"}, {"type": "tool_result"},
                             _delta("Done."), _final("Done.")])
        socket = FakeSocket()
        await _drive(_call(host, socket, voice={"filler": "Hold on."}), socket, _prompt("q"))
        assert saved["marks"] == [("q", {"filler": ["Hold on."]})]

    async def test_both_phrases_of_a_long_tool_are_noted(self, saved):
        done = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "search"}, ("wait", done),
                             {"type": "tool_result", "tool_name": "search"}, _delta("Found."), _final("Found.")])
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.sleep(0.5)
        done.set()
        await asyncio.sleep(0.1)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert saved["marks"] == [("q", {"filler": [call_mod.DEFAULT_FILLER, call_mod.DEFAULT_FILLER_LATE]})]

    async def test_a_turn_without_filler_notes_nothing(self, saved):
        host = ScriptedHost([_delta("Hi."), _final("Hi.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert saved["marks"] == []

    async def test_a_turn_that_failed_notes_nothing(self, saved):
        """It saved nothing, so the last answer is an earlier question's."""
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "lookup"}, {"type": "tool_result"}])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert call_mod.FAILED_LINE in socket.words() and saved["marks"] == []

    async def test_a_turn_cut_while_its_tool_ran_notes_it_after_its_save(self, saved):
        tool_done = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "book"}, ("wait", tool_done),
                             {"type": "tool_result", "tool_name": "book"}, _final("Booked.")])
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("book it"))
        await asyncio.sleep(0.05)
        await socket.inbox.put(Interrupt("interrupt", heard=""))
        await asyncio.sleep(0.05)
        tool_done.set()
        await asyncio.sleep(0.1)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert saved["turns"] == [("book it", "", ["book"])]
        assert saved["marks"] == [("book it", {"filler": [call_mod.DEFAULT_FILLER]})]


class TestBargeIn:
    async def test_talking_over_the_answer_stops_it_and_history_keeps_what_was_heard(self, saved):
        never = asyncio.Event()
        host = ScriptedHost([_delta("It is sunny, "), ("wait", never), _final("never")],
                            [_delta("Okay."), _final("Okay.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("weather?"), 0.1,
                     Interrupt("interrupt", heard="It is"), _prompt("never mind"), settle=0.2)
        assert saved["turns"][0] == ("weather?", "It is", []), "what was sent, cut at the clause heard"
        assert saved["cuts"] == [""], "then only marked interrupted"
        assert [t["text"] for t in host.turns] == ["weather?", "never mind"]
        assert "never" not in socket.words()

    async def test_a_caller_who_speaks_again_before_any_answer_keeps_the_first_question(self, saved):
        """VOX1d, on a live call: a question, ten silent seconds (the org's tool servers starting), then "No.".
        The first turn was stopped before it said a word, and Twilio sends no interrupt when nothing is playing,
        so the question was lost: the agent heard only "No." and ended the call."""
        never = asyncio.Event()
        host = ScriptedHost([("wait", never), _final("never")], [_delta("We have two."), _final("We have two.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("Is there any phones available?"), 0.05, _prompt("No."),
                     settle=0.2)
        assert [t["text"] for t in host.turns] == ["Is there any phones available?",
                                                   "Is there any phones available? No."]
        assert "We have two." in socket.words() and saved["turns"] == []

    async def test_a_turn_that_already_acted_keeps_its_question_and_tools_instead(self, saved):
        """A tool ran, then the model was still wording the answer: joining the words could make it act twice."""
        never = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "book"},
                             {"type": "tool_result", "tool_name": "book"}, ("wait", never), _final("never")],
                            [_delta("Done."), _final("Done.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("book it"), 0.05, _prompt("hello?"), settle=0.2)
        assert saved["turns"] == [("book it", "", ["book"])]
        assert [t["text"] for t in host.turns] == ["book it", "hello?"]

    async def test_bookkeeping_alone_is_not_acting(self, saved):
        never = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "update_tasks"},
                             {"type": "tool_result", "tool_name": "update_tasks"}, ("wait", never), _final("never")],
                            [_delta("Two."), _final("Two.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("phones?"), 0.05, _prompt("hello?"), settle=0.2)
        assert [t["text"] for t in host.turns] == ["phones?", "phones? hello?"] and saved["turns"] == []

    async def test_words_after_an_answer_began_are_a_turn_of_their_own(self, saved):
        never = asyncio.Event()
        host = ScriptedHost([_delta("It is sunny, "), ("wait", never), _final("never")],
                            [_delta("Okay."), _final("Okay.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("weather?"), 0.1, _prompt("never mind"), settle=0.2)
        assert [t["text"] for t in host.turns] == ["weather?", "never mind"]

    async def test_a_running_tool_finishes_but_its_answer_is_not_spoken(self, saved):
        tool_done = asyncio.Event()
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "book"}, ("wait", tool_done),
                             {"type": "tool_result", "tool_name": "book"}, _delta("Booked for noon."),
                             _final("Booked for noon.")])
        socket = FakeSocket()
        call = _call(host, socket)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("book it"))
        await asyncio.sleep(0.05)
        await socket.inbox.put(Interrupt("interrupt", heard=""))
        await asyncio.sleep(0.1)
        assert not call._turn_task.done(), "the tool was not cancelled"
        tool_done.set()
        await asyncio.sleep(0.1)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert "Booked" not in socket.words()
        assert saved["turns"] == [("book it", "", ["book"])]

    async def test_after_the_answer_was_saved_the_saved_answer_is_cut(self, saved):
        host = ScriptedHost([_delta("A long answer, with more."), _final("A long answer, with more.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"), 0.1, Interrupt("interrupt", heard="A long answer"))
        assert saved["cuts"] == ["A long answer"] and saved["turns"] == []


class TestACutTurnKeepsWhatItDid:
    """F477 (VOX1e, D263, call CA0b8498…): speech-to-text split "Can you check the prices of phone case, a
    charger, and a watch? | And tell me which is cheapest?". The first turn searched "phone case" and said the
    filler; the continuation stopped it after the tool and kept the tool's name only, so the next turn searched
    "phone case" again and said "One moment…" again. A cut turn keeps its finished calls' results, as a barge-in
    and a chat's Stop keep them; the filler is said once a request, not once a turn."""

    SEARCH = {"type": "tool_execution", "tool_name": "shop_search_products", "args": {"query": "phone case"}}
    FOUND = {"type": "tool_result", "tool_name": "shop_search_products", "preview": '{"products": []}'}

    async def _split(self, host, socket):
        gate = asyncio.Event()
        host.scripts[0] = [self.SEARCH, ("wait", gate), self.FOUND, _delta("never said"), _final("never said")]
        call = _call(host, socket)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("Check the prices of a phone case, a charger and a watch?"))
        await _until(lambda: call_mod.DEFAULT_FILLER in socket.words())
        await socket.inbox.put(_prompt("And tell me which is cheapest?"))
        await asyncio.sleep(0.05)
        gate.set()
        return call, task

    async def test_its_finished_calls_are_kept_with_their_results(self, saved):
        host = ScriptedHost([], [_final("The charger is cheapest.")])
        socket = FakeSocket()
        _, task = await self._split(host, socket)
        await _until(lambda: "cheapest" in socket.words())
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert saved["turns"][0] == ("Check the prices of a phone case, a charger and a watch?", "",
                                     ["shop_search_products"])
        assert saved["calls"][0] == [{"tool": "shop_search_products", "args": {"query": "phone case"},
                                      "result": '{"products": []}'}]

    async def test_the_filler_is_said_once_for_the_request(self, saved):
        again = [{"type": "tool_execution", "tool_name": "shop_search_products", "args": {"query": "charger"}},
                 {"type": "tool_result", "tool_name": "shop_search_products", "preview": "[]"},
                 _final("The charger is cheapest.")]
        host = ScriptedHost([], again)
        socket = FakeSocket()
        _, task = await self._split(host, socket)
        await _until(lambda: "cheapest" in socket.words())
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert socket.words().count(call_mod.DEFAULT_FILLER) == 1, socket.words()
        assert saved["marks"][0] == ("Check the prices of a phone case, a charger and a watch?",
                                     {"filler": [call_mod.DEFAULT_FILLER]}), "the log shows it once, where it was said"

    async def test_a_turn_its_hosts_job_keeps_is_left_to_the_host(self, saved):
        """F486 (VOX1f, call 7): a workflow agent's turn runs on the fleet, whose job saves it whatever the call does
        (V14). The call saved a cut copy too, so a cut turn was in the conversation twice. A host that keeps its own
        turns is handed the cut (what was heard, and the turn's notes) and the call saves nothing."""
        class KeepsItsTurns(ScriptedHost):
            def __init__(self, *scripts):
                super().__init__(*scripts)
                self.cut: list = []

            async def cut_turn(self, call, *, heard, fields):
                self.cut.append((heard, fields))
                return True

        host = KeepsItsTurns([], [_final("The charger is cheapest.")])
        socket = FakeSocket()
        _, task = await self._split(host, socket)
        await _until(lambda: "cheapest" in socket.words())
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert all(t[0] != "Check the prices of a phone case, a charger and a watch?" for t in saved["turns"])
        assert host.cut == [("", {"filler": [call_mod.DEFAULT_FILLER]})]

    async def test_an_unsaid_answer_its_hosts_job_carries_is_not_said_again(self, saved):
        """F486: an approval's answer cut before the caller heard any of it is said again at the next quiet moment
        (F474) — unless its host's job already carried it on, which would answer the approval twice."""
        class KeepsItsTurns(ScriptedHost):
            async def cut_turn(self, call, *, heard, fields):
                return True

        call = _call(KeepsItsTurns(), FakeSocket())
        turn = call_mod._Turn("weather?")
        turn.answer = {"approval_id": "ap_1", "approved": True, "message": "weather?"}
        await call._keep_unsaid_answer(turn)
        assert call._answers == []

    async def test_a_new_request_after_an_answer_gets_its_own_filler(self, saved):
        """Once a request: a request the caller makes after hearing an answer is a new one."""
        tool = [{"type": "tool_execution", "tool_name": "shop_search_products", "args": {"query": "x"}},
                {"type": "tool_result", "tool_name": "shop_search_products", "preview": "[]"}]
        host = ScriptedHost(tool + [_final("First.")], tool + [_final("Second.")])
        socket = FakeSocket()
        call = _call(host, socket, timing=Timing(**{**FAST.__dict__, "silence_after": 5.0}))
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("one"))
        await _until(lambda: "First." in socket.words())
        await socket.inbox.put(_prompt("two"))
        await _until(lambda: "Second." in socket.words())
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert socket.words().count(call_mod.DEFAULT_FILLER) == 2


class TestApprovals:
    async def test_a_confirm_parks_once_and_the_caller_is_told(self, saved, monkeypatch):
        parked = []

        async def park(**kw):
            parked.append(kw)
            return "ap_1"
        from core import api_approvals
        monkeypatch.setattr(api_approvals, "park", park)
        ask = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "refund"}
        host = ScriptedHost([{"type": "_step_progress", "turn": 1}, ask], [ask])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("refund me"), 0.1, _prompt("and again"))
        assert [t for t, _ in socket.said] == [call_mod.APPROVAL_LINE, call_mod.CANNOT_ASK_LINE]
        assert len(parked) == 1
        p = parked[0]
        assert p["door"] == "voice" and p["caller"]["number"] == "+14155550100"
        assert p["caller"]["visitor"]["approvals"] == "park" and p["progress"] == {"turn": 1}
        assert host.records[CALL]["approvals"] == ["ap_1"]
        assert saved["turns"][0] == ("refund me", call_mod.APPROVAL_LINE, [])

    @pytest.fixture
    def parks(self, monkeypatch):
        async def park(**kw):
            return "ap_1"
        from core import api_approvals
        monkeypatch.setattr(api_approvals, "park", park)

    def _parked_in_its_tool(self, gate):
        ask = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "refund"}
        return ScriptedHost([{"type": "tool_execution", "tool_name": "refund"}, ask, ("wait", gate),
                             {"type": "tool_result", "tool_name": "refund"}])

    async def test_a_parked_turn_is_saved_once_when_the_call_ends_behind_it(self, saved, parks):
        """It saved its own line; stopping it as the call ends must not save a second, empty copy (VOX1c,
        found when noting the filler slowed the parked turn's tail)."""
        gate = asyncio.Event()
        host = self._parked_in_its_tool(gate)
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("refund me"))
        await asyncio.sleep(0.1)
        await socket.inbox.put(None)  # the caller hangs up while the paused tool is still "running"
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.wait_for(task, 5)
        assert saved["turns"] == [("refund me", call_mod.APPROVAL_LINE, ["refund"])]
        assert saved["marks"] == [("refund me", {"filler": [call_mod.DEFAULT_FILLER]})]

    async def test_talking_over_a_parked_turn_cuts_its_line(self, saved, parks):
        gate = asyncio.Event()
        host = self._parked_in_its_tool(gate)
        socket = FakeSocket()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("refund me"))
        await asyncio.sleep(0.1)
        await socket.inbox.put(Interrupt("interrupt", heard="I've asked someone"))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.1)
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)
        assert saved["turns"] == [("refund me", call_mod.APPROVAL_LINE, ["refund"])]
        assert saved["cuts"] == ["I've asked someone"]


def _answer(approved=True, approval_id="ap_1"):
    """What a door hands a live call when a person answers its parked question (F474)."""
    return {"approval_id": approval_id, "approved": approved, "tool_name": "weather_today", "message": "weather?",
            "resume_state": {"pending_call": {"approval_id": approval_id, "approved": approved}}}


class TestAnAnswerDuringTheCall:
    """F474 (VOX1e, D263): an approval answered while the caller is still on the line was saved into the call and
    never spoken, so the call went on from words nobody heard, and a second ask was refused. Now the call speaks
    it at its next quiet moment, saved once, in the order heard; one it cannot say is resumed after the call."""

    QUIET = Timing(**{**FAST.__dict__, "silence_after": 5.0})
    ASK = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "weather_today"}
    ANSWER = [{"type": "tool_execution", "tool_name": "weather_today"},
              {"type": "tool_result", "tool_name": "weather_today"},
              _delta("It is cloudy, 27 degrees."), _final("It is cloudy, 27 degrees.")]
    HEARD = "It is cloudy, 27 degrees."

    @pytest.fixture
    def parks(self, monkeypatch):
        parked = []

        async def park(**kw):
            parked.append(kw)
            return f"ap_{len(parked)}"
        from core import api_approvals
        monkeypatch.setattr(api_approvals, "park", park)
        return parked

    async def _parked(self, host, socket):
        call = _call(host, socket, timing=self.QUIET)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("weather?"))
        await _until(lambda: (call_mod.APPROVAL_LINE, True) in socket.said)
        return call, task

    async def _hang_up(self, socket, task):
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)

    @staticmethod
    def _after_the_park(socket):
        return socket.words().split(call_mod.APPROVAL_LINE, 1)[1]

    async def test_it_is_spoken_with_its_lead_in_and_saved_once_as_the_approvals_answer(self, saved, parks):
        host = ScriptedHost([self.ASK], list(self.ANSWER))
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        assert call_mod.live_for_session(call.session_id) is call
        assert call.hand_answer(_answer()) is True
        await _until(lambda: socket.words().endswith(self.HEARD))
        await self._hang_up(socket, task)
        assert self._after_the_park(socket) == call_mod.APPROVED_LINE + call_mod.DEFAULT_FILLER + self.HEARD
        assert host.resumed == [_answer()] and host.after_call == []
        # The engine saves the resumed turn; the call saves only its own line, and marks the answer as the
        # approval's, so the log never reads it as the caller asking again.
        assert saved["turns"] == [("weather?", call_mod.APPROVAL_LINE, [])]
        assert saved["marks"] == [("weather?", {"filler": [call_mod.DEFAULT_FILLER],
                                                "answered_approval": {"id": "ap_1", "approved": True}})]
        assert call.first_text_ms == [], "an answer the caller did not just ask for is not a first-words sample"
        assert call_mod.live_for_session(call.session_id) is None

    async def test_a_refused_one_says_so_and_no_filler_for_the_tool_that_never_runs(self, saved, parks):
        refused = [{"type": "tool_execution", "tool_name": "weather_today"},
                   {"type": "tool_result", "tool_name": "weather_today", "refused": True},
                   _delta("Okay, I won't check it."), _final("Okay, I won't check it.")]
        host = ScriptedHost([self.ASK], refused)
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        assert call.hand_answer(_answer(approved=False)) is True
        await _until(lambda: socket.words().endswith("Okay, I won't check it."))
        await self._hang_up(socket, task)
        assert self._after_the_park(socket) == call_mod.REFUSED_LINE + "Okay, I won't check it."
        assert saved["marks"] == [("weather?", {"answered_approval": {"id": "ap_1", "approved": False}})]

    async def test_it_waits_for_the_turn_that_is_running(self, saved, parks):
        gate = asyncio.Event()
        host = ScriptedHost([self.ASK], [("wait", gate), _delta("We open at nine."), _final("We open at nine.")],
                            list(self.ANSWER))
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        await socket.inbox.put(_prompt("when do you open?"))
        await _until(lambda: len(host.turns) == 2)
        assert call.hand_answer(_answer()) is True
        await asyncio.sleep(0.1)
        assert call_mod.APPROVED_LINE not in socket.words(), "never over a turn that is running"
        gate.set()
        await _until(lambda: socket.words().endswith(self.HEARD))
        await self._hang_up(socket, task)
        assert self._after_the_park(socket) == ("We open at nine." + call_mod.APPROVED_LINE
                                                + call_mod.DEFAULT_FILLER + self.HEARD)

    async def test_after_it_a_second_ask_parks_again(self, saved, parks):
        host = ScriptedHost([self.ASK], list(self.ANSWER), [self.ASK])
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        call.hand_answer(_answer())
        await _until(lambda: socket.words().endswith(self.HEARD))
        await socket.inbox.put(_prompt("and tomorrow?"))
        await _until(lambda: socket.words().endswith(call_mod.APPROVAL_LINE))
        await self._hang_up(socket, task)
        assert len(parks) == 2 and call_mod.CANNOT_ASK_LINE not in socket.words()
        assert host.records[CALL]["approvals"] == ["ap_1", "ap_2"]

    async def test_one_still_waiting_when_the_caller_hangs_up_is_resumed_after_the_call(self, saved, parks):
        gate = asyncio.Event()
        host = ScriptedHost([self.ASK], [("wait", gate), _final("never")])
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        await socket.inbox.put(_prompt("when do you open?"))
        await _until(lambda: len(host.turns) == 2)
        assert call.hand_answer(_answer()) is True
        await self._hang_up(socket, task)
        assert host.after_call == [_answer()] and host.resumed == []
        assert call.hand_answer(_answer()) is False, "a call that is over takes no answer"

    async def test_one_the_caller_talks_over_as_it_starts_is_said_after_their_turn(self, saved, parks):
        gate = asyncio.Event()
        host = ScriptedHost([self.ASK], [("wait", gate)], [_final("We open at nine.")], list(self.ANSWER))
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        call.hand_answer(_answer())
        await _until(lambda: socket.words().endswith(call_mod.APPROVED_LINE))
        await socket.inbox.put(_prompt("when do you open?"))
        await _until(lambda: socket.words().endswith(self.HEARD))
        await self._hang_up(socket, task)
        assert self._after_the_park(socket) == (call_mod.APPROVED_LINE + "We open at nine." + call_mod.APPROVED_LINE
                                                + call_mod.DEFAULT_FILLER + self.HEARD)
        assert [t["text"] for t in host.turns] == ["weather?", "weather?", "when do you open?", "weather?"]
        assert saved["turns"] == [("weather?", call_mod.APPROVAL_LINE, [])], "nothing of the stopped one was kept"

    async def test_talking_over_it_keeps_what_was_heard(self, saved, parks):
        gate = asyncio.Event()
        host = ScriptedHost([self.ASK], [_delta("It is cloudy, "), ("wait", gate), _delta("27 degrees."),
                                         _final(self.HEARD)])
        socket = FakeSocket()
        call, task = await self._parked(host, socket)
        call.hand_answer(_answer())
        await _until(lambda: socket.words().endswith("It is cloudy, "))
        await socket.inbox.put(Interrupt("interrupt", heard="It is cloudy"))
        await asyncio.sleep(0.05)
        gate.set()
        await self._hang_up(socket, task)
        assert saved["turns"][-1][0] == "weather?" and saved["turns"][-1][1].startswith("It is cloudy")
        assert saved["marks"][-1] == ("weather?", {"answered_approval": {"id": "ap_1", "approved": True}})
        assert host.after_call == []

    async def test_a_moved_call_knows_its_question_still_waits(self, saved, parks):
        """A call picked up on a new socket (V10) used to forget it had parked one, and a second ask took the
        conversation's one slot from the first."""
        from core.store import sessions as sessions_mod
        socket = FakeSocket()
        call = _call(ScriptedHost([self.ASK]), socket, timing=self.QUIET, resume=True)
        await sessions_mod.set_pending_approval(call.session_id, "a1", {"approval_id": "ap_0",
                                                                        "tool_name": "weather_today",
                                                                        "api_turn": {"door": "voice"}})
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("weather?"))
        await _until(lambda: call_mod.CANNOT_ASK_LINE in socket.words())
        await self._hang_up(socket, task)
        assert parks == []


class TestTheVoiceTools:
    async def test_end_call_first_asks_whether_there_is_anything_else_then_ends_after_the_goodbye(self, saved):
        """VOX1d: the close is the call's. Changed deliberately from VOX1a's one-step end: a "No." to a question
        about a product ended a live call on a caller who still wanted it added to the cart."""
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Goodbye!"}),
               {"type": "tool_result", "tool_name": "end_call"}, _delta("never asked for"), _final("never")]
        host = ScriptedHost(list(end), list(end))
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("no"), 0.1, _prompt("no, that's all"), hang_up=False)
        assert socket.said == [(call_mod.CLOSING_QUESTION, True), ("Goodbye!", True)], "no second model call"
        assert socket.ended == [{"ended": "agent"}] and host.records[CALL]["outcome"] == "agent_ended"
        assert saved["turns"][0] == ("no", call_mod.CLOSING_QUESTION, ["end_call"])
        assert host.turns[0]["tools"] == ["end_call"]

    async def test_a_reply_that_wants_more_carries_on_and_the_next_end_asks_again(self, saved):
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        host = ScriptedHost(list(end), [_delta("Added to your cart."), _final("Added to your cart.")], list(end))
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("no"), 0.1, _prompt("just add it to my cart"), 0.1,
                     _prompt("no"), settle=0.15)
        assert [t for t, _ in socket.said] == [call_mod.CLOSING_QUESTION, "Added to your cart.",
                                               call_mod.CLOSING_QUESTION]
        assert socket.ended == [], "never ended on a caller who wasn't done"

    async def test_silence_after_the_closing_question_is_the_answer(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "end_call"},
                             ("tool", "end_call", {"goodbye": "Bye!"}), {"type": "tool_result"}])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("that's it"), settle=0.6, hang_up=False)
        assert [t for t, _ in socket.said] == [call_mod.CLOSING_QUESTION, call_mod.DEFAULT_GOODBYE]
        assert call_mod.STILL_THERE not in socket.words()
        assert socket.ended == [{"ended": "agent"}] and host.records[CALL]["outcome"] == "agent_ended"

    async def test_a_caller_who_speaks_during_the_goodbye_keeps_the_call(self, saved):
        """VOX1d: "No… just add it to my cart" lost its second half to a goodbye that ended the call at once."""
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Goodbye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        host = ScriptedHost(list(end), [_delta("Added to your cart."), _final("Added to your cart.")])
        socket = FakeSocket()
        call = _call(host, socket, timing=Timing(**{**FAST.__dict__, "goodbye_grace": 0.5}))
        call._closing_asked = True  # the caller has said there is nothing else
        await _drive(call, socket, _prompt("no"), 0.1, _prompt("just add it to my cart"), settle=0.2)
        assert [t for t, _ in socket.said] == ["Goodbye!", "Added to your cart."]
        assert socket.ended == [] and [t["text"] for t in host.turns] == ["no", "just add it to my cart"]

    async def test_silence_after_the_goodbye_ends_it_and_a_hang_up_on_it_is_still_the_agents(self, saved):
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        socket = FakeSocket()
        call = _call(ScriptedHost(list(end)), socket, timing=Timing(**{**FAST.__dict__, "goodbye_grace": 0.3}))
        call._closing_asked = True
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("no"))
        await asyncio.sleep(0.1)
        assert socket.ended == [], "the goodbye is said; the call waits a moment before it ends"
        await asyncio.sleep(0.4)
        assert socket.ended == [{"ended": "agent"}]
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)

        hung_up = FakeSocket()
        host = ScriptedHost(list(end))
        call = _call(host, hung_up, timing=Timing(**{**FAST.__dict__, "goodbye_grace": 0.5}))
        call._closing_asked = True
        await _drive(call, hung_up, _prompt("no"), 0.1, settle=0.0)  # the caller hangs up on "Bye!"
        assert hung_up.ended == [] and call.outcome == "agent_ended"
        assert host.records[CALL]["outcome"] == "agent_ended"

    async def test_after_the_agents_own_question_the_close_invites_instead_of_asking_again(self, saved):
        """F529 (VOX1f): the agent ended its answer "Is there anything else I can help you with?", the caller said
        "No. Thank you.", and the call asked the same question again before the goodbye (calls 1 and 3, two models,
        though the prompt says not to). Now the caller's reply to the agent's own question gets one line that
        leaves the door open, and a longer moment to carry on; VOX1d's question is kept for every other close."""
        answer = "The weather in Delhi is sunny. Is there anything else I can help you with?"
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        host = ScriptedHost([_delta(answer), _final(answer)], list(end))
        socket = FakeSocket()
        call = _call(host, socket, timing=Timing(**{**FAST.__dict__, "invite_grace": 0.05}))
        await _drive(call, socket, _prompt("weather in Delhi?"), 0.1, _prompt("No. Thank you."), settle=0.3,
                     hang_up=False)
        said = [t for t, _ in socket.said]
        assert call_mod.CLOSING_QUESTION not in said
        assert said[-1] == call_mod.INVITE_GOODBYE
        assert socket.ended == [{"ended": "agent"}] and host.records[CALL]["outcome"] == "agent_ended"
        assert saved["turns"][-1] == ("No. Thank you.", call_mod.INVITE_GOODBYE, ["end_call"])

    async def test_a_caller_who_speaks_after_the_invitation_carries_on(self, saved):
        answer = "It is in stock. Anything else about this product?"
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        host = ScriptedHost([_delta(answer), _final(answer)], list(end),
                            [_delta("The watch is $89.99."), _final("The watch is $89.99.")])
        socket = FakeSocket()
        call = _call(host, socket, timing=Timing(**{**FAST.__dict__, "invite_grace": 0.6}))
        await _drive(call, socket, _prompt("is the case in stock?"), 0.1, _prompt("No."), 0.15,
                     _prompt("but how much is a watch?"), settle=0.2)
        assert [t for t, _ in socket.said][-2:] == [call_mod.INVITE_GOODBYE, "The watch is $89.99."]
        assert socket.ended == [], "VOX1d's narrow question: the caller still had more, and the call kept them"

    async def test_the_invitation_holds_longer_than_a_goodbye(self, saved):
        answer = "Done. Is there anything else?"
        end = [{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye!"}),
               {"type": "tool_result", "tool_name": "end_call"}]
        socket = FakeSocket()
        call = _call(ScriptedHost([_delta(answer), _final(answer)], list(end)), socket,
                     timing=Timing(**{**FAST.__dict__, "goodbye_grace": 0.05, "invite_grace": 0.5}))
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("cancel my order"))
        await asyncio.sleep(0.1)
        await socket.inbox.put(_prompt("no"))
        await asyncio.sleep(0.25)
        assert socket.ended == [], "still open past the goodbye's moment"
        await asyncio.sleep(0.5)
        assert socket.ended == [{"ended": "agent"}]
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)

    async def test_transfer_is_offered_only_with_a_number(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "transfer_call"},
                             ("tool", "transfer_call", {"reason": "billing"}), {"type": "tool_result"},
                             _delta("never"), _final("never")])
        socket = FakeSocket()
        await _drive(_call(host, socket, voice={"transfer_number": "+15550001111"}), socket, _prompt("a human"),
                     hang_up=False)
        assert host.turns[0]["tools"] == ["end_call", "transfer_call"]
        assert "transfer_call" in host.turns[0]["system"]
        assert socket.said == [(call_mod.DEFAULT_TRANSFER_LINE, True)]
        assert socket.ended == [{"transfer": "billing"}]

        bare = ScriptedHost([_final("ok")])
        socket2 = FakeSocket()
        await _drive(_call(bare, socket2), socket2, _prompt("q"))
        assert bare.turns[0]["tools"] == ["end_call"] and "transfer_call" not in bare.turns[0]["system"]


class TestSilenceAndTheLimit:
    async def test_still_there_twice_then_goodbye(self, saved):
        socket = FakeSocket()
        call = _call(ScriptedHost(), socket)
        await asyncio.wait_for(call.run(), 5)
        assert [t for t, _ in socket.said] == [call_mod.STILL_THERE, call_mod.STILL_THERE, call_mod.SILENCE_GOODBYE]
        assert socket.ended == [{"ended": "silence"}] and call.outcome == "silence"

    async def test_speaking_resets_the_silence_count(self, saved):
        host = ScriptedHost([_final("Yes.")])
        socket = FakeSocket()
        call = _call(host, socket)
        task = asyncio.create_task(call.run())
        await asyncio.sleep(0.35)
        await socket.inbox.put(_prompt("I'm here"))
        await asyncio.wait_for(task, 5)
        said = [t for t, _ in socket.said]
        assert said[:3] == [call_mod.STILL_THERE, "Yes.", call_mod.STILL_THERE]
        assert said[-1] == call_mod.SILENCE_GOODBYE

    async def test_the_limit_warns_the_agent_then_says_goodbye(self, saved):
        host = ScriptedHost([_final("ok")], minutes=1)
        socket = FakeSocket()
        slow_silence = Timing(**{**FAST.__dict__, "silence_after": 5.0})
        call = _call(host, socket, started_at=time.monotonic() - 59.5, timing=slow_silence)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.wait_for(task, 5)
        assert "Wrap up now" in host.turns[0]["system"]
        assert socket.said[-1] == (call_mod.LIMIT_GOODBYE, True)
        assert socket.ended == [{"ended": "limit"}]


class TestTheTimedLinesAreKept:
    """F485 (VOX1e, call CAe1789c…): the lines the call says on its own timer — "Are you still there?", the
    goodbye after silence at the closing question, the silence and limit goodbyes — were never saved, so the log
    ended at the closing question. Each is noted on the last saved answer, as the filler is."""

    async def test_silence_after_an_answer_is_noted_on_it(self, saved):
        socket = FakeSocket()
        call = _call(ScriptedHost([_final("Yes.")]), socket)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("open on sunday?"))
        await asyncio.wait_for(task, 5)
        assert saved["marks"][-1] == ("open on sunday?", {"said_after": [
            call_mod.STILL_THERE, call_mod.STILL_THERE, call_mod.SILENCE_GOODBYE]})

    async def test_the_goodbye_after_silence_at_the_closing_question_is_noted(self, saved):
        host = ScriptedHost([{"type": "tool_execution", "tool_name": "end_call"},
                             ("tool", "end_call", {"goodbye": "Bye!"}), {"type": "tool_result"}])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("that's it"), settle=0.6, hang_up=False)
        assert saved["marks"][-1] == ("that's it", {"said_after": [call_mod.DEFAULT_GOODBYE]})

    async def test_a_new_turn_starts_its_own(self, saved):
        host = ScriptedHost([_final("Yes.")], [_final("No.")])
        socket = FakeSocket()
        call = _call(host, socket)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("one"))
        await _until(lambda: call_mod.STILL_THERE in socket.words())
        await socket.inbox.put(_prompt("two"))
        await asyncio.wait_for(task, 5)
        assert ("one", {"said_after": [call_mod.STILL_THERE]}) in saved["marks"]
        assert saved["marks"][-1][0] == "two" and saved["marks"][-1][1]["said_after"][0] == call_mod.STILL_THERE

    async def test_a_call_with_no_turn_notes_nothing(self, saved):
        socket = FakeSocket()
        await asyncio.wait_for(_call(ScriptedHost(), socket).run(), 5)
        assert saved["marks"] == []


class TestMovingTheCall:
    async def test_between_turns_it_ends_with_reconnect(self, saved):
        socket = FakeSocket()
        call = _call(ScriptedHost(), socket)
        task = asyncio.create_task(call.run())
        await asyncio.sleep(0.02)
        assert call_mod.move_all() == 1
        await asyncio.wait_for(task, 5)
        assert socket.said == [(call_mod.MOVE_LINE, True)] and socket.ended == [{"reconnect": True}]
        assert call_mod.live_calls() == []

    async def test_a_running_turn_finishes_first(self, saved):
        gate = asyncio.Event()
        host = ScriptedHost([("wait", gate), _delta("Done."), _final("Done.")])
        socket = FakeSocket()
        call = _call(host, socket)
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("q"))
        await asyncio.sleep(0.02)
        call.move()
        await asyncio.sleep(0.05)
        assert socket.ended == []
        gate.set()
        await asyncio.wait_for(task, 5)
        assert socket.words().endswith("Done." + call_mod.MOVE_LINE) and socket.ended == [{"reconnect": True}]


class TestRefusals:
    async def test_a_turn_the_host_refuses_ends_the_call(self, saved):
        host = ScriptedHost(refuse="I have to end the call here, someone will follow up.")
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"), hang_up=False)
        assert socket.said == [("I have to end the call here, someone will follow up.", True)]
        assert socket.ended == [{"ended": "refused"}] and host.turns == []

    async def test_a_socket_for_another_call_is_ended(self, saved):
        socket = FakeSocket()
        await _drive(_call(ScriptedHost(), socket), socket, Setup("setup", call_sid="CA" + "9" * 32), hang_up=False)
        assert socket.ended == [{"ended": "mismatch"}]

    async def test_a_dropped_socket_is_the_caller_hanging_up(self, saved):
        socket = FakeSocket()
        call = _call(ScriptedHost(), socket)
        await _drive(call, socket, Frame("unknown"), settle=0.01)
        assert call.outcome == "caller_hung_up"


class TestWhatTheCallerHeard:
    """ConversationRelay's `utteranceUntilInterrupt` is the clause that was playing, not all that was heard
    (measured on a real call, VOX1a P1: "over the hill and down to the")."""

    def test_the_answer_is_cut_at_the_end_of_that_clause(self):
        from core.store.sessions import cut_at_heard
        answer = "The fox jumps over the dog, then runs over the hill and down to the river, where it drinks."
        assert cut_at_heard(answer, "over the hill and down to the") == (
            "The fox jumps over the dog, then runs over the hill and down to the")

    def test_a_clause_not_in_the_answer_or_none_leaves_it(self):
        from core.store.sessions import cut_at_heard
        assert cut_at_heard("**Bold** answer.", "something else") == "**Bold** answer."
        assert cut_at_heard("An answer.", "") == "An answer."
        assert cut_at_heard("Hello There friend.", "hello there") == "Hello There"

    async def test_the_saved_answer_keeps_everything_before_the_clause(self):
        from core.session import _save_conversation_turn
        from core.store import sessions
        await _save_conversation_turn("voice_c_CA1", "a1", "story?", "Once upon a time, a fox ran, and then it slept.")
        assert await sessions.cut_last_answer("voice_c_CA1", "a1", "a fox ran")
        last = (await sessions.get("voice_c_CA1", "a1"))["messages"][-1]
        assert last["content"] == "Once upon a time, a fox ran" and last["interrupted"] is True


class TestTheCallsLogReadsTheMarks:
    async def test_the_filler_is_noted_only_on_its_own_questions_answer(self):
        from core.session import _save_conversation_turn
        from core.store import sessions
        await _save_conversation_turn("voice_c_CA2", "a1", "first?", "One.")
        assert not await sessions.mark_last_answer("voice_c_CA2", "a1", question="second?", filler=["Hold on."])
        assert await sessions.mark_last_answer("voice_c_CA2", "a1", question="first?", filler=["Hold on."])
        last = (await sessions.get("voice_c_CA2", "a1"))["messages"][-1]
        assert last["content"] == "One." and last["filler"] == ["Hold on."]
        assert not await sessions.mark_last_answer("voice_c_none", "a1", question="first?", filler=["x"])

    async def test_the_history_carries_the_cut_and_the_filler(self):
        from core.session import _get_conversation_history, _save_conversation_turn
        from core.store import sessions
        await _save_conversation_turn("voice_c_CA3", "a1", "plain?", "Plain.")
        await _save_conversation_turn("voice_c_CA3", "a1", "story?", "Once upon a time, a fox ran, and slept.")
        await sessions.cut_last_answer("voice_c_CA3", "a1", "a fox ran")
        await sessions.mark_last_answer("voice_c_CA3", "a1", question="story?", filler=["One moment."])
        plain, story = await _get_conversation_history("voice_c_CA3", agent_id="a1")
        assert "interrupted" not in plain and "filler" not in plain, "a chat's turns are unchanged"
        assert story["interrupted"] is True and story["filler"] == ["One moment."]
        assert story["assistant"] == "Once upon a time, a fox ran"


class TestEndingTheSession:
    async def test_we_never_close_first_the_provider_does_after_the_last_words(self, saved):
        """Closing right after `end` cut the goodbye and Twilio called it a dropped socket (64105), which then
        resumed the call (Naveen's first real call)."""
        class SlowToClose(FakeSocket):
            async def end(self, handoff=None):
                self.ended.append(handoff)  # Twilio closes later, when it has finished speaking

        host = ScriptedHost([{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Goodbye!"}), {"type": "tool_result", "tool_name": "end_call"}])
        socket = SlowToClose()
        call = _call(host, socket)
        call._closing_asked = True  # VOX1d: the caller already said there was nothing else
        task = asyncio.create_task(call.run())
        await socket.inbox.put(_prompt("bye"))
        await asyncio.sleep(0.2)
        assert socket.ended == [{"ended": "agent"}] and not task.done(), "still waiting for the provider"
        await socket.inbox.put(None)
        await asyncio.wait_for(task, 5)

    async def test_a_provider_that_never_closes_is_let_go(self, saved):
        class NeverCloses(FakeSocket):
            async def end(self, handoff=None):
                self.ended.append(handoff)

        host = ScriptedHost([{"type": "tool_execution", "tool_name": "end_call"}, ("tool", "end_call", {"goodbye": "Bye."}), {"type": "tool_result", "tool_name": "end_call"}])
        socket = NeverCloses()
        task = asyncio.create_task(_call(host, socket).run())
        await socket.inbox.put(_prompt("bye"))
        await asyncio.wait_for(task, 5)  # close_wait is 0.5 s here


class TestWhatTheModelSaysBeforeATool:
    async def test_it_is_spoken_even_without_punctuation(self, saved):
        """B: "Let me check that for you" before a tool is for the caller, so a reset does not drop it."""
        host = ScriptedHost([_delta("Let me check that for you"), {"type": "answer_reset", "reason": "tool_call"},
                             {"type": "tool_execution", "tool_name": "check_stock"}, {"type": "tool_result"},
                             _delta("It's in stock."), _final("It's in stock.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("router in stock?"))
        assert socket.said[0] == ("Let me check that for you", True)
        assert call_mod.DEFAULT_FILLER not in socket.words()
        assert socket.words().endswith("It's in stock.")

    async def test_another_reset_still_drops_it(self, saved):
        host = ScriptedHost([_delta("Half a thought"), {"type": "answer_reset", "reason": "retry"},
                             _delta("The answer."), _final("The answer.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("q"))
        assert socket.words() == "The answer."

    async def test_a_goodbye_said_before_end_call_is_not_said_twice(self, saved):
        host = ScriptedHost([_delta("Thanks for calling, bye!"), {"type": "answer_reset", "reason": "tool_call"},
                             {"type": "tool_execution", "tool_name": "end_call"},
                             ("tool", "end_call", {"goodbye": "Goodbye!"}), {"type": "tool_result"}])
        socket = FakeSocket()
        call = _call(host, socket)
        call._closing_asked = True  # VOX1d: the caller already said there was nothing else
        await _drive(call, socket, _prompt("bye"), hang_up=False)
        assert socket.words() == "Thanks for calling, bye!" and socket.ended == [{"ended": "agent"}]

    def test_the_voice_prompt_says_so(self):
        # "Before you use a tool" became "Before your first tool call … Say it once" (VOX1b's real calls:
        # a model that searched twice said it twice).
        for rule in ("Before your first tool call", "Say it once", "Never read a web address", "card numbers",
                     "anything else", "a thank-you alone is not goodbye"):
            assert rule in call_mod.VOICE_SUFFIX

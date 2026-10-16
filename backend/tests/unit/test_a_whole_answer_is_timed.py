"""An answer said whole is timed as a streamed one is (F731, VOX1f, D273).

A call records how long each turn took to its first words (`first_words_ms`; the Phone tab's "First answer in
…"). A turn the fleet runs — a workflow agent's — reaches the call as one `final`, not a stream, and the call said
it without its timer: no workflow agent's call on dev had a first-answer time, so the delay Naveen heard on one
(2026-10-16, CAe0fa321e…) could not be read off its log.
"""
from tests.unit.test_voice_call_loop import CALL, FakeSocket, ScriptedHost, _call, _drive, _final, _prompt, saved  # noqa: F401


async def test_a_whole_answer_has_its_first_words_timed(saved):
    host = ScriptedHost([_final("We open at nine.", streamed=False)])
    socket = FakeSocket()
    await _drive(_call(host, socket), socket, _prompt("When do you open?"))
    assert "We open at nine." in socket.words()
    timed = host.records.get(CALL, {}).get("first_words_ms") or []
    assert len(timed) == 1 and timed[0] >= 0


async def test_a_streamed_answer_is_timed_once_as_before(saved):
    host = ScriptedHost([{"type": "answer_delta", "text": "We open at nine. ", "turn": 1}, _final("We open at nine.")])
    socket = FakeSocket()
    await _drive(_call(host, socket), socket, _prompt("When do you open?"))
    assert len(host.records.get(CALL, {}).get("first_words_ms") or []) == 1

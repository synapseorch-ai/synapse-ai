"""A replay that ended leaves nobody driving its browser (F1376, v2.0's laptop session; eb 11-04).

On the v2.0 candidate's run page, once a Browser step's replay had run its last step, the live pane said "The agent
is driving", with Take over, until the run closed: `Session.replay` takes the agent's wheel to drive (the network
gate and the money backstop hold as for an agent), and nothing gave it back. An agent's own turn ends with `idle`:
nobody drives until its next call. Now a replay that ran to its end does the same, so the pane says nobody is
driving. A replay that stopped keeps the wheel where its stop put it: a miss leaves it for the step's Browser agent to
find the way, a person's turn and a take-over are the person's.
"""
from __future__ import annotations

from test_a_take_over_stops_the_replay import STEPS, _replay, _session


async def test_a_replay_that_ran_to_its_end_leaves_nobody_driving():
    s = _session()
    done = await _replay(s)
    assert done.get("done") == 3, done
    assert s.state()["wheel"] == "nobody" and s.state()["replaying"] is False


async def test_an_unattended_replays_send_list_and_the_agents_gate_outlast_its_idle():
    """A page may send late (test_browser_host_e2e's late send): the replay's own send list, and the agent's gate
    over what the page sends, stay until the agent's next call or the next replay, idle or not."""
    s = _session()
    sending = [{**STEPS[0], "sends": ["POST shop.example"]}, *STEPS[1:]]
    await s.replay(sending, {}, unattended=True, recording_id="rec_e014a4ab03517d2ee981")
    assert s.state()["wheel"] == "nobody" and s.idle
    assert s.replay_sends == {"POST shop.example"}, "the send list outlasts the replay's end"
    assert s.gates_as_agent(), "what the page sends now is held to the agent's rules, as while the replay drove"


async def test_a_person_who_took_over_is_never_gated_as_the_agent():
    s = _session()
    s.page.during["https://example.com/"] = s.take
    await _replay(s)
    assert not s.gates_as_agent()


async def test_the_agents_next_call_takes_the_idle_browser_back():
    s = _session()
    await _replay(s)
    assert s.idle is True, "idle, as an agent's turn leaves it: the next call takes it back"


async def test_a_take_over_keeps_the_persons_wheel():
    s = _session()
    s.page.during["https://example.com/"] = s.take
    await _replay(s)
    assert s.state()["wheel"] == "person"


async def test_a_miss_leaves_the_wheel_for_the_agent_that_finds_the_way():
    s = _session()
    broken = [{**STEPS[0], "url": "https://broken.example/"}]

    async def goto(url, **_kw):
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    s.page.goto = goto
    stopped = await s.replay(broken, {}, unattended=True, recording_id="rec_e014a4ab03517d2ee981")
    assert stopped.get("miss"), stopped
    assert s.state()["wheel"] == "agent"

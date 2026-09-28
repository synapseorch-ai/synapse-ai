"""An attached image is honest across turns (F245).

A screenshot of a 7-row table was answered exactly. On the next turn, "on that
same screenshot, what is the 4th failing test and the text at the bottom?" was
answered with a test name and a footer that are not in it: the image had gone
to the model on the first model call of its own turn only, was never stored,
and nothing told the model it was gone.

Now the image rides every model call of the turn it was attached to, and every
later turn is told it was there and is no longer attached — so the model can
say it no longer sees it instead of inventing what it showed. The stored turn
keeps only what it was (a count and a type), never the image.
"""
import json
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDw"
       "AChwGA60e6kgAAAABJRU5ErkJggg==")


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _turn(agent, fake_llm, script, message, images=None, session_id="s_f245"):
    from core.react_engine import run_agent_step

    async def executor(name, args):
        return "ok"

    fake_llm.script(script)
    start = fake_llm.call_count
    [ev async for ev in run_agent_step(
        message=message, agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent, images=images, tool_executor=executor, max_turns=5,
        tools_override=[{"type": "function", "function": {
            "name": "zoom", "description": "z", "parameters": {"type": "object", "properties": {}}}}])]
    return fake_llm.calls[start:]


class TestTheNextTurn:
    async def test_it_is_told_the_image_is_no_longer_attached(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, ["Seven tests fail."], "what fails in this screenshot?", images=[PNG])
        calls = await _turn(agent, fake_llm, ["I can no longer see it."],
                            "on that same screenshot, what is the 4th failing test?")

        question = calls[0]["history_messages"][0]["content"]
        assert question.startswith("what fails in this screenshot?")
        assert "image" in question and "no longer attached" in question and "PNG" in question
        assert not calls[0].get("images"), "the image itself is not sent again"

    async def test_the_stored_turn_keeps_what_it_was_not_the_image(self, fake_llm):
        from core.store import sessions as store

        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, ["ok"], "look", images=[PNG, PNG], session_id="s_f245_store")
        (row,) = [r for r in await store.load() if r["session_id"] == "s_f245_store"]
        user = row["messages"][0]
        assert user["content"] == "look", "the person's words are stored as typed"
        assert user["images"] == [{"type": "image/png"}, {"type": "image/png"}]
        assert "base64" not in json.dumps(row["messages"])


class TestItsOwnTurn:
    async def test_every_model_call_of_the_turn_sees_it(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        calls = await _turn(agent, fake_llm, [tool_call("zoom"), "The 4th is test_summary_total_row."],
                            "what is the 4th failing test?", images=[PNG])
        assert len(calls) == 2
        assert all(c.get("images") == [PNG] for c in calls), [bool(c.get("images")) for c in calls]


class TestOnTheFleet:
    async def test_the_job_keeps_what_the_question_carried(self, monkeypatch):
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod
        upserts = []

        async def history(*_a):
            return []

        async def upsert(_sf, _sid, _aid, status, messages):
            upserts.append((status, [dict(m) for m in messages]))

        async def nothing(*_a, **_kw):
            return None

        async def acquire(*_a, **_kw):
            return object()

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "final", "response": "Seven tests fail."}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        await worker_mod.run_agent_chat_job(
            {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j"},
            "f245-fleet", "agent_x", "what fails here?", images=[PNG])

        user = upserts[-1][1][-2]
        assert user == {"role": "user", "content": "what fails here?", "images": [{"type": "image/png"}]}

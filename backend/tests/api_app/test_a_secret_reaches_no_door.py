"""No door a client reads carries the engine's own events, or a secret variable's value — F854 (BF11, D311).

QA1c read an agent's whole prompt, `{{ global.x }}` rendered, on the chat stream (`_log_llm_call`). The engine's
side is `tests/unit/test_a_secret_reaches_no_stream_and_no_store.py`; this is each door's, through
`core.events_public.public_event`: the app's chat stream and its replay, the Redis streams every scale-mode reader
follows (written by the publishers, read by the bridge — an entry an older publisher wrote included), and the
queue and journal a run's readers drain.
"""
from __future__ import annotations

import asyncio
import json

from _fakes import canary
from _fakes import seed as S

canary_secret = canary.canary_secret
CANARY = canary.CANARY

PROMPT = {"type": "_log_llm_call", "turn": 1, "system_prompt": "You are the billing agent.", "prompt": "User Request"}


class TestTheAppsChat:
    async def test_the_stream_and_its_replay_carry_no_prompt_and_no_secret(
            self, client, canary_secret, fake_llm, tmp_path):
        agent = S.make_agent(id="agent_door", name="Billing", tools=["all"], skip_default_tools=True,
                             system_prompt="Use key {{ global.API_TOKEN }}.")
        await S.seed_agents([agent])
        fake_llm.set_default(f"Done. (I was told {CANARY}.)")

        streamed = await client.post("/chat/stream", json={"message": "hi", "session_id": "chat_door",
                                                            "agent_id": agent["id"]})
        replayed = await client.get("/chat/chat_door/events")

        assert CANARY in fake_llm.calls[0]["sys_prompt"]
        for body in (streamed.text, replayed.text):
            assert "_log_llm_call" not in body and "system_prompt" not in body
            assert CANARY not in body, "an answer that echoes it is shown by name"
        assert json.dumps(canary.MARK)[1:-1] in streamed.text  # as the frame's JSON writes it
        assert canary.left_behind(tmp_path) == []


class TestTheRedisStreams:
    async def test_the_publishers_write_only_what_a_client_sees(self, fake_redis):
        from core.scale.pubsub import ChatEventPublisher, RunEventPublisher

        chat = ChatEventPublisher(fake_redis, "s_pub")
        run = RunEventPublisher(fake_redis, "r_pub")
        for publisher in (chat, run):
            await publisher.publish(PROMPT)
            await publisher.publish({"type": "_step_progress", "context_text": "User Request"})
            await publisher.publish({"type": "status", "message": "Working"})
        await chat.publish({"type": "orchestration_complete", "final_state": {"a": 1, "_exec_memory_s": {}}})

        def written(key):
            return [json.loads(fields[b"data"]) for _, fields in key]

        chat_entries = written(await fake_redis.xrange("synapse:chat:s_pub:events"))
        run_entries = written(await fake_redis.xrange("synapse:run:r_pub:events"))
        assert [e["type"] for e in chat_entries] == ["status", "orchestration_complete"]
        assert chat_entries[-1]["final_state"] == {"a": 1}
        assert [e["type"] for e in run_entries] == ["status"]

    async def test_an_entry_written_before_the_fix_is_not_read(self, fake_redis):
        from core.scale.event_bridge import get_chat_events, stream_chat_events

        key = "synapse:chat:s_old:events"
        await fake_redis.xadd(key, {"data": json.dumps(PROMPT)})
        await fake_redis.xadd(key, {"data": json.dumps({"type": "final", "response": "ok"})})
        await fake_redis.xadd(key, {"data": json.dumps({"type": "done"})})

        listed = [e["event"]["type"] for e in await get_chat_events(fake_redis, "s_old")]
        frames = []
        async for frame in stream_chat_events(fake_redis, "s_old", "0"):
            frames.append(frame)
            if '"done"' in frame:
                break
        assert listed == ["final", "done"]
        assert "_log_llm_call" not in "".join(frames) and '"final"' in "".join(frames)


class TestARunsQueue:
    async def test_the_queue_a_runs_readers_drain_holds_only_public_events(self):
        from core.orchestration.runner import SENTINEL, spawn_engine_run

        async def engine():
            yield {"type": "_log_prompt", "prompt": "Fetch the invoice"}
            yield {"type": "step_start", "orch_step_id": "s1"}
            yield {"type": "orchestration_complete", "status": "completed",
                   "final_state": {"answer": 1, "_caller": {"user": "u"}}}

        task, queue = spawn_engine_run(engine(), "run_door")
        await task
        seen = []
        while True:
            event = await asyncio.wait_for(queue.get(), 5)
            if event is SENTINEL:
                break
            seen.append(event)
        assert [e["type"] for e in seen] == ["step_start", "orchestration_complete"]
        assert seen[-1]["final_state"] == {"answer": 1}

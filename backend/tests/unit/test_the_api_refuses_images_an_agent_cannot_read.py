"""
OSS's chat API refuses an image the agent will not read (F396).

The composer gates Attach, a drop and a paste on `/api/status`'s
`can_see_images` (F317, and F313 for a workflow agent), but `/chat/stream`,
v1 and v2 took the images from any other caller and handed them on: a
text-only provider dropped them (DeepSeek with a log line, the CLI providers in
silence) and a workflow with no step that takes attachments never read them.
The person was answered as if nothing had been attached. Cloud's `start_turn`
answers 422 with a sentence; so does OSS now, before a turn is claimed or a
job queued, with the composer's own reason.
"""
import base64
import struct
import zlib

import pytest

from _fakes import seed as S


def _png() -> str:
    """A real 1×1 PNG, as a data URI."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(raw).decode()


IMAGE = _png()
BLIND = "deepseek-chat"     # DeepSeek's chat model reads no images
SEES = "gpt-4o"


def _workflow(step_model: str, *, takes: bool) -> dict:
    return S.make_orchestration(id="orch_f396", entry_step_id="look", steps=[{
        "id": "look", "name": "Look", "type": "agent", "agent_id": "agent_f396_eyes",
        "model": step_model, "pass_images": takes, "prompt": "Describe it.", "next_step_id": None,
    }])


@pytest.fixture
async def agents():
    await S.seed_agents([
        S.make_agent(id="agent_f396_blind", model=BLIND),
        S.make_agent(id="agent_f396_eyes", model=SEES),
        S.make_orchestrator_agent("orch_f396", id="agent_f396_flow"),
    ])


class TestChatStream:
    async def test_an_agent_whose_model_cannot_read_images(self, client, agents):
        resp = await client.post("/chat/stream", json={
            "message": "what is this?", "agent_id": "agent_f396_blind", "session_id": "s_f396a",
            "images": [IMAGE]})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            f"This agent's model ({BLIND}) cannot read images. Send the message without them, "
            "or choose a model that can for this agent.")

    async def test_a_workflow_no_step_of_which_takes_them(self, client, agents):
        await S.seed_orchestrations([_workflow(SEES, takes=False)])
        resp = await client.post("/chat/stream", json={
            "message": "what is this?", "agent_id": "agent_f396_flow", "session_id": "s_f396b",
            "images": [IMAGE]})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith("No step of this workflow takes attachments.")

    async def test_a_workflow_whose_taking_step_cannot_read_them(self, client, agents):
        await S.seed_orchestrations([_workflow(BLIND, takes=True)])
        resp = await client.post("/chat/stream", json={
            "message": "what is this?", "agent_id": "agent_f396_flow", "session_id": "s_f396c",
            "images": [IMAGE]})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"Step 'Look' takes attachments, but {BLIND} cannot read images."

    async def test_an_agent_that_can_is_not_refused(self, client, agents, fake_llm):
        fake_llm.script(["A green pixel."])
        resp = await client.post("/chat/stream", json={
            "message": "what is this?", "agent_id": "agent_f396_eyes", "session_id": "s_f396d",
            "images": [IMAGE]})
        assert resp.status_code == 200, resp.text
        assert "A green pixel." in resp.text

    async def test_no_images_is_never_refused(self, client, agents, fake_llm):
        fake_llm.script(["Hello."])
        resp = await client.post("/chat/stream", json={
            "message": "hi", "agent_id": "agent_f396_blind", "session_id": "s_f396e"})
        assert resp.status_code == 200, resp.text

    async def test_the_one_shot_route_too(self, client, agents):
        resp = await client.post("/chat", json={
            "message": "what is this?", "agent_id": "agent_f396_blind", "session_id": "s_f396f",
            "images": [IMAGE]})
        assert resp.status_code == 422, resp.text


class TestV1:
    @pytest.mark.parametrize("route", ["/api/v1/chat", "/api/v1/chat/stream"])
    async def test_refused(self, client, api_key, agents, route):
        resp = await client.post(route, json={"message": "what is this?", "agent": "agent_f396_blind",
                                              "images": [IMAGE]}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert f"({BLIND}) cannot read images" in resp.json()["detail"]


class TestV2:
    @pytest.fixture
    async def v2(self, scale_app, monkeypatch):
        from _fakes.fake_pg import fake_session_factory
        scale_app.state.pg_session_factory = fake_session_factory()
        return scale_app

    async def test_refused_before_anything_is_queued(self, client, api_key, agents, v2):
        resp = await client.post("/api/v2/chat", json={"message": "what is this?", "agent": "agent_f396_blind",
                                                       "images": [IMAGE]}, headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        assert f"({BLIND}) cannot read images" in resp.json()["detail"]
        v2.state.arq_redis.enqueue_job.assert_not_awaited()

    async def test_an_agent_that_can_is_queued_with_them(self, client, api_key, agents, v2):
        resp = await client.post("/api/v2/chat", json={"message": "what is this?", "agent": "agent_f396_eyes",
                                                       "images": [IMAGE]}, headers=api_key["headers"])
        assert resp.status_code == 202, resp.text
        _, kwargs = v2.state.arq_redis.enqueue_job.call_args
        assert kwargs["images"] == [IMAGE]

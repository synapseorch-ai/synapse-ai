"""A Synapse AI chat read back carries what each finished turn built — F1577 (BLD5; Naveen 11-13).

After Synapse AI builds something, the person is taken to it: a new workflow's page, where Run is, or Chat with the
agent it made. Someone who left the assistant while the build ran is not pulled back; the conversation says where it is
instead, with a link (Naveen, through eb). A Synapse AI tab left during a build lets its stream go (TABS1: only what is
on screen streams), so coming back reads the chat from its runs (`GET /api/builder/conversation?session=`), and that
read held each turn's words only: the way to what the turn built was gone.

Each finished turn now comes back with its writes as its `resource_saved` events said them (`run_writes`, the run's
own record): the workflows and agents it made, changed or took away, each with its kind, action, id, name and, for
a removal or a put-back, why. The panel draws the way to what was built from those, by the rule it uses live
(`builderEvents.placeBuilt`). A turn that failed or is still open carries none, and nothing else of the run's private
state goes with them.
"""
from __future__ import annotations

from core.orchestration.run_writes import WRITES_KEY
from test_builder_comes_back import _park, _row, _set, builder  # noqa: F401 — fixture

BUILT = [
    {"kind": "tool", "action": "created", "id": "tool_lookup", "name": "Lookup"},
    {"kind": "agent", "action": "created", "id": "agent_reader", "name": "Reader"},
    {"kind": "orchestration", "action": "created", "id": "orch_invoices", "name": "Invoice reader"},
    {"kind": "schedule", "action": "created", "id": "sched_1", "name": "Every Friday", "enabled": False,
     "when": "every Friday at 9:00"},
]


async def _chat(client, cid: str) -> dict:
    response = await client.get("/api/builder/conversation", params={"session": cid})
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


async def _finish(run_id: str, *, status: str = "completed", writes=None, **state) -> None:
    row = await _row(run_id)
    await _set(run_id, status=status, waiting_for_human=False,
               shared_state={**row.shared_state, "summary": "Built the workflow \"Invoice reader\".",
                             **({WRITES_KEY: writes} if writes is not None else {}), **state})


async def test_a_finished_build_comes_back_with_its_workflows_and_agents(client, builder, fake_llm):
    asked = await _park(client, fake_llm, message="read my invoices", cid="built-back")
    await _finish(asked["run_id"], writes=BUILT)

    [turn] = (await _chat(client, "built-back"))["turns"]

    assert turn["user"] == "read my invoices"
    assert turn["writes"] == [
        {"kind": "agent", "action": "created", "id": "agent_reader", "name": "Reader"},
        {"kind": "orchestration", "action": "created", "id": "orch_invoices", "name": "Invoice reader"},
    ], "a read-back turn does not say which workflows and agents it built"


async def test_a_removal_says_why(client, builder, fake_llm):
    # A build that stopped half way takes back what it made, and says why (F1281): the panel reads that as nothing
    # standing, so nobody is sent to a workflow that is gone.
    asked = await _park(client, fake_llm, cid="built-undone")
    undone = [{"kind": "orchestration", "action": "created", "id": "orch_x", "name": "X"},
              {"kind": "orchestration", "action": "deleted", "id": "orch_x", "name": "X",
               "reason": "the build stopped before it was saved whole: a step failed"}]
    await _finish(asked["run_id"], writes=undone)

    [turn] = (await _chat(client, "built-undone"))["turns"]

    assert turn["writes"] == undone


async def test_a_turn_that_built_nothing_carries_no_writes(client, builder, fake_llm):
    # Every turn that only talked reads back as it always has: its words and nothing else (TABS2's shape).
    asked = await _park(client, fake_llm, cid="built-nothing")
    await _finish(asked["run_id"], writes=[{"kind": "schedule", "action": "updated", "id": "s", "name": "S"}])

    [turn] = (await _chat(client, "built-nothing"))["turns"]

    assert "writes" not in turn


async def test_a_failed_turn_carries_none(client, builder, fake_llm):
    # A build that failed moves nobody, live or read back.
    asked = await _park(client, fake_llm, cid="built-failed")
    await _finish(asked["run_id"], status="failed", writes=BUILT)

    [turn] = (await _chat(client, "built-failed"))["turns"]

    assert "writes" not in turn


async def test_an_open_turn_carries_none(client, builder, fake_llm):
    # A plan still waiting has written nothing; what it may write is its card's business, never a way to go.
    await _park(client, fake_llm, cid="built-open")
    asked = await _chat(client, "built-open")

    assert asked["pending"] is not None
    assert all("writes" not in t for t in asked["turns"])

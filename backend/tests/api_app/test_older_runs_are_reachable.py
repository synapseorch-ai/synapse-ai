"""Every run can be reached, however many came after it, and a workflow's own runs on their own (F1639).

`GET /api/orchestrations/runs` gave the newest runs up to 200 and nothing past them: no cursor, and no way to ask for
one workflow's runs, so the editor's Recent Runs read everyone's newest hundred and kept its own (a workflow behind a
hundred newer runs elsewhere showed none). Now the list takes `before`, the last row's `created_at` and `run_id` as it
gave them, and answers the runs strictly older, newest first, with runs created in the same instant told apart by
their id; and `workflow`, one workflow's runs, paged the same way. Each row says its `created_at`.

Near misses: the call with no limit gives exactly the twenty it always gave; a limit is still capped at 200; a cursor
past the oldest run gives none; a cursor the list never gave is refused with what it takes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState

BASE = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


async def _seed(count_a: int = 150, count_b: int = 80) -> list[tuple[datetime, str, str]]:
    """`count_a` runs of orch_a and `count_b` of orch_b, five to an instant, so the cursor meets ties."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import OrchestrationRunDB

    made: list[tuple[datetime, str, str]] = []
    for i in range(count_a + count_b):
        workflow = "orch_a" if i < count_a else "orch_b"
        run_id = f"run_older_{i:04d}"
        await SharedState(OrchestrationRun(run_id=run_id, orchestration_id=workflow, status="completed",
                                           shared_state={})).checkpoint()
        made.append((BASE + timedelta(seconds=i // 5), run_id, workflow))
    async with session() as s:
        for at, run_id, _ in made:
            await s.execute(update(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id).values(created_at=at))
        await s.commit()
    return made


def _cursor(row: dict) -> str:
    return f"{row['created_at']},{row['run_id']}"


async def _all_pages(client, **params) -> list[dict]:
    rows: list[dict] = []
    before = None
    for _ in range(50):
        page = (await client.get("/api/orchestrations/runs",
                                 params={**params, **({"before": before} if before else {})})).json()
        if not page:
            return rows
        rows += page
        before = _cursor(page[-1])
    raise AssertionError("the pages never ran out")


class TestEveryRunIsReachable:
    async def test_the_pages_hold_every_run_once_newest_first(self, client):
        made = await _seed()
        rows = await _all_pages(client, limit=100)
        ids = [r["run_id"] for r in rows]
        assert len(ids) == len(set(ids)) == len(made), (len(ids), len(set(ids)))
        expected = [run_id for _, run_id, _ in sorted(made, key=lambda m: (m[0], m[1]), reverse=True)]
        assert ids == expected

    async def test_one_workflows_runs_page_on_their_own(self, client):
        made = await _seed()
        rows = await _all_pages(client, limit=30, workflow="orch_b")
        assert {r["orchestration_id"] for r in rows} == {"orch_b"}
        assert sorted(r["run_id"] for r in rows) == sorted(run_id for _, run_id, w in made if w == "orch_b")

    async def test_each_row_says_when_it_was_made(self, client):
        await _seed(3, 0)
        rows = (await client.get("/api/orchestrations/runs", params={"limit": 5})).json()
        assert all(r.get("created_at") for r in rows), rows


class TestNearMisses:
    async def test_no_limit_gives_the_twenty_it_always_gave(self, client):
        await _seed(30, 0)
        assert len((await client.get("/api/orchestrations/runs")).json()) == 20

    async def test_a_limit_is_still_capped_at_200(self, client):
        await _seed(205, 0)
        assert len((await client.get("/api/orchestrations/runs", params={"limit": 500})).json()) == 200

    async def test_a_cursor_past_the_oldest_gives_none(self, client):
        await _seed(3, 0)
        oldest = (await client.get("/api/orchestrations/runs", params={"limit": 5})).json()[-1]
        assert (await client.get("/api/orchestrations/runs", params={"before": _cursor(oldest)})).json() == []

    @pytest.mark.parametrize("before", ["yesterday", "2026-09-30T10:00:00+00:00", ",run_x"])
    async def test_a_cursor_the_list_never_gave_is_refused(self, client, before):
        response = await client.get("/api/orchestrations/runs", params={"before": before})
        assert response.status_code == 422 and "created_at" in response.text, response.text

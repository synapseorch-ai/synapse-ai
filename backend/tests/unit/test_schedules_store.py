"""
Schedules in the store.

Schedules were a JSON file with two independent writers. What matters after the
move is that the tick's "enabled and overdue" question is answered by the
database rather than a Python scan, that a fire updates one row instead of
rewriting everything, and that the tenant predicate is on every read — the
table is keyed on a bare `id`, so nothing else stops a cross-tenant reach.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.scale.context import set_resource_provider
from core.store import schedules as store
from core.store.resources import CrossTenantWrite
from core.tenancy import tenant_scope


class _Provider:
    """Registering a resource provider is what unlocks a second tenant."""

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


def _now():
    return datetime.now(timezone.utc)


def _make(sid="sched_1", **overrides):
    item = {
        "id": sid,
        "name": "Nightly",
        "description": "",
        "enabled": True,
        "target_type": "agent",
        "target_id": "agent_1",
        "prompt": "go",
        "schedule_type": "interval",
        "interval_value": 5,
        "interval_unit": "minutes",
        "cron_expression": None,
        "missed_run_policy": "skip",
        "created_at": "2026-01-02T03:04:05Z",
        "last_run_at": None,
        "next_run_at": None,
    }
    item.update(overrides)
    return item


async def test_round_trip_keeps_the_api_shape():
    await store.save(_make(next_run_at="2026-01-02T03:09:05Z"))

    got = await store.get("sched_1")
    assert got["id"] == "sched_1"
    assert got["interval_value"] == 5
    assert got["missed_run_policy"] == "skip"
    # Promoted columns come back as the same ISO strings the API always spoke.
    assert got["enabled"] is True
    assert got["created_at"] == "2026-01-02T03:04:05Z"
    assert got["next_run_at"] == "2026-01-02T03:09:05Z"
    assert got["last_run_at"] is None


async def test_due_selects_only_enabled_and_overdue():
    now = _now()
    past = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    await store.save(_make("sched_overdue", next_run_at=past))
    await store.save(_make("sched_future", next_run_at=future))
    await store.save(_make("sched_off", next_run_at=past, enabled=False))
    await store.save(_make("sched_never", next_run_at=None))

    assert [s["id"] for s in await store.due(now)] == ["sched_overdue"]


async def test_a_fire_updates_one_row_and_leaves_the_rest_alone():
    """The two whole-file rewrites per fire are what this replaces.

    The second one used to run *after* the run finished, so it wrote back a
    list loaded before the run started — clobbering anything the API changed
    while it was in flight.
    """
    await store.save(_make("sched_a", next_run_at="2026-01-02T03:09:05Z"))
    await store.save(_make("sched_b", next_run_at="2026-01-02T03:09:05Z"))

    fired = datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone.utc)
    await store.set_next_run("sched_a", fired + timedelta(minutes=5))
    await store.set_last_run("sched_a", fired)

    a = await store.get("sched_a")
    b = await store.get("sched_b")
    assert a["next_run_at"] == "2026-01-02T04:05:00Z"
    assert a["last_run_at"] == "2026-01-02T04:00:00Z"
    assert b["next_run_at"] == "2026-01-02T03:09:05Z"   # untouched
    assert b["last_run_at"] is None


async def test_editing_a_schedule_does_not_move_it_in_the_list():
    await store.save(_make("sched_1", created_at="2026-01-01T00:00:00Z"))
    await store.save(_make("sched_2", created_at="2026-01-02T00:00:00Z"))

    await store.save(_make("sched_1", name="Renamed", created_at="2099-01-01T00:00:00Z"))

    listed = await store.load()
    assert [s["id"] for s in listed] == ["sched_1", "sched_2"]
    assert listed[0]["name"] == "Renamed"
    assert listed[0]["created_at"] == "2026-01-01T00:00:00Z"


async def test_another_tenants_schedule_is_invisible_and_unwritable(multi_tenant):
    with tenant_scope("acme"):
        await store.save(_make("sched_1", next_run_at="2026-01-02T03:09:05Z"))

    with tenant_scope("globex"):
        assert await store.load() == []
        assert await store.get("sched_1") is None
        assert await store.delete_one("sched_1") is False
        # The tick is the read that runs unattended every 30 seconds; it must
        # not pick up another tenant's overdue schedule and execute it.
        assert await store.due(_now() + timedelta(days=365)) == []
        with pytest.raises(CrossTenantWrite):
            await store.save(_make("sched_1", name="stolen"))

    with tenant_scope("acme"):
        assert (await store.get("sched_1"))["name"] == "Nightly"


async def test_replace_is_scoped_to_one_tenant(multi_tenant):
    with tenant_scope("acme"):
        await store.save(_make("sched_1"))

    with tenant_scope("globex"):
        await store.replace([_make("sched_other")])
        assert [s["id"] for s in await store.load()] == ["sched_other"]

    with tenant_scope("acme"):
        assert [s["id"] for s in await store.load()] == ["sched_1"]


class TestADeploymentThatArmsIsTold:
    """F149 (D122). A deployment that does not poll `due()` — cloud arms each
    occurrence as a deferred job instead — never learns about a schedule the
    engine writes itself: the builder's `create_schedule`, the operator's
    `set_schedule_enabled`. The row is listed, enabled, with a next run, and
    nothing ever fires it. `set_saved_observer` is how such a deployment hears
    of every save, after it commits."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        yield
        store.set_saved_observer(None)

    async def test_nothing_is_installed_by_default(self):
        seen = []
        await store.save(_make("sched_quiet"))
        assert seen == []  # and no error: OSS's scheduler polls, so it installs none

    async def test_each_save_is_reported_after_it_commits(self):
        seen = []

        async def observer(item):
            # The row must already be readable: an armed job that wakes to
            # find no row stands down and never re-arms.
            seen.append((item["id"], await store.get(item["id"]) is not None))

        store.set_saved_observer(observer)
        await store.save(_make("sched_seen"))
        await store.save(_make("sched_seen", enabled=False))
        assert seen == [("sched_seen", True), ("sched_seen", True)]

    async def test_a_save_that_writes_nothing_reports_nothing(self):
        seen = []

        async def observer(item):
            seen.append(item)

        store.set_saved_observer(observer)
        await store.save({"name": "no id"})
        assert seen == []

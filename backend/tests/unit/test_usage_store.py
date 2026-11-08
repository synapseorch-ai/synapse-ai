"""
Usage logs and the model rate card.

Two tables with deliberately opposite tenancy. Usage is per-tenant and
append-only; pricing is what a model costs, which is the same number for
everyone, so it carries no tenant at all — the one table in the store where a
second tenant seeing the same rows is the intended behaviour rather than a
leak.
"""
import pytest

from core.scale.context import set_resource_provider
from core.store import usage as store
from core.tenancy import tenant_scope


class _Provider:
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


async def _log(**overrides):
    from core import usage_tracker

    kwargs = {
        "model": "claude-x", "provider": "anthropic", "input_tokens": 100,
        "output_tokens": 50, "context_chars": 1000, "session_id": "s1", "source": "chat",
    }
    kwargs.update(overrides)
    await usage_tracker.log_usage(**kwargs)


# ── the event log ────────────────────────────────────────────────────────────

async def test_a_call_is_one_row_and_keeps_the_record_shape():
    await _log(run_id="run_1", tool_name="search", latency_seconds=0.5)

    records = await store.query(limit=10)
    assert len(records) == 1
    r = records[0]
    assert r["model"] == "claude-x"
    assert r["run_id"] == "run_1"
    assert r["tool_name"] == "search"
    assert r["response_cache_hit"] is False
    assert r["timestamp"].endswith("Z")


async def test_a_compaction_event_keeps_its_own_shape():
    """The two kinds of row share a table but not a key set.

    The usage screen tells them apart by which keys are present, so a
    compaction record must not start carrying `provider` or the cache counters
    just because an LLM call needs columns for them.
    """
    from core import usage_tracker

    await usage_tracker.log_compaction_event(
        stage="trim", chars_before=1000, chars_after=400, session_id="s1",
    )

    r = (await store.query(limit=10))[0]
    assert r["event_type"] == "compaction"
    assert r["stage"] == "trim"
    assert r["chars_saved"] == 600
    assert r["reduction_pct"] == 60
    assert "provider" not in r
    assert "cache_read_tokens" not in r


async def test_filtering_and_ordering_happen_in_the_database():
    for i in range(5):
        await _log(session_id="s1", run_id=f"run_{i}", model=f"m{i}")
    await _log(session_id="s2", model="other")

    # A session read is oldest-first, because the screen shows the context
    # delta from one turn to the next.
    per_session = await store.query(limit=10, session_id="s1")
    assert [r["model"] for r in per_session] == ["m0", "m1", "m2", "m3", "m4"]

    # An unfiltered read is newest-first.
    assert (await store.query(limit=1))[0]["model"] == "other"

    assert len(await store.query(limit=10, run_id="run_3")) == 1
    assert len(await store.query(limit=2, session_id="s1")) == 2
    assert (await store.query(limit=2, offset=2, session_id="s1"))[0]["model"] == "m2"


async def test_summaries_exclude_compaction_from_the_request_count():
    from core import usage_tracker

    await _log()
    await usage_tracker.log_compaction_event(stage="trim", chars_before=10, chars_after=5)

    summary = await usage_tracker.get_usage_summary()
    assert summary["total_requests"] == 1
    assert summary["total_input_tokens"] == 100


async def test_clear_reports_what_it_removed():
    from core import usage_tracker

    await _log()
    await _log()
    assert await usage_tracker.clear_usage_logs() == 2
    assert await store.query(limit=10) == []


async def test_one_tenants_usage_is_invisible_to_another(multi_tenant):
    with tenant_scope("acme"):
        await _log(model="acme-model")

    with tenant_scope("globex"):
        assert await store.query(limit=10) == []
        assert await store.all_records() == []
        assert await store.clear() == 0

    with tenant_scope("acme"):
        assert len(await store.query(limit=10)) == 1


# ── the rate card ────────────────────────────────────────────────────────────

async def test_seeding_never_overwrites_an_edited_price():
    """A restart must not undo an operator's correction.

    This is the whole reason seeding inserts rather than upserts: pricing is
    editable on the usage screen, and in a hosted deployment by staff or an
    automated feed.
    """
    await store.seed_pricing({"gpt-4o": {"provider": "openai", "input_per_1m": 2.5}})
    await store.save_pricing({"gpt-4o": {"provider": "openai", "input_per_1m": 99.0}})

    added = await store.seed_pricing(
        {"gpt-4o": {"provider": "openai", "input_per_1m": 2.5},
         "brand-new": {"provider": "openai", "input_per_1m": 1.0}}
    )

    assert added == 1
    table = await store.load_pricing()
    assert table["gpt-4o"]["input_per_1m"] == 99.0
    assert "brand-new" in table


async def test_pricing_is_shared_across_tenants(multi_tenant):
    """The one table in the store with no tenant dimension, on purpose.

    A per-tenant rate card would mean every org carried its own copy of the
    same numbers, and an operator updating a price reached nobody.
    """
    with tenant_scope("acme"):
        await store.save_pricing({"gpt-4o": {"provider": "openai", "input_per_1m": 2.5}})

    with tenant_scope("globex"):
        assert (await store.load_pricing())["gpt-4o"]["input_per_1m"] == 2.5

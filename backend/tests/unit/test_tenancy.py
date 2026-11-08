"""
The shipped product is single-tenant, and there is no way to make it otherwise.

The engine is multi-tenant *capable* — that is what lets one worker fleet serve
many tenants concurrently, and it is what an embedder builds on. But tenancy is
a capability of the engine, not a feature of the application: nothing a user can
reach — no setting, no environment variable, no API field, no HTTP route —
produces a second tenant.

These tests are the guard on that boundary. They are the same shape as the
"exactly two SECURITY DEFINER functions" test on the cloud schema: an assertion
about what does *not* exist, which is the only kind that survives someone
helpfully adding a knob back.

This is a product boundary, not a claim about what is possible. The source is
AGPL; a fork may call `enable_multi_tenancy()` and is then distributing a
derivative work under the same licence. What is prevented is the shipped
application offering tenancy as something to switch on.
"""
import asyncio
import inspect

import pytest

from core import tenancy
from core.scale.context import set_resource_provider
from core.store.models import DEFAULT_TENANT


@pytest.fixture(autouse=True)
def _single_tenant_by_default():
    """Every test starts from the shipped configuration and restores it."""
    set_resource_provider(None)
    yield
    set_resource_provider(None)


# ── the boundary ─────────────────────────────────────────────────────────────

def test_the_shipped_build_has_exactly_one_tenant():
    assert tenancy.is_multi_tenant() is False
    assert tenancy.get_tenant() == DEFAULT_TENANT


def test_tenant_scope_refuses_to_open():
    """The only way to change tenants, and it is closed by default."""
    with pytest.raises(tenancy.SingleTenantError):
        with tenancy.tenant_scope("acme"):
            pass


def test_there_is_no_public_setter():
    """A `set_tenant()` would be a one-line bypass of every check here."""
    exported = {name for name in dir(tenancy) if not name.startswith("_")}
    assert "set_tenant" not in exported
    assert "current_tenant" not in exported


def test_no_environment_variable_selects_a_tenant(monkeypatch):
    """Config knobs were the other way in. DEFAULT_TENANT_ID and
    ENABLE_TENANT_ISOLATION used to exist and are gone."""
    for var in ("SYNAPSE_TENANT_ID", "DEFAULT_TENANT_ID", "ENABLE_TENANT_ISOLATION",
                "TENANT_ID", "WORKER_QUEUE_SHARD"):
        monkeypatch.setenv(var, "acme")

    from core.scale.config import get_scale_config
    cfg = get_scale_config()

    assert not hasattr(cfg, "default_tenant_id")
    assert not hasattr(cfg, "enable_tenant_isolation")
    assert tenancy.get_tenant() == DEFAULT_TENANT


def test_no_setting_selects_a_tenant():
    """A setting that *names* a tenant is a selector and must not exist.

    `rate_limit_per_tenant_rps` is deliberately still here: it is a per-tenant
    limit, not a way to choose which tenant you are.
    """
    from core.config import default_settings

    selectors = [
        k for k in default_settings()
        if k.lower() in {"tenant", "tenant_id"} or k.lower().endswith("_tenant_id")
    ]
    assert not selectors


def test_the_queue_name_does_not_encode_a_tenant():
    """A queue bound to a tenant is one worker pool per tenant, which is the
    per-org fleet this design exists to remove."""
    from core.scale.config import QUEUE_NAME
    assert QUEUE_NAME == "synapse:orchestrations"


def test_job_functions_take_no_tenant_argument():
    """`tenant_id` on the job payload let any caller who could enqueue label
    its run with any tenant."""
    from core.scale import worker

    for name in ("run_orchestration_job", "resume_orchestration_job",
                 "resume_failed_job", "run_agent_chat_job"):
        params = inspect.signature(getattr(worker, name)).parameters
        assert "tenant_id" not in params, f"{name} still accepts tenant_id"


class TestTheJobTenantResolver:
    """The hook that lets an embedder say which tenant a queued job belongs to.

    It is the one place tenancy enters the worker, so the thing worth pinning is
    that it is not a *second* place multi-tenancy can be obtained from. It is
    strictly downstream of `set_resource_provider()`: without one it refuses to
    register, and even if it were installed the tenant it named could not be
    entered, because `tenant_scope()` is shut.
    """

    def test_it_refuses_to_register_without_a_resource_provider(self):
        from core.scale import context

        context.set_resource_provider(None)
        with pytest.raises(tenancy.SingleTenantError):
            context.set_job_tenant_resolver(lambda *a: None)

        assert context.get_job_tenant_resolver() is None
        assert tenancy.is_multi_tenant() is False

    def test_removing_the_provider_removes_the_resolver(self):
        from core.scale import context

        context.set_resource_provider(_Provider())
        context.set_job_tenant_resolver(lambda *a: None)
        assert context.get_job_tenant_resolver() is not None

        context.set_resource_provider(None)
        assert context.get_job_tenant_resolver() is None, (
            "a resolver left installed would name tenants nothing can enter"
        )

    async def test_no_resolver_means_the_default_tenant(self):
        """The shipped product: the handler runs exactly as it did before."""
        from core.scale import context, worker

        context.set_resource_provider(None)
        seen = []

        @worker._for_the_jobs_tenant
        async def a_job(ctx, value):
            seen.append((tenancy.get_tenant(), value))

        await a_job({}, "x")
        assert seen == [(DEFAULT_TENANT, "x")]

    async def test_the_resolver_establishes_the_tenant_around_the_body(self):
        from core.scale import context, worker

        context.set_resource_provider(_Provider())
        try:
            async def resolver(ctx, job_name, args, kwargs):
                return kwargs.get("whose") or args[0]

            context.set_job_tenant_resolver(resolver)
            seen = []

            @worker._for_the_jobs_tenant
            async def a_job(ctx, *args, **kwargs):
                seen.append(tenancy.get_tenant())

            await a_job({}, "acme")
            await a_job({}, whose="globex")

            assert seen == ["acme", "globex"]
            assert tenancy.get_tenant() == DEFAULT_TENANT, "the scope leaked past the job"
        finally:
            context.set_resource_provider(None)

    async def test_concurrent_jobs_keep_their_own_tenant(self):
        """The property the whole shared fleet rests on, at the job boundary."""
        from core.scale import context, worker

        context.set_resource_provider(_Provider())
        try:
            async def resolver(ctx, job_name, args, kwargs):
                return args[0]

            context.set_job_tenant_resolver(resolver)
            observed: dict[str, list[str]] = {}

            @worker._for_the_jobs_tenant
            async def a_job(ctx, whose):
                seen = []
                for _ in range(3):
                    seen.append(tenancy.get_tenant())
                    await asyncio.sleep(0)   # the other job runs here
                observed[whose] = seen

            await asyncio.gather(a_job({}, "acme"), a_job({}, "globex"))

            assert observed["acme"] == ["acme"] * 3
            assert observed["globex"] == ["globex"] * 3
        finally:
            context.set_resource_provider(None)

    def test_the_decorator_preserves_the_signature_it_guards(self):
        """`test_job_functions_take_no_tenant_argument` must keep meaning something."""
        from core.scale import worker

        params = inspect.signature(worker.run_orchestration_job).parameters
        assert "run_id" in params and "orch_id" in params
        assert "tenant_id" not in params
        assert worker.run_orchestration_job.__name__ == "run_orchestration_job", (
            "ARQ dispatches on __name__"
        )


def test_the_v2_api_does_not_accept_a_tenant():
    from core.routes.api_v2 import V2ChatRequest, V2OrchestrationRunRequest

    for model in (V2OrchestrationRunRequest, V2ChatRequest):
        assert "tenant_id" not in model.model_fields, f"{model.__name__} accepts tenant_id"


def test_there_are_no_tenant_management_routes():
    """`POST /scale/tenants` made tenancy an operator-facing feature."""
    from core.routes import scale

    paths = {r.path for r in scale.router.routes}
    assert not [p for p in paths if "tenant" in p], f"tenant routes present: {paths}"


# ── the capability, once an embedder opts in ─────────────────────────────────

class _Provider:
    """The minimum a resource provider must supply."""

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


def test_registering_a_provider_is_what_unlocks_tenancy():
    """The two are coupled deliberately: switching tenants without a
    tenant-aware way to resolve resources gives every tenant the same agents
    and tools, which is worse than refusing to switch."""
    set_resource_provider(_Provider())
    assert tenancy.is_multi_tenant() is True

    with tenancy.tenant_scope("acme"):
        assert tenancy.get_tenant() == "acme"

    assert tenancy.get_tenant() == DEFAULT_TENANT


def test_removing_the_provider_closes_the_door_again():
    set_resource_provider(_Provider())
    set_resource_provider(None)

    assert tenancy.is_multi_tenant() is False
    with pytest.raises(tenancy.SingleTenantError):
        with tenancy.tenant_scope("acme"):
            pass


def test_scopes_nest_and_restore_by_token():
    set_resource_provider(_Provider())
    with tenancy.tenant_scope("acme"):
        with tenancy.tenant_scope("globex"):
            assert tenancy.get_tenant() == "globex"
        assert tenancy.get_tenant() == "acme"


def test_an_empty_tenant_is_refused():
    """A falsy tenant would silently read the default tenant's data."""
    set_resource_provider(_Provider())
    with pytest.raises(ValueError):
        with tenancy.tenant_scope(""):
            pass


async def test_concurrent_tasks_do_not_observe_each_others_tenant():
    """The property that makes one shared worker fleet safe.

    A module global here would mean job B's tenant is visible to job A the
    moment they interleave on the event loop. Each asyncio task gets its own
    copy of the context, so they cannot see each other.
    """
    set_resource_provider(_Provider())
    observed: dict[str, list[str]] = {}

    async def job(tenant: str) -> None:
        with tenancy.tenant_scope(tenant):
            seen = [tenancy.get_tenant()]
            await asyncio.sleep(0)          # yield: the other job runs here
            seen.append(tenancy.get_tenant())
            await asyncio.sleep(0)
            seen.append(tenancy.get_tenant())
            observed[tenant] = seen

    await asyncio.gather(job("acme"), job("globex"))

    assert observed["acme"] == ["acme"] * 3
    assert observed["globex"] == ["globex"] * 3

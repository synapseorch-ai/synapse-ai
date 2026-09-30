"""The index conformance suite, run against the in-memory reference in every capability variant.

B1 runs the same `conformance.CASES` against `SqlKnowledgeIndex` (SQLite, and
Postgres when `SYNAPSE_TEST_POSTGRES_URL` is set); Knowledge E1 against cloud's
`PgKnowledgeIndex`. Passing here proves the cases themselves are sound.
"""
from __future__ import annotations

import pytest

from core.knowledge.testing import conformance
from core.knowledge.testing.reference_index import VARIANTS, InMemoryKnowledgeIndex
from core.scale.context import set_resource_provider
from core.tenancy import tenant_scope


class _Provider:
    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def harness():
    set_resource_provider(_Provider())
    yield conformance.IndexHarness(tenant=tenant_scope)
    set_resource_provider(None)


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize("case", conformance.CASES, ids=lambda c: c.name)
async def test_reference_index(case, variant, harness):
    caps = VARIANTS[variant]
    if not case.applies(caps):
        pytest.skip(f"{case.name} does not apply to {variant}")
    assert await conformance.run_case(case, lambda: InMemoryKnowledgeIndex(caps), harness)


def test_every_variant_runs_a_real_share_of_the_cases():
    for name, caps in VARIANTS.items():
        assert sum(case.applies(caps) for case in conformance.CASES) >= 12, name


async def test_a_broken_index_fails_the_suite(harness):
    """The cases catch what they claim to: an index that ignores its filter fails."""
    class IgnoresFilters(InMemoryKnowledgeIndex):
        def _filtered(self, pool, node):
            return pool, True  # says it filtered, did not

    case = next(c for c in conformance.CASES if c.name == "filters_run_inside_the_index")
    with pytest.raises(AssertionError):
        await conformance.run_case(case, IgnoresFilters, harness)


async def test_a_leaky_index_fails_the_tenant_case(harness):
    class Leaky(InMemoryKnowledgeIndex):
        def _mine(self):
            return list(self._rows.values())

    case = next(c for c in conformance.CASES if c.name == "tenants_are_isolated")
    with pytest.raises(AssertionError):
        await conformance.run_case(case, Leaky, harness)

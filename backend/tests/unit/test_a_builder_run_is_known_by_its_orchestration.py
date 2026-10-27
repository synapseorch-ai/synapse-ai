"""A builder run is a run of the builder's orchestration, by its stored id, in both products (F1126, SAI2-P3).

Cloud seeds a builder copy per workspace under an id it mints, so three checks that compared a run with open source's
fixed `orch_native_builder` never matched there (F1093's reply net, F946's made-tool removal, F1063's one writer). The
session never decides: a caller may name an ordinary run's session `builder:…`. Each site's own test file holds its
case with a host's id; this holds the predicate.
"""
from __future__ import annotations

import pytest

from core.models_orchestration import OrchestrationRun
from core.native_builder import identity
from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

HOSTS = "orch_0192f3a1c4d07b2e9a6f5d8c3b1e0a47"


@pytest.fixture
def host():
    async def ids():
        return [HOSTS]

    identity.set_builder_ids(ids)
    yield
    identity.set_builder_ids(None)


def _run(orchestration_id: str, session_id: str | None = None) -> OrchestrationRun:
    return OrchestrationRun(run_id="run_x", orchestration_id=orchestration_id, session_id=session_id)


async def test_open_source_knows_its_fixed_id():
    assert await identity.builder_ids() == {NATIVE_BUILDER_ORCH_ID}
    assert await identity.is_builder_run(_run(NATIVE_BUILDER_ORCH_ID))
    assert not await identity.is_builder_run(_run("orch_digest"))


async def test_a_hosts_own_id_is_the_builders(host):
    assert await identity.is_builder_run(_run(HOSTS))


async def test_a_host_replaces_the_fixed_id(host):
    """On cloud `orch_native_builder` is no builder: any id a workspace's row could carry is an ordinary one."""
    assert not await identity.is_builder_run(_run(NATIVE_BUILDER_ORCH_ID))


@pytest.mark.parametrize("with_host", [False, True])
async def test_a_builder_session_never_makes_an_ordinary_run_the_builders(request, with_host):
    if with_host:
        request.getfixturevalue("host")
    spoof = _run("orch_digest", session_id="builder:org1:panel:someone-elses-chat")
    assert not await identity.is_builder_run(spoof)


async def test_a_run_with_no_orchestration_is_no_builders(host):
    assert not await identity.is_builder_run(_run(""))
    assert not await identity.is_builder_run(object())

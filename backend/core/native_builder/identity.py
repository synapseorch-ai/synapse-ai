"""Which runs are Synapse AI's own: a builder run is a run of the builder's orchestration (F1126, SAI2-P3).

Open source seeds the builder once, under the fixed id `orch_native_builder`. Cloud seeds a copy per workspace under
an id it mints, so three checks that compared a run's orchestration with the fixed id never matched there: the reply
net that takes a secret ask out of a turn's reply (F1093), the removal of a cancelled build's made tools (F946), and
one chat writing a workflow at a time (F1063). A host whose builder ids are its own registers them (`set_builder_ids`),
and every check asks here.

The run's stored orchestration id decides, never its session: a session id is the caller's to choose on an ordinary
run (`builder:…` included), so it would let a run pass for the builder's, or the builder's for an ordinary one.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable

from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

#: The host's reading of the current tenant's builder orchestration ids; None is open source's one fixed id.
_host_ids: Callable[[], Awaitable[Iterable[str]]] | None = None


def set_builder_ids(read: Callable[[], Awaitable[Iterable[str]]] | None) -> None:
    """A host whose builder orchestrations carry ids of its own (cloud's per-workspace copy) registers them here: they
    replace the fixed id, which is then an ordinary id like any other."""
    global _host_ids
    _host_ids = read


async def builder_ids() -> frozenset[str]:
    """The ids of the current tenant's builder orchestration."""
    if _host_ids is None:
        return frozenset({NATIVE_BUILDER_ORCH_ID})
    return frozenset(str(i) for i in await _host_ids() if i)


async def is_builder_run(run: Any) -> bool:
    """Whether `run` is a run of the builder's orchestration."""
    orchestration_id = str(getattr(run, "orchestration_id", "") or "")
    return bool(orchestration_id) and orchestration_id in await builder_ids()

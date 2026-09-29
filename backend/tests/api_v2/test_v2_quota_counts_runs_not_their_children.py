"""v2's queue quota counts a tenant's runs, not their nested children (F292).

`_check_tenant_quota` counted every live row of the tenant, and a run with
nested steps has a row per live child (`{run}__{step}_d{depth}_…`, one per
loop iteration since F251) — so nesting spent the tenant's run allowance, and
before F257 a child left `running` by a timeout counted for ever. The OSS side
of cloud's F283, which admits the same way.
"""
import pytest
from fastapi import HTTPException

from core.routes.api_v2 import _check_tenant_quota


async def _seed(tenant_id: str, limit: int, runs: dict[str, str]):
    from core.store.engine import get_store
    from core.store.models import OrchestrationRunDB, TenantDB
    sf = await get_store()
    async with sf() as s:
        s.add(TenantDB(tenant_id=tenant_id, name=tenant_id, max_queued_runs=limit))
        for run_id, status in runs.items():
            s.add(OrchestrationRunDB(run_id=run_id, orchestration_id="orch_q", tenant_id=tenant_id,
                                     status=status, shared_state={}, step_history=[]))
        await s.commit()
    return sf


class TestTheQuota:
    async def test_a_run_with_live_children_counts_once(self):
        sf = await _seed("t_f292", 2, {
            "run_orch_q_1_aaaaaa": "running",
            "run_orch_q_1_aaaaaa__work_d1_bbbbbb": "running",
            "run_orch_q_1_aaaaaa__ask_d1_cccccc": "paused",
        })
        await _check_tenant_quota(sf, "t_f292", None)  # one run of two: admitted

    async def test_a_tenant_at_its_limit_is_still_refused(self):
        sf = await _seed("t_f292_full", 2, {
            "run_orch_q_1_dddddd": "running",
            "run_orch_q_2_eeeeee": "queued",
            "run_orch_q_2_eeeeee__work_d1_ffffff": "running",
        })
        with pytest.raises(HTTPException) as refused:
            await _check_tenant_quota(sf, "t_f292_full", None)
        assert refused.value.status_code == 429

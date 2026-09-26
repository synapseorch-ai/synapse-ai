"""
A schedule is checked before it is saved (F197).

`POST /api/schedules` — what the form, v1 and the CLI use — took an interval of
0 or -5 (a next run in the past, so an enabled one fired on every 30 s tick),
answered a cron it could not read with a 500, and saved a schedule whose target
does not exist, which then fired into nothing for ever. The answers are cloud's
(`api/control/routes/schedules.py`): 422 with a sentence for the timing, 404
"No such agent: X." for the target, so one request gets one answer on both.
"""
import pytest

from _fakes import seed as S


def _body(**overrides):
    body = {"name": "Nightly", "target_type": "agent", "target_id": "agent_f197",
            "prompt": "run", "schedule_type": "interval",
            "interval_value": 5, "interval_unit": "minutes"}
    body.update(overrides)
    return body


@pytest.fixture
def manager(test_app):
    # The routes gate only on a manager being present; the work goes to the store.
    test_app.state.schedule_manager = object()


@pytest.fixture
async def target():
    await S.seed_agents([S.make_agent(id="agent_f197", name="Target")])


async def _saved():
    from core.store import schedules as schedule_store
    return await schedule_store.load()


@pytest.fixture(params=["app", "v1"])
async def post(request, client, api_key):
    path, headers = (("/api/schedules", {}) if request.param == "app"
                     else ("/api/v1/schedules", api_key["headers"]))

    async def _post(body):
        return await client.post(path, json=body, headers=headers)
    return _post


class TestTiming:
    @pytest.mark.parametrize("value", [0, -5, 10_001])
    async def test_an_interval_outside_1_to_10000_is_refused(self, post, manager, target, value):
        resp = await post(_body(interval_value=value))
        assert resp.status_code == 422, resp.text
        assert isinstance(resp.json()["detail"], str), "a sentence the form's toast can show"
        assert await _saved() == []

    async def test_an_interval_needs_a_value_and_a_unit(self, post, manager, target):
        resp = await post(_body(interval_value=None))
        assert resp.status_code == 422
        assert resp.json()["detail"] == "An interval schedule needs a value and a unit."
        assert await _saved() == []

    async def test_a_cron_it_cannot_read_is_a_422_not_a_500(self, post, manager, target):
        resp = await post(_body(schedule_type="cron", cron_expression="not a cron"))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith("That is not a valid cron expression")
        assert await _saved() == []

    async def test_a_cron_schedule_needs_an_expression(self, post, manager, target):
        resp = await post(_body(schedule_type="cron", cron_expression="  "))
        assert resp.status_code == 422
        assert resp.json()["detail"] == "A cron schedule needs an expression."

    async def test_a_good_schedule_is_saved_with_a_next_run_ahead(self, post, manager, target):
        from core.scheduler import _parse_iso, _utc_now
        made = await post(_body())
        assert made.status_code == 200, made.text
        assert _parse_iso(made.json()["next_run_at"]) > _utc_now()
        cron = await post(_body(schedule_type="cron", cron_expression="0 9 * * 1"))
        assert cron.status_code == 200, cron.text


class TestTarget:
    async def test_an_agent_that_does_not_exist(self, post, manager):
        resp = await post(_body(target_id="agent_does_not_exist"))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No such agent: agent_does_not_exist."
        assert await _saved() == []

    async def test_an_orchestration_that_does_not_exist(self, post, manager):
        resp = await post(_body(target_type="orchestration", target_id="orch_nope"))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No such orchestration: orch_nope."

    async def test_the_builders_own_agents_and_orchestration_are_not_targets(self, post, manager):
        await S.seed_agents([S.make_agent(id="agent_native_builder_x", system="native_builder")])
        await S.seed_orchestrations([S.make_orchestration(id="orch_native_builder", system="native_builder")])
        agent = await post(_body(target_id="agent_native_builder_x"))
        orch = await post(_body(target_type="orchestration", target_id="orch_native_builder"))
        assert (agent.status_code, orch.status_code) == (404, 404)
        assert await _saved() == []


class TestEditing:
    async def _made(self, client):
        made = await client.post("/api/schedules", json=_body())
        assert made.status_code == 200, made.text
        return made.json()["id"]

    async def test_a_full_update_is_checked_too(self, client, manager, target):
        sid = await self._made(client)
        resp = await client.put(f"/api/schedules/{sid}", json=_body(schedule_type="cron",
                                                                   cron_expression="61 * * * *"))
        assert resp.status_code == 422
        assert (await client.put(f"/api/schedules/{sid}", json=_body(target_id="agent_gone"))).status_code == 404

    async def test_a_patch_to_the_timing_or_target_is_checked(self, client, manager, target):
        sid = await self._made(client)
        assert (await client.patch(f"/api/schedules/{sid}", json={"interval_value": 0})).status_code == 422
        assert (await client.patch(f"/api/schedules/{sid}", json={"schedule_type": "cron"})).status_code == 422
        assert (await client.patch(f"/api/schedules/{sid}", json={"target_id": "agent_gone"})).status_code == 404
        stored = (await client.get(f"/api/schedules/{sid}")).json()
        assert (stored["interval_value"], stored["schedule_type"], stored["target_id"]) == (
            5, "interval", "agent_f197"), "a refused patch changes nothing"

    async def test_a_schedule_saved_before_the_checks_can_still_be_switched_off(self, client, manager):
        """A row saved before F197 (-5, a target since deleted) must not be stuck on."""
        from core.store import schedules as schedule_store
        await schedule_store.save({
            "id": "sched_old", "name": "Old", "enabled": True, "created_at": "2026-09-01T00:00:00Z",
            "target_type": "agent", "target_id": "agent_deleted", "prompt": "go",
            "schedule_type": "interval", "interval_value": -5, "interval_unit": "minutes",
            "missed_run_policy": "skip", "last_run_at": None, "next_run_at": "2026-09-01T00:00:00Z",
        })
        resp = await client.patch("/api/schedules/sched_old", json={"enabled": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        # Turning it back on is where it would run, so that is checked.
        again = await client.patch("/api/schedules/sched_old", json={"enabled": True})
        assert again.status_code == 422
        assert (await client.get("/api/schedules/sched_old")).json()["enabled"] is False

    async def test_one_with_an_unreadable_cron_is_refused_on_not_a_500(self, client, manager):
        from core.store import schedules as schedule_store
        await schedule_store.save({
            "id": "sched_cron", "name": "Old cron", "enabled": False, "created_at": "2026-09-01T00:00:00Z",
            "target_type": "agent", "target_id": "agent_f197", "prompt": "go",
            "schedule_type": "cron", "cron_expression": "not a cron",
            "missed_run_policy": "skip", "last_run_at": None, "next_run_at": None,
        })
        resp = await client.patch("/api/schedules/sched_cron", json={"enabled": True})
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("That is not a valid cron expression")


class TestAStoredBadInterval:
    def test_never_puts_the_next_run_in_the_past(self):
        """-5 made every 30 s tick fire it; it is read as 1, as 0 already was."""
        from datetime import timedelta

        from core.scheduler import _utc_now, compute_next_run
        now = _utc_now()
        nxt = compute_next_run({"schedule_type": "interval", "interval_value": -5,
                                "interval_unit": "minutes"}, now)
        assert nxt == now + timedelta(minutes=1)


class TestTheBuilder:
    async def test_the_builder_cannot_make_one_that_fires_every_tick(self):
        import json

        from core.builder_tools import execute_builder_tool
        from core.store import schedules as schedule_store

        await S.seed_agents([S.make_agent(id="agent_f197b")])
        result = json.loads(await execute_builder_tool("create_schedule", {
            "name": "Every tick", "target_type": "agent", "target_id": "agent_f197b",
            "prompt": "go", "schedule_type": "interval", "interval_value": -5, "interval_unit": "minutes",
        }, None))
        assert "error" in result, result
        assert await schedule_store.load() == []

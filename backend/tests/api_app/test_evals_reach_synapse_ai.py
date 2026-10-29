"""F1255 (BLD1; TRI1's triage part 3): Evals are product tools, through the Evals screen's own code.

"Create an eval for my curiosity post generator" was built as a workflow copy of Evals: nothing could reach a rubric, a
golden set or a suite run. Now eight product tools do, each through `core.evals.service`, the code the screen's
routes call too: a rubric made, edited and attached to a workflow; cases proposed (never saved) and saved as approved;
a golden set run with what it costs said first, and stopped; a workflow's scores read. A host does each its own way
through `set_evals_host` (cloud's roles, plan counts and credit check).
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from core.evals import generate, service
from core.product_tools import areas, evals as E, registry
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

BUILDER = Caller(role=Role.BUILDER, via="assistant")
CRITERIA = [{"name": "Curiosity", "weight": 2, "guidance": "It makes the reader want to know more."},
            {"name": "Accuracy", "weight": 1}]


@pytest.fixture(autouse=True)
async def flow(client):
    areas.ensure_loaded()
    await S.seed_orchestrations([S.make_orchestration(id="orch_posts", name="Curiosity post generator")])
    yield "orch_posts"
    E.set_evals_host(None)


async def _call(name, args, caller=BUILDER, **kw):
    return await registry.invoke(name, caller, args, **kw)


async def _orch(orch_id="orch_posts"):
    from core.store.resources import get_orchestration

    return await get_orchestration(orch_id)


class TestTheTools:
    def test_they_are_a_builders_acts_and_two_reads(self):
        acts = ("save_rubric", "attach_rubric", "propose_eval_cases", "save_eval_cases", "start_eval_suite",
                "cancel_eval_suite")
        for name in acts:
            tool = registry.get(name)
            assert (tool.kind, tool.min_role, tool.scope, tool.area) == ("act", Role.BUILDER, "build", "evals"), name
        for name in ("list_rubrics", "read_eval_scores"):
            assert registry.get(name).kind == "read", name
        assert registry.get("start_eval_suite").confirmed, "a run spends: its person sees what it costs first"

    async def test_a_viewer_cannot_write_a_rubric(self):
        with pytest.raises(ToolRefusal, match="needs the builder role"):
            await _call("save_rubric", {"name": "Posts"}, Caller(role=Role.VIEWER, via="assistant"))


class TestARubric:
    async def test_it_is_made_and_attached_and_the_screen_lists_it(self, client):
        out = await _call("save_rubric", {"name": "Post quality", "criteria": CRITERIA,
                                          "workflow": "Curiosity post generator"})
        rubric = out["rubric"]
        assert (await _orch())["rubric_id"] == rubric["id"] == out["attached"]["rubric_id"]
        listed = (await client.get("/api/rubrics")).json()
        assert [r["name"] for r in listed] == ["Post quality"]

    async def test_an_edit_keeps_what_it_does_not_say_and_the_store_sets_the_version(self):
        made = (await _call("save_rubric", {"name": "Post quality", "criteria": CRITERIA}))["rubric"]
        edited = (await _call("save_rubric", {"rubric": "post QUALITY", "floor": 6}))["rubric"]
        assert edited["id"] == made["id"] and edited["version"] == made["version"], "a floor scores nothing anew"
        assert [c["name"] for c in edited["criteria"]] == ["Curiosity", "Accuracy"] and edited["floor"] == 6
        rescored = (await _call("save_rubric", {"rubric": made["id"], "criteria": CRITERIA[:1]}))["rubric"]
        assert rescored["version"] == made["version"] + 1, "new criteria are a new version"

    async def test_the_preview_says_what_is_scored_and_what_it_costs(self):
        preview = await E._preview_save_rubric(BUILDER, {"name": "Post quality", "criteria": CRITERIA,
                                                         "workflow": "orch_posts"})
        assert preview["criteria"] == "Curiosity (weight 2); Accuracy (weight 1); scored 0–10"
        assert "a judge call each" in preview["attaches_to"]

    async def test_what_the_form_refuses_is_refused_in_its_words(self):
        with pytest.raises(ToolRefusal, match="criteria"):
            await _call("save_rubric", {"name": "Posts", "criteria": [{"weight": 2}]})

    async def test_attach_names_one_that_is_here_or_takes_it_off(self):
        made = (await _call("save_rubric", {"name": "Post quality", "criteria": CRITERIA}))["rubric"]
        with pytest.raises(ToolRefusal, match="no rubric 'Nope'"):
            await _call("attach_rubric", {"workflow": "orch_posts", "rubric": "Nope"})
        await _call("attach_rubric", {"workflow": "orch_posts", "rubric": "Post quality"})
        assert (await _orch())["rubric_id"] == made["id"]
        await _call("attach_rubric", {"workflow": "orch_posts"})
        assert (await _orch())["rubric_id"] == ""

    async def test_an_unknown_workflow_is_said(self):
        with pytest.raises(ToolRefusal, match="no workflow 'Ghost'"):
            await _call("attach_rubric", {"workflow": "Ghost", "rubric": "x"})


class TestAGoldenSet:
    async def test_proposed_cases_are_never_saved(self, monkeypatch):
        async def propose(orch, rubric, settings, *, count, steer):
            return [{"name": "Teaser", "input": "Write a post about tides", "notes": ""}][:count]

        monkeypatch.setattr(generate, "propose", propose)
        out = await _call("propose_eval_cases", {"workflow": "orch_posts", "count": 1})
        assert out["saved"] is False and out["cases"][0]["input"] == "Write a post about tides"
        assert await service.list_suites() == []

    async def test_approved_cases_are_saved_whole_and_added_to_its_set(self):
        out = await _call("save_eval_cases", {"workflow": "orch_posts", "cases": [{"input": "About tides"}]})
        first = out["golden_set"]
        assert first["name"] == "Curiosity post generator golden set" and len(first["cases"]) == 1
        again = await _call("save_eval_cases", {"workflow": "orch_posts", "golden_set": first["id"],
                                                "cases": [{"input": "About owls"}]})
        assert [c["input"] for c in again["golden_set"]["cases"]] == ["About tides", "About owls"]

    async def test_more_than_a_set_holds_is_refused_never_cut(self):
        with pytest.raises(ToolRefusal, match="at most 100 cases"):
            await _call("save_eval_cases", {"workflow": "orch_posts",
                                            "cases": [{"input": f"case {i}"} for i in range(101)]})

    async def test_the_screen_s_own_route_still_keeps_the_first_hundred(self, client):
        resp = await client.post("/api/evals/suites", json={"name": "Big", "orchestration_id": "orch_posts",
                                                            "cases": [{"input": f"c{i}"} for i in range(101)]})
        assert resp.status_code == 200 and len(resp.json()["cases"]) == 100

    async def test_a_set_of_another_workflow_is_refused(self):
        await S.seed_orchestrations([S.make_orchestration(id="orch_posts", name="Curiosity post generator"),
                                     S.make_orchestration(id="orch_other", name="Other")])
        other = (await _call("save_eval_cases", {"workflow": "orch_other", "cases": [{"input": "x"}]}))["golden_set"]
        with pytest.raises(ToolRefusal, match="tests another workflow"):
            await _call("save_eval_cases", {"workflow": "orch_posts", "golden_set": other["id"],
                                            "cases": [{"input": "y"}]})


class TestARun:
    async def test_it_says_what_it_costs_before_it_starts(self):
        await _call("save_eval_cases", {"workflow": "orch_posts", "cases": [{"input": "a"}, {"input": "b"}]})
        preview = await E._preview_run_set(BUILDER, {"workflow": "orch_posts"})
        assert preview["costs"].startswith("2 runs of the workflow and 2 judge calls; it has not run yet")

    async def test_the_press_starts_it_through_the_service(self, monkeypatch):
        await _call("save_eval_cases", {"workflow": "orch_posts", "cases": [{"input": "a"}]})
        started = {}

        async def start(suite_id, module):
            started["suite"] = suite_id
            return {"suite_run_id": "srun_1", "status": "running", "estimate": {"runs": 1}}

        monkeypatch.setattr(service, "start_suite_run", start)
        out = await E._run_set(BUILDER, {"workflow": "orch_posts"})
        assert out["suite_run_id"] == "srun_1" and started["suite"].startswith("set_")

    async def test_a_set_with_no_cases_is_said(self):
        await service.save_suite({"name": "Empty", "orchestration_id": "orch_posts", "cases": []})
        with pytest.raises(ToolRefusal, match="no cases yet"):
            await E._preview_run_set(BUILDER, {"golden_set": "Empty"})


class TestScoresAndTheHost:
    async def test_a_workflow_s_eval_is_read_whole(self):
        await _call("save_rubric", {"name": "Post quality", "criteria": CRITERIA, "workflow": "orch_posts"})
        await _call("save_eval_cases", {"workflow": "orch_posts", "cases": [{"input": "a"}]})
        out = await _call("read_eval_scores", {"workflow": "Curiosity post generator"})
        assert out["rubric"]["name"] == "Post quality" and out["scores"] == [] and out["regressions"] == []
        assert [(s["name"], s["cases"]) for s in out["golden_sets"]] == [("Curiosity post generator golden set", 1)]

    async def test_a_host_s_own_way_is_used_on_every_door(self):
        seen = []

        async def save_rubric(caller, body, rubric_id):
            seen.append((caller.role, body["name"], rubric_id))
            raise ToolRefusal("The Solo plan includes 3 rubrics. Delete one, or upgrade.")

        E.set_evals_host(E.EvalsHost(save_rubric=save_rubric))
        with pytest.raises(ToolRefusal, match="includes 3 rubrics"):
            await _call("save_rubric", {"name": "Fourth"})
        assert seen == [(Role.BUILDER, "Fourth", None)]

"""A tool step's set inputs that say where it acts are shown, and a re-plan that changes one says so (F1245).

TRI1's turn 6 on prod (10-29): the re-plan swapped `https://synapseorch.com` for `https://synapseorchestrator.com`
in the scrape step, and the card said only "uses `scrape_url`", so neither Naveen nor the chat saw it; the saved
workflow scrapes the wrong site. Each tool step now shows its URL, host, path, address or target — never a value or
key that reads as a secret, and a value from the run as that — on the card, the panel, the last message and the
`workflow` topic alike (they all read `step_words`); and a re-plan lists what changed from the plan the person last
saw: this run's last card, or the parked plan CAP3's F1241 carries into the next turn (`plan_before`).
"""
from __future__ import annotations

from pathlib import Path

from core.native_builder import objects as O
from core.native_builder import parts_words as PW
from core.native_builder.build_plan import PLAN_BEFORE_KEY, plan_before_of, since_last
from core.native_builder.render import render

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "assistant"
TITLES = {"scrape_url": PW.title_of("scrape_url", "Read a web page and return its text.")}
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _plan(url: str, **args) -> O.Plan:
    return O.Plan.from_draft(O.PlanDraft(kind="create", title="Weekly post", summary="a post about our product", steps=[
        O.Step(id="scrape", type="tool", name="Read our site", does="reads the product page", tool="scrape_url",
               config={"tool_arguments": {"url": url, **args}}, next="end")]))


def _words(plan: O.Plan) -> str:
    steps, _parts, _off = PW.describe(plan, titles=TITLES)
    return steps["scrape"]


class TestTheInputsAreShown:
    def test_turn_6s_url_is_on_the_step(self):
        assert _words(_plan("https://synapseorch.com")) == (
            "read a web page and return its text (url: https://synapseorch.com)")

    def test_a_secret_never_is(self):
        said = _words(_plan("https://synapseorch.com", api_key="abc123", target=GITHUB_TOKEN,
                            authorization="Bearer xyz"))
        assert said == "read a web page and return its text (url: https://synapseorch.com)"

    def test_a_value_from_the_run_is_said_as_that(self):
        said = _words(_plan("{{state.site}}", to="{state.person.email}", query="{{state.topic}}"))
        assert said == "read a web page and return its text (url: from the run; to: from the run)"

    def test_what_is_not_a_place_is_left_to_the_step(self):
        said = _words(_plan("https://synapseorch.com", max_pages=3, mode="text", prompt="x" * 200))
        assert said == "read a web page and return its text (url: https://synapseorch.com)"

    def test_a_host_a_path_and_an_address_by_their_values(self):
        assert PW.shown_inputs({"q": "example.com", "dest": "/reports/weekly.md", "who": "ops@example.com",
                                "n": 2}) == {"q": "example.com", "dest": "/reports/weekly.md", "who": "ops@example.com"}

    def test_the_card_shows_it(self):
        plan = _plan("https://synapseorch.com")
        plan.step_words, plan.part_words, plan.off = PW.describe(plan, titles=TITLES)
        assert "(url: https://synapseorch.com)" in render(plan)


class TestARePlanSaysWhatChanged:
    def test_turn_6s_swap_is_listed(self):
        before = _plan("https://synapseorch.com").model_dump()
        assert since_last(_plan("https://synapseorchestrator.com"), before) == [
            '"Read our site": url https://synapseorch.com → https://synapseorchestrator.com']

    def test_a_tool_a_step_added_and_one_removed(self):
        before = _plan("https://synapseorch.com").model_dump()
        now = O.Plan.from_draft(O.PlanDraft(kind="create", title="Weekly post", summary="s", steps=[
            O.Step(id="scrape", type="tool", name="Read our site", does="d", tool="fetch_page",
                   config={"tool_arguments": {"url": "https://synapseorch.com"}}, next="post"),
            O.Step(id="post", type="tool", name="Post it", does="d", tool="post_tweet", next="end")]))
        assert since_last(now, before) == ['"Read our site" now uses `fetch_page`, not `scrape_url`',
                                           'Added the step "Post it"']
        assert since_last(_plan("https://synapseorch.com"), now.model_dump()) == [
            '"Read our site" now uses `scrape_url`, not `fetch_page`', 'Removed the step "Post it"']

    def test_a_step_renamed_and_moved_is_matched_by_its_id(self):
        before = _plan("https://synapseorch.com").model_dump()
        before["steps"][0]["name"] = "Scrape the site"
        assert since_last(_plan("https://synapseorch.com"), before) == []

    def test_nothing_before_says_nothing(self):
        assert since_last(_plan("https://synapseorch.com"), None) == []
        assert since_last(_plan("https://synapseorch.com"), {}) == []

    def test_the_card_lists_it_before_its_steps(self):
        plan = _plan("https://synapseorchestrator.com")
        plan.since_last = since_last(plan, _plan("https://synapseorch.com").model_dump())
        card = render(plan)
        assert ("## Changed from the last plan\n- \"Read our site\": url https://synapseorch.com → "
                "https://synapseorchestrator.com\n") in card
        assert card.index("## Changed from the last plan") < card.index("## Steps")


class TestWhichPlanIsTheLastOne:
    def test_this_runs_last_card_first_the_newest_they_saw(self):
        parked, card = {"title": "parked"}, {"title": "card"}
        state = {PLAN_BEFORE_KEY: {"plan": parked, "draft": "d1", "run_id": "run_a"}}
        assert plan_before_of(state, {"outcome": "review", "plan": card}) == card
        assert plan_before_of({}, {"outcome": "same", "plan": card}) == card

    def test_else_the_plan_a_revision_was_asked_of(self):
        parked = {"title": "parked"}
        state = {PLAN_BEFORE_KEY: {"plan": parked, "draft": "d1", "run_id": "run_a"}}
        assert plan_before_of(state, {"outcome": "ask", "plan": None}) == parked
        assert plan_before_of(state, {}) == parked

    def test_a_question_or_nothing_is_no_plan(self):
        assert plan_before_of({}, {"outcome": "ask", "plan": None}) is None
        assert plan_before_of({}, {}) is None
        assert plan_before_of({PLAN_BEFORE_KEY: "not an object"}, {}) is None

    def test_the_key_is_the_one_cap3s_f1241_sets(self):
        assert PLAN_BEFORE_KEY == "plan_before"


class TestThePanelShowsIt:
    def test_plan_ts_reads_since_last(self):
        text = (FRONTEND / "plan.ts").read_text()
        assert "since_last?: string[];" in text and "changed: bullets(view.plan.since_last)" in text

    def test_the_card_draws_it_under_its_label(self):
        text = (FRONTEND / "cards.tsx").read_text()
        assert "Changed from the last plan" in text and "{plan.changed}" in text


class TestARevisionKeepsTheCardItRevises:
    async def test_change_something_records_it(self):
        import json

        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live

        card = _plan("https://synapseorch.com").model_dump()
        run = OrchestrationRun(run_id="run_f1245", orchestration_id="orch_native_builder", shared_state={
            "plan_checked": json.dumps({"object": "plan", "outcome": "review", "plan": card, "draft": "d1",
                                        "offers": ["build", "change", "stop"]}),
            "review_feedback": json.dumps({"feedback": "post it to Twitter too"})})
        with live(run):
            raw, _ = await execute_builder_tool_with_events("read_plan_answer", {}, None, run_id=run.run_id)
        assert json.loads(raw)["outcome"] == "revise"
        assert run.shared_state[PLAN_BEFORE_KEY] == {"plan": card, "draft": "d1", "run_id": "run_f1245"}

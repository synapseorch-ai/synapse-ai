"""The grounded judge (V1, D168): a plain answer from the served passages, B1b's quote check for free, and a
verdict from a judge of another model family against the "Grounded in cited sources" rubric. The models are
fakes ($0). SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from _fakes.knowledge_eval import QUESTIONS, finished, policies, question_set
from core import usage_tracker
from core.evals import store as rubrics
from core.knowledge import dispatch, evals_judge as J, evals_runs as RUNS, seams, snapshots
from core.knowledge.api_models import EvalJudgeRequest, EvalRunRequest, KnowledgeBaseUpdate
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher

pytestmark = pytest.mark.usefixtures("knowledge_store")
JUDGE = {"answer_model": "gpt-4o-mini", "judge_model": "claude-haiku-4-5"}


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    yield
    for task in list(RUNS.tasks().values()):
        task.cancel()
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


class Models:
    """The answer model quotes the first passage it was shown under its label; the judge scores 8."""

    def __init__(self, *, answer=None, verdict=None):
        self.calls: list[dict] = []
        self.answer = answer
        self.verdict = verdict

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["prompt_msg"]
        if kwargs["agent_id"] == "knowledge_eval_answerer":
            if self.answer is not None:
                return self.answer
            block = re.search(r"\[K1\][^\n]*\n(.+?)(?:\n\n\[K|\n<<END)", prompt, re.S).group(1)
            sentence = next(s for s in re.split(r"(?<=[.!?])\s|\n", block.strip()) if len(s.split()) >= 6)
            return f'The policy says "{sentence.strip().rstrip(".")}" [K1].'
        return self.verdict or json.dumps({"score": 8, "reasoning": "Supported.",
                                           "criteria": {"crit_supported": {"score": 9, "reasoning": "ok"},
                                                        "invented": {"score": 1, "reasoning": "?"}},
                                           "steps": {"<two sentences>": {"score": 0}}})


async def judged_run(kb, made, models, judge=JUDGE):
    started = await RUNS.start(kb, EvalRunRequest(set_id=made.id, judge=EvalJudgeRequest(**judge)))
    await finished()
    return await RUNS.get_run(kb.id, started.id)


class TestFamilies:
    @pytest.mark.parametrize("model,family", [
        ("claude-sonnet-4-5", "anthropic"),
        ("bedrock.us.anthropic.claude-3-5-haiku-20241022-v1:0", "anthropic"),
        ("synapse.claude-sonnet-4", "anthropic"),
        ("synapse.nova-pro", "amazon"),
        ("gpt-4o", "openai"),
        ("oaic.gpt-4o-mini", "openai"),
        ("o3-mini", "openai"),
        ("gemini-2.5-flash", "google"),
        ("llama3.1:8b", "meta"),
        ("mistral-large-latest", "mistral"),
        ("mystery-7", None),
    ])
    def test_a_models_family_is_read_past_its_routing_prefix(self, model, family):
        assert J.family_of(model) == family

    def test_a_judge_of_the_answer_models_own_family_is_refused(self):
        with pytest.raises(ValueError, match="another family"):
            J.check_families(EvalJudgeRequest(answer_model="claude-sonnet-4-5",
                                              judge_model="bedrock.us.anthropic.claude-3-5-haiku-20241022-v1:0"))
        assert J.check_families(EvalJudgeRequest(**JUDGE)) is None
        assert "Could not tell" in J.check_families(EvalJudgeRequest(answer_model="mystery-7", judge_model="gpt-4o"))


class TestQuoteCheck:
    def _hits(self, *texts):
        return [SimpleNamespace(label=f"[K{i + 1}]", context="", content=t) for i, t in enumerate(texts)]

    def test_quotes_found_misquotes_and_labels_no_passage_has(self):
        hits = self._hits("Refunds arrive in five working days.", "Support answers within one business day.")
        answer = ('Refunds "arrive in five working days" [K1], and support answers "within two business days" '
                  "[K2]. See also [K9].")
        check = J.quote_check(answer, hits)
        assert (check["labels"], check["unresolved"], check["quotes"], check["matched"]) == ([1, 2, 9], [9], 2, 1)


class TestAJudgedRun:
    async def test_each_question_gets_an_answer_a_check_and_a_verdict(self, jobs, monkeypatch):
        models = Models()
        monkeypatch.setattr("core.llm_providers.generate_response", models)
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await judged_run(kb, made, models)
        assert run.status == "completed", run.error
        judge = run.judge
        assert (judge.answer_model, judge.judge_model, judge.judged, judge.failed) == ("gpt-4o-mini",
                                                                                      "claude-haiku-4-5", 3, 0)
        assert judge.mean_score == 8.0 and judge.rubric_id == J.RUBRIC_ID and judge.rubric_version == 1
        assert judge.quotes == 3 and judge.quotes_matched == 3 and judge.unresolved_labels == 0
        case = run.cases[0]
        assert case.answer.startswith("The policy says") and case.verdict.score == 8.0
        assert set(case.verdict.per_criterion) == {"crit_supported"}  # an invented criterion is dropped
        answers = [c for c in models.calls if c["agent_id"] == "knowledge_eval_answerer"]
        verdicts = [c for c in models.calls if c["agent_id"] == "knowledge_eval_judge"]
        assert len(answers) == len(verdicts) == 3
        assert {c["current_model"] for c in answers} == {"gpt-4o-mini"}
        assert {c["session_id"] for c in models.calls} == {f"knowledge-eval:{run.id}"}
        assert all(c["source"] == "evals" for c in models.calls)
        assert QUESTIONS[0][1] in verdicts[0]["prompt_msg"] and "crit_supported" in verdicts[0]["prompt_msg"]

    async def test_the_answer_is_drawn_only_from_what_an_agent_would_get(self, jobs, monkeypatch):
        models = Models()
        monkeypatch.setattr("core.llm_providers.generate_response", models)
        kb = await policies(jobs, retrieval={"top_k": 1})
        made = await question_set(kb)
        await judged_run(kb, made, models)
        answer_prompts = [c["prompt_msg"] for c in models.calls if c["agent_id"] == "knowledge_eval_answerer"]
        assert answer_prompts and all(p.startswith("1 passage from Policies.") for p in answer_prompts)

    async def test_a_misquote_and_a_failed_judge_are_said(self, jobs, monkeypatch):
        models = Models(answer='It says "refunds take ninety weeks" [K1].', verdict="I cannot score this.")
        monkeypatch.setattr("core.llm_providers.generate_response", models)
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await judged_run(kb, made, models)
        assert run.status == "completed"
        assert (run.judge.quotes, run.judge.quotes_matched, run.judge.failed, run.judge.mean_score) == (3, 0, 3, None)
        assert run.cases[0].verdict.error == "The judge's reply had no score."

    async def test_the_rubric_is_a_normal_one_a_person_can_edit(self, jobs, monkeypatch):
        monkeypatch.setattr("core.llm_providers.generate_response", Models())
        kb = await policies(jobs)
        made = await question_set(kb)
        await judged_run(kb, made, Models())
        stored = await rubrics.get_rubric(J.RUBRIC_ID)
        assert stored["name"] == "Grounded in cited sources" and len(stored["criteria"]) == 3
        stored["criteria"][0]["guidance"] = "Stricter."
        await rubrics.save_rubric(stored)
        run = await judged_run(kb, made, Models())
        assert run.judge.rubric_version == 2

    async def test_a_same_family_judge_is_refused_before_anything_runs(self, jobs, client):
        kb = await policies(jobs)
        made = await question_set(kb)
        with pytest.raises(ValueError, match="another family"):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id, judge=EvalJudgeRequest(
                answer_model="gpt-4o", judge_model="oaic.gpt-4o-mini")))
        same = {"answer_model": "gpt-4o", "judge_model": "o3-mini"}
        resp = await client.post(f"/api/knowledge/bases/{kb.id}/eval/runs", json={"set_id": made.id, "judge": same})
        assert resp.status_code == 422 and "both OpenAI models" in resp.json()["detail"]
        priced = await client.post(f"/api/knowledge/bases/{kb.id}/eval/estimate",
                                   json={"run": {"set_id": made.id, "judge": same}})
        assert priced.status_code == 422 and "another family" in priced.json()["detail"]  # said before Run
        assert RUNS.tasks() == {}

    async def test_answers_and_verdicts_are_priced_but_never_against_the_budget(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        monkeypatch.setattr(usage_tracker, "_pricing_snapshot",
                            {"gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.6}})
        est = await RUNS.estimate(kb, EvalRunRequest(set_id=made.id, judge=EvalJudgeRequest(**JUDGE)))
        parts = {line.part: line for line in est.lines}
        assert set(parts) == {"search", "answers", "judge"}
        assert parts["answers"].est_usd > 0 and not parts["answers"].counts_toward_budget
        assert parts["judge"].est_usd is None and est.unpriced == ["claude-haiku-4-5"]

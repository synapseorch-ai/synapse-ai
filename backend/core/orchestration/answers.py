"""
Answering a paused run — from the browser or from a messaging channel.

One implementation for both, so a channel reply resumes a run exactly the way
the app's Answer control does (F54): a run a worker ran goes back to the fleet
as `resume_orchestration_job`, with the answer as the job's argument; any other
run is resumed in this process.

**A run is answered once.** A run that is not paused on a question is refused
(`RunNotWaiting`): the browser and a channel can both answer the same question,
and before this the engine had no such guard — a second answer was written into
whatever step the run had reached by then.

**And once when two answers arrive together (F136).** Both used to pass the
check above — here, across the `await` that reads the run; on the fleet, in two
jobs picked up in one poll — and both drove the run from the same checkpoint.
In this process the run is claimed before the first `await`; on the fleet the
worker claims it with one conditional UPDATE (`state_store.claim_answer`).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from core.orchestration.human_fields import BadAnswer, check_answer  # noqa: F401 — BadAnswer for the doors


class RunNotWaiting(Exception):
    """The run is not paused on a question. `status` is None when there is no such run."""

    def __init__(self, run_id: str, status: Optional[str]):
        self.run_id = run_id
        self.status = status
        super().__init__(
            f"Run {run_id} not found" if status is None
            else f"Run {run_id} is {status} and is not waiting for an answer"
        )


class QuestionNotAsked(Exception):
    """An answer names a question the run does not ask now (TEAM1e): answered already, or a later pause's."""

    def __init__(self, question_id: str):
        self.question_id = question_id
        super().__init__(f"The question {question_id} is not asked now: it was answered, or the run moved on.")


def questions_asked(run: Any) -> list[dict]:
    """The questions `run` is paused on, each with its own id, when a team's step asks several (TEAM1e); [] for any
    other pause, whose one question needs no id."""
    from core.orchestration.pauses import asking_step_id

    asker = asking_step_id(run.step_history or [], run.current_step_id)
    pending = ((run.step_progress or {}).get(asker) or {}).get("pending_approval") or {}
    return [dict(q) for q in pending.get("questions") or []]


def check_question(run: Any, question_id: str | None) -> None:
    """Raise `QuestionNotAsked` unless `question_id` is one `run` asks now (None names its only, or first, one)."""
    if not question_id:
        return
    asked = questions_asked(run)
    if question_id not in {q.get("question_id") for q in asked}:
        raise QuestionNotAsked(question_id)


class EmptyAnswer(ValueError):
    """An answer with nothing in it (F232)."""


EMPTY_ANSWER = "The answer is empty. Send text, or an object keyed by the question's field names."


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == {}


async def refuse_card_for_browser(run: Any, response: Any) -> None:
    """A card number never reaches a Browser agent through an answer (F1100): it is the person's to
    type in the browser, in their turn. Raises `BadAnswer`. Other steps are untouched: a workflow
    that collects card digits for its own tools keeps doing so."""
    from core.browser.gate import CARD_WORDS, looks_like_card, strings_in

    if not any(looks_like_card(text) for text in strings_in(response)):
        return
    if await _waits_on_browser_agent(run):
        from core.orchestration.human_fields import BadAnswer
        raise BadAnswer("card", CARD_WORDS)


async def _waits_on_browser_agent(run: Any) -> bool:
    from core.store.resources import get_agent, get_orchestration
    try:
        orch = await get_orchestration(getattr(run, "orchestration_id", "") or "")
        step = next((s for s in (orch or {}).get("steps") or [] if s.get("id") == getattr(run, "current_step_id", None)),
                    None)
        agent = await get_agent(step.get("agent_id")) if step and step.get("agent_id") else None
    except Exception:
        return False
    return bool(agent) and (agent.get("type") == "browser" or bool(agent.get("browser")))


def refuse_empty(response: Any) -> None:
    """Raise `EmptyAnswer` for an answer that says nothing (F232).

    v1's resume defaulted a missing `response` to `{}`, so a caller that
    misspelt the field answered a person's question with nothing and the run
    carried on. Blank text, an empty mapping, and text wrapped the way v1 and
    MCP wrap it (`{"response": <blank>}`) say nothing. A mapping whose fields are
    blank does say something — it is what a form sends for a fielded question
    nobody filled in.

    Every door refuses it: `answer_paused_run` calls it first (the app's Answer
    control, a channel reply, v1, MCP's `answer_run`), and v2, which queues its
    own job, calls it itself. Since F842 (Naveen, 2026-10-23, superseding his
    2026-09-26 call that a page may send one): on prod an empty "Send answer"
    resumed a run past its approval, which read "" as declined.
    """
    if isinstance(response, dict) and "question_id" in response:
        # A team question's answer names its question inside the mapping (TEAM1e): that is
        # not an answer, so `{"question_id": q, "response": " "}` says nothing either (F842).
        response = {k: v for k, v in response.items() if k != "question_id"}
    if _blank(response) or (
        isinstance(response, dict) and list(response) == ["response"] and _blank(response["response"])
    ):
        raise EmptyAnswer(EMPTY_ANSWER)


class RunAlreadyActive(Exception):
    """Another answer already has this run: a pump driving it in this process,
    an answer being applied here, or one just queued for the fleet."""


@dataclass
class Answered:
    #: True when a worker will resume the run; its events are on the run's Redis stream.
    on_fleet: bool
    #: The in-process run's event queue, when resumed here.
    queue: Optional[asyncio.Queue] = None
    #: An answer to one of a team's questions given while its step works (TEAM1e): nothing is resumed; the member
    #: carries on ("applied"), or the answer waits for the team ("held"; "sent" on the fleet, read by its job).
    live: Optional[str] = None


async def worker_run_row(app_state: Any, run_id: str):
    """The run's row when a worker ran it and the scale layer is up, else None.

    A run a worker ran has `worker_id` set; its state is in the store, so any
    worker can pick it up again, and it goes back to the fleet rather than
    running inside the API process.
    """
    redis = getattr(app_state, "redis", None)
    arq_redis = getattr(app_state, "arq_redis", None)
    pg_session_factory = getattr(app_state, "pg_session_factory", None)
    if not (redis and arq_redis and pg_session_factory):
        return None
    from sqlalchemy import select

    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with pg_session_factory() as session:
        row = (await session.execute(
            select(
                OrchestrationRunDB.worker_id,
                OrchestrationRunDB.status,
                OrchestrationRunDB.current_step_id,
                OrchestrationRunDB.waiting_for_human,
                OrchestrationRunDB.step_history,
                OrchestrationRunDB.human_fields,
                # A team step's questions (TEAM1e): an answer names one the run asks.
                OrchestrationRunDB.step_progress,
            ).where(
                OrchestrationRunDB.run_id == run_id,
                OrchestrationRunDB.tenant_id == get_tenant(),
            )
        )).one_or_none()
    return row if row is not None and row.worker_id else None


def _waiting(status: Optional[str], waiting_for_human: Any) -> bool:
    return status == "paused" and bool(waiting_for_human)


#: Runs an answer is being applied to in this process, from before the answer's
#: first `await` until its pump is registered in `runner._active_tasks`. Taken
#: and checked with no `await` between, which is the whole of its correctness:
#: the event loop cannot hand a second answer the run in between. A run is
#: resumed in-process only by the process that holds it, so this set is enough
#: here; several processes are the fleet's case, and the fleet claims the row.
_answering: set[str] = set()


async def _held_for(run_id: str, response: Any) -> list:
    """The workspace's secrets in an answer to any run (F1307): a builder run's and a workflow's Human step's alike
    (Naveen, 11-03: a held value pasted into a workflow's answer is a mistake, and the workflow has it as a variable or
    a connection already)."""
    from core.secret_scrub import held_in

    return await held_in(json.dumps(response, default=str, ensure_ascii=False))


async def _card_up(run_id: str, state: dict | None = None) -> bool:
    """Whether a builder run put up a Connect card (its turn's own card, F1345), or its plan asks for a key on one after
    the yes (F1387; `state`, the run's, when the caller holds it); never asked of any other run."""
    from core.native_builder.seed import BUILDER_RUN_PREFIX

    if not str(run_id).startswith(BUILDER_RUN_PREFIX):
        return False
    from core.native_builder.turn_facts import card_up

    return await card_up(run_id, state)


def _held_out(value: Any, held: Any) -> Any:
    """`value` with every held secret in its strings taken out by its exact value (F1307)."""
    from core.native_builder.door import _without_held

    if isinstance(value, str):
        return _without_held(value, held)[0]
    if isinstance(value, dict):
        return {k: _held_out(v, held) for k, v in value.items()}
    if isinstance(value, list):
        return [_held_out(v, held) for v in value]
    return value


def _through_door(run_id: str, response: Any, question: Optional[str], step: Optional[str] = None,
                  held: Any = (), card_up: bool = False) -> Any:
    """An answer to an AI builder run goes through the builder's door (BWA1, D274), whichever way it arrives (the
    run page, v1, the MCP server, a channel): a card number or a password is refused as a bad answer, before
    anything is queued or kept, and a key is masked. Read with the question it answers, so a lone secret-looking
    word in reply to "what's the password?" counts as one, and one at a Connect card's wait (`step`) or in a run
    that put a Connect card up (`card_up`, F1345) as a key (F1280); a secret the workspace holds is masked by its
    value (`held`, F1307). Any other run's answer — a
    workflow's own Human step — loses only the held secrets (Naveen, 11-03): its other words are the workflow's
    business, as F1100 keeps a workflow's own card digits."""
    from core.native_builder.seed import BUILDER_RUN_PREFIX

    if not str(run_id).startswith(BUILDER_RUN_PREFIX):
        return _held_out(response, held) if held else response
    from core.native_builder import door

    try:
        masked, _removed = door.check_answer(response, question=question, step=step, held=held, card_up=card_up)
    except door.Refused as exc:
        raise BadAnswer("answer", str(exc)) from None
    return masked


def _verdict(response: Any, question_id: str) -> dict:
    """One team question's answer, as TEAM1c's mailbox holds it."""
    from core.orchestration.engine import OrchestrationEngine

    approve, reason = OrchestrationEngine._approved(response)
    return {"question_id": question_id, "approve": approve, "reason": reason}


def _named(response: Any, question_id: str | None) -> Any:
    """The answer with the question it answers, for the engine's resume (`_resume_tool_approval`)."""
    if not question_id:
        return response
    return {**(response if isinstance(response, dict) else {"response": response}), "question_id": question_id}


async def _answer_live(app_state: Any, run_id: str, response: Any, question_id: str, *, on_fleet: bool) -> Answered:
    """A team's question answered while its step works (TEAM1e; TEAM1c's mailbox): that member carries on, or the
    answer waits for the team — taken once, when it starts or before it pauses. Nothing is resumed."""
    from core import team as _team

    verdict = _verdict(response, question_id)
    if on_fleet:
        from core.scale.pubsub import publish_member_answer
        await publish_member_answer(app_state.redis, _team.run_mailbox(run_id), verdict)
        return Answered(on_fleet=True, live="sent")
    from core.chat_approval import post_answer
    return Answered(on_fleet=False, live=post_answer(_team.run_mailbox(run_id), _team.RUN_KEY, verdict))


_WORKING = ("running", "pending", "queued")


#: A key an answer might carry to claim a door (F1303): never believed, taken out at the door.
_CLAIMED = ("_answered_in_app",)
#: Where a fleet resume's door is kept, by the job's own id, beside the job rather than as its argument: a worker
#: older than the door never sees it and grants nothing, so the API and the workers deploy in any order (79).
DOOR_KEY = "synapse:resume-door:{job_id}"
DOOR_TTL_SECONDS = 86400


async def keep_door(redis: Any, job_id: str, door: str | None) -> None:
    if door and redis is not None:
        await redis.set(DOOR_KEY.format(job_id=job_id), door, ex=DOOR_TTL_SECONDS)


async def door_of(redis: Any, job_id: str | None) -> str | None:
    """The door kept for `job_id`, read once (a redelivered job grants nothing); None for none."""
    if not job_id or redis is None:
        return None
    try:
        got = await redis.getdel(DOOR_KEY.format(job_id=job_id))
    except Exception:  # noqa: BLE001 — a door that cannot be read grants nothing
        return None
    return got.decode() if isinstance(got, bytes) else got


async def answer_paused_run(app_state: Any, run_id: str, response: Any, *,
                            question_id: str | None = None, door: str | None = None) -> Answered:
    """Resume `run_id` with `response`, on the fleet or in this process.

    `question_id` names one of a team step's questions (TEAM1e): it must be one the run asks now. Given while the
    step works — another question of the pause was answered first — it reaches that member without a resume.

    Raises `RunNotWaiting` for a run that is not paused on a question,
    `RunAlreadyActive` when this process is already running it, `QuestionNotAsked`
    for a question the paused run does not ask, `EmptyAnswer` for an answer that
    says nothing (F842), and `human_fields.BadAnswer` for an answer to an email,
    date, phone or dropdown field that is not one (F455), before anything is
    queued or resumed.

    `door` is where the answer came through, the server's to say (F1303): "app" (the app's own Answer
    control), "browser" (the pane), "api", "mcp", "channel". It rides beside the answer to the resume and is
    never written into it; a browser turn is granted only from a door the engine names (`GRANTING_DOORS`).
    """
    if isinstance(response, dict) and any(k in response for k in _CLAIMED):
        response = {k: v for k, v in response.items() if k not in _CLAIMED}
    refuse_empty(response)
    try:
        row = await worker_run_row(app_state, run_id)
    except Exception:  # noqa: BLE001 — the store for the fleet is unreachable; treat as local
        row = None

    if row is not None:
        if not _waiting(row.status, row.waiting_for_human):
            if question_id and row.status in _WORKING:
                return await _answer_live(app_state, run_id, response, question_id, on_fleet=True)
            raise RunNotWaiting(run_id, row.status)
        check_question(row, question_id)
        response = _through_door(run_id, response, getattr(row, "human_prompt", None),
                                 getattr(row, "current_step_id", None), await _held_for(run_id, response),
                                 await _card_up(run_id))
        response = check_answer(row.human_fields, response)
        await refuse_card_for_browser(row, response)
        from core.orchestration.pauses import pause_ordinal
        from core.scale.config import QUEUE_NAME

        resp = _named(response if isinstance(response, dict) else {"response": response}, question_id)
        job_id = f"resume_{run_id}_{int(time.time() * 1000)}"
        await keep_door(app_state.arq_redis, job_id, door)
        job = await app_state.arq_redis.enqueue_job(
            "resume_orchestration_job",
            run_id=run_id,
            human_response=resp,
            # The question this answer is for: a job that finds the run asking
            # another does not answer it (F111).
            pause_ordinal=pause_ordinal(row.step_history),
            _queue_name=QUEUE_NAME,
            _job_id=job_id,
        )
        if job is None:
            # ARQ keeps one job per id and answers None for a second: an answer
            # to this run was queued this same millisecond. It was dropped, so
            # it must not be reported as queued.
            raise RunAlreadyActive(run_id)
        return Answered(on_fleet=True)

    from core.orchestration.runner import _active_tasks, spawn_engine_run
    from core.orchestration.state import SharedState

    # Two pumps for one run would interleave its journal and duplicate work.
    # Checked and claimed with no `await` between (F136).
    active = _active_tasks.get(run_id)
    if question_id and active is not None and not active.done():
        # Its step works on (another of its questions was answered first): this one goes to its team (TEAM1e).
        return await _answer_live(app_state, run_id, response, question_id, on_fleet=False)
    if run_id in _answering or (active is not None and not active.done()):
        raise RunAlreadyActive(run_id)
    _answering.add(run_id)
    try:
        try:
            run = (await SharedState.restore(run_id)).run
        except FileNotFoundError:
            raise RunNotWaiting(run_id, None)
        if not _waiting(run.status, run.waiting_for_human):
            raise RunNotWaiting(run_id, run.status)
        check_question(run, question_id)
        response = _through_door(run_id, response, run.human_prompt, run.current_step_id,
                                 await _held_for(run_id, response), await _card_up(run_id, run.shared_state))
        response = check_answer(run.human_fields, response)
        await refuse_card_for_browser(run, response)
        response = _named(response, question_id)

        from core.orchestration.engine import OrchestrationEngine

        # Registers the pump in `_active_tasks` before it returns, so the claim
        # can be let go as soon as it has.
        _task, queue = spawn_engine_run(
            OrchestrationEngine.resume(run_id, response, app_state.server_module, door=door), run_id
        )
    finally:
        _answering.discard(run_id)
    return Answered(on_fleet=False, queue=queue)

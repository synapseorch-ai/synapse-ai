"""After Synapse AI builds something, the person is taken to it — F1577 (BLD5; Naveen 11-13, in-tab, through eb).

A build ended in the conversation that asked for it: a green "Workflow … created" row and the summary, and the person
then went to find what was built. Naveen's rule: a new workflow takes them to its page, where Run is; an agent made on
its own (or agents) to Chat with it; both to the workflow; a change to what it changed, unless they are on it already.
The conversation stays where it was, and a build that fails or is stopped moves nobody.

The way there is a message of its own under what the turn said (`go_to`), made by the reducer both products share
(`lib/builderEvents.ts`, synced into cloud) once the server says the run completed, from the writes the turn's
`resource_saved` events reported: never on a plan still waiting (it has written nothing), never for a run that failed
or was stopped, never for a write that was taken back. The same rule draws it on a chat read back from its runs
(`lib/builderReattach.ts`), so someone who left while it built finds the way there when they come back. What moves the
person is the host's: it presses that message's own link (`components/assistant/GoToCard.tsx`), and only when the
build ended live where they asked from (`ui-smoke`'s builtgoes, builtstays and builttab drive it in a browser).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
EVENTS = SRC / "lib/builderEvents.ts"
REATTACH = SRC / "lib/builderReattach.ts"

#: Node resolves the app's `@/` imports to `frontend/src`, and a relative import without its extension to the `.ts`
#: beside it, as the bundler does: what the modules import at run time is what is tested.
HOOKS = r"""
import { existsSync } from 'node:fs';
import { dirname, resolve as join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const SRC = %s;
export async function resolve(specifier, context, next) {
  let base = null;
  if (specifier.startsWith('@/')) base = join(SRC, specifier.slice(2));
  else if (specifier.startsWith('.') && context.parentURL && !/\.[cm]?[jt]sx?$/.test(specifier))
    base = join(dirname(fileURLToPath(context.parentURL)), specifier);
  if (base) for (const ext of ['.ts', '.tsx', '/index.ts']) {
    if (existsSync(base + ext)) return next(pathToFileURL(base + ext).href, context);
  }
  return next(specifier, context);
}
"""


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the panel's reducer is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(script: str):
        program = (f"import * as E from {json.dumps(EVENTS.as_uri())};\n"
                   f"import * as R from {json.dumps(REATTACH.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


def W(kind, action, id, name=None, **more):
    return {"kind": kind, "action": action, "id": id, "name": name or id, **more}


PUT_BACK = "put back as it was: the build stopped before it was saved whole: a step failed"


# ── where a turn's writes take the person ────────────────────────────────────

@pytest.mark.parametrize("writes, place", [
    # A new workflow: its page, where Run is.
    ([W("orchestration", "created", "orch_a", "Invoices")],
     {"kind": "workflow", "id": "orch_a", "name": "Invoices", "made": True}),
    # Only an agent: Chat with it.
    ([W("agent", "created", "agent_r", "Researcher")],
     {"kind": "agent", "id": "agent_r", "name": "Researcher", "made": True}),
    # Both: the workflow, whatever came first.
    ([W("agent", "created", "agent_r"), W("tool", "created", "tool_x"), W("orchestration", "created", "orch_a"),
      W("schedule", "created", "sched_1")],
     {"kind": "workflow", "id": "orch_a", "name": "orch_a", "made": True}),
    # Inner workflows are made before the one that runs them (build_save.save_calls): the last one is it.
    ([W("orchestration", "created", "orch_inner"), W("orchestration", "created", "orch_main")],
     {"kind": "workflow", "id": "orch_main", "name": "orch_main", "made": True}),
    # Members are made before their team: the team is the one to talk to.
    ([W("agent", "created", "agent_member"), W("agent", "created", "agent_team")],
     {"kind": "agent", "id": "agent_team", "name": "agent_team", "made": True}),
    # A change: what it changed, which was there before.
    ([W("orchestration", "updated", "orch_a", "Invoices")],
     {"kind": "workflow", "id": "orch_a", "name": "Invoices", "made": False}),
    ([W("agent", "updated", "agent_r", "Researcher")],
     {"kind": "agent", "id": "agent_r", "name": "Researcher", "made": False}),
    # Made, then given its settings: still made.
    ([W("orchestration", "created", "orch_a"), W("orchestration", "updated", "orch_a")],
     {"kind": "workflow", "id": "orch_a", "name": "orch_a", "made": True}),
    # A change to an agent and a workflow: the workflow.
    ([W("orchestration", "updated", "orch_a"), W("agent", "updated", "agent_r")],
     {"kind": "workflow", "id": "orch_a", "name": "orch_a", "made": False}),
    # A build that stopped half way took what it made back, saying why (F1281): nothing stands.
    ([W("agent", "created", "agent_r"), W("orchestration", "created", "orch_a"),
      W("orchestration", "deleted", "orch_a", reason="the build stopped before it was saved whole: a step failed"),
      W("agent", "deleted", "agent_r", reason="the build stopped before it was saved whole: a step failed")],
     None),
    # An update put back as it was is no change to go and see.
    ([W("orchestration", "updated", "orch_a"), W("orchestration", "updated", "orch_a", reason=PUT_BACK)], None),
    # Its workflow taken back, the agent the build kept stands: Chat with it.
    ([W("agent", "created", "agent_r"), W("orchestration", "created", "orch_a"),
      W("orchestration", "deleted", "orch_a", reason="it never passed validation")],
     {"kind": "agent", "id": "agent_r", "name": "agent_r", "made": True}),
    # A workflow asked to be deleted is nowhere to go.
    ([W("orchestration", "deleted", "orch_a")], None),
    # Nothing else a build writes has a page this takes anyone to.
    ([W("tool", "created", "tool_x"), W("schedule", "created", "s"), W("skill", "updated", "k"),
      W("knowledge_base", "created", "kb"), W("settings", "updated", None)], None),
    ([], None),
    # A write with no id names nothing.
    ([W("orchestration", "created", None)], None),
])
def test_where_a_turns_writes_take_the_person(node, writes, place):
    assert node(f"return E.placeBuilt({json.dumps(writes)});") == place


# ── the reducer: the way there, once the run says it completed ───────────────

#: The reducer over a turn: the person's message, then `events` in order, each numbered as a stream numbers them;
#: `again` reads the same events a second time, as a reattaching stream replays them.
REDUCE = """
const start = { ...E.emptyBuilderState([%(welcome)s, ...%(before)s,
  { role: 'user', content: 'Go ahead', kind: 'text', key: 'user-9' }]) };
let state = start;
const events = %(events)s;
for (let pass = 0; pass < %(passes)d; pass++) events.forEach((e, i) => { state = E.applyBuilderEvent(state, e, 1000 + i); });
return state.messages.slice(start.messages.length).map((m) => ({ kind: m.kind, key: m.key, place: m.place ?? null,
  content: m.content }));
"""
WELCOME = "{ role: 'assistant', kind: 'text', key: 'welcome', content: 'Hi' }"
RUN = "builder_f1577"


def _reduce(node, events, *, before="[]", passes=1):
    return node(REDUCE % {"welcome": WELCOME, "before": before, "events": json.dumps(events), "passes": passes})


def _saved(kind, action, id, name=None, **more):
    return {"type": "resource_saved", "kind": kind, "action": action, "id": id, "name": name or id, **more}


def _done(status="completed", summary='Built the workflow "Invoices".'):
    return {"type": "orchestration_complete", "run_id": RUN, "status": status, "final_state": {"summary": summary}}


BUILD = [{"type": "orchestration_start", "run_id": RUN},
         {"type": "step_start", "orch_step_id": "step_save", "step_name": "Save the Build", "step_type": "tool"},
         _saved("agent", "created", "agent_r", "Reader"),
         _saved("orchestration", "created", "orch_a", "Invoices"),
         {"type": "step_complete", "orch_step_id": "step_save"}]


def _ways(messages):
    return [m for m in messages if m["kind"] == "go_to"]


def test_a_build_that_completed_shows_the_way_to_its_workflow_under_what_it_said(node):
    out = _reduce(node, [*BUILD, _done()])

    ways = _ways(out)
    assert [w["place"] for w in ways] == [{"kind": "workflow", "id": "orch_a", "name": "Invoices", "made": True}]
    assert ways[0]["key"] == f"go-to-{RUN}"
    said = [m["kind"] for m in out if m["kind"] in ("text", "go_to")]
    assert said == ["text", "go_to"], f"the way there is not under the summary: {said}"


def test_a_replayed_stream_keeps_one_way_there(node):
    # A reattaching stream reads the run from its first event again: the message keeps its key, so it is redrawn.
    assert len(_ways(_reduce(node, [*BUILD, _done()], passes=2))) == 1


def test_a_plan_waiting_has_no_way_anywhere(node):
    # A plan preview has written nothing: nobody moves while the person reads it.
    plan = [{"type": "orchestration_start", "run_id": RUN},
            {"type": "human_input_required", "run_id": RUN, "orch_step_id": "step_human_review",
             "prompt": "Here is the plan.", "fields": [{"name": "feedback"}]}]
    assert _ways(_reduce(node, plan)) == []


def test_a_failed_build_moves_nobody(node):
    failed = [*BUILD, {"type": "step_error", "orch_step_id": "step_save", "error": "An internal error occurred.",
                       "reason": "boom"}, _done("failed", summary="")]
    assert _ways(_reduce(node, failed)) == []


def test_a_stopped_build_moves_nobody(node):
    stopped = [*BUILD, {"type": "orchestration_error", "error": "Cancelled"}, _done("cancelled", summary="")]
    assert _ways(_reduce(node, stopped)) == []
    # A completion that lost its status on the way (a journal shrinks an event past 32 KB to its type) after a stop.
    assert _ways(_reduce(node, [*BUILD, {"type": "orchestration_error", "error": "Cancelled"},
                                {"type": "orchestration_complete", "truncated": True}])) == []


def test_a_build_taken_back_moves_nobody(node):
    why = "the build stopped before it was saved whole: a step failed"
    undone = [*BUILD, _saved("orchestration", "deleted", "orch_a", "Invoices", reason=why),
              _saved("agent", "deleted", "agent_r", "Reader", reason=why), _done(summary="I stopped before saving.")]
    assert _ways(_reduce(node, undone)) == []


def test_a_change_put_back_moves_nobody(node):
    put_back = [{"type": "orchestration_start", "run_id": RUN}, _saved("orchestration", "updated", "orch_a"),
                _saved("orchestration", "updated", "orch_a", reason=PUT_BACK), _done(summary="It was put back.")]
    assert _ways(_reduce(node, put_back)) == []


def test_only_this_turns_writes_count(node):
    # The turn before built a workflow; this one answered a question. The earlier way stays where it was, and this
    # turn adds none.
    earlier = ("[{ role: 'assistant', kind: 'banner', key: 'saved-orchestration-orch_old-created', content: 'x',"
               " saved: { kind: 'orchestration', action: 'created', id: 'orch_old', name: 'Old' } }]")
    answered = [{"type": "orchestration_start", "run_id": RUN}, _done(summary="You have three workflows.")]
    assert _ways(_reduce(node, answered, before=earlier)) == []


def test_an_agent_made_alone_shows_the_way_to_chat_with_it(node):
    made = [{"type": "orchestration_start", "run_id": RUN}, _saved("agent", "created", "agent_r", "Researcher"),
            _done(summary='Made the agent "Researcher".')]
    assert [w["place"] for w in _ways(_reduce(node, made))] == [
        {"kind": "agent", "id": "agent_r", "name": "Researcher", "made": True}]


def test_a_completion_without_a_status_still_shows_the_way(node):
    # An older engine's event, or one shrunk to its type alone: completed unless the turn failed or was stopped.
    shrunk = [*BUILD, {"type": "orchestration_complete", "truncated": True}]
    assert [w["place"]["id"] for w in _ways(_reduce(node, shrunk))] == ["orch_a"]


# ── a chat read back from its runs ───────────────────────────────────────────

def test_a_chat_read_back_shows_the_way_under_each_turn_that_built(node):
    conversation = {
        "client_session_id": "c", "run_id": "r2", "status": "completed", "pending": None,
        "turns": [
            {"user": "read my invoices", "assistant": 'Built the workflow "Invoices".',
             "writes": [W("agent", "created", "agent_r"), W("orchestration", "created", "orch_a", "Invoices")]},
            {"user": "what does it cost?", "assistant": "About a cent a run."},
        ],
    }
    out = node(f"""const s = R.reattachedState({json.dumps(conversation)}, {WELCOME});
                   return s.messages.map((m) => ({{ kind: m.kind, key: m.key, place: m.place ?? null }}));""")
    assert [m["kind"] for m in out] == ["text", "text", "text", "go_to", "text", "text"]
    assert out[3]["place"] == {"kind": "workflow", "id": "orch_a", "name": "Invoices", "made": True}


def test_the_card_and_the_hosts_wiring_are_there():
    # The card is the kit's, synced into cloud; each product's conversation draws it and presses it.
    kit = (SRC / "components/assistant/index.ts").read_text()
    assert "GoToCard" in kit, "components/assistant does not export the way-there card"
    panel = (SRC / "components/app/BuilderPanel.tsx").read_text()
    assert "message.kind === 'go_to'" in panel and "<GoToCard" in panel, "the conversation never draws the way there"
    chats = (SRC / "components/app/builderChats.ts").read_text()
    assert "watchBuilt" in chats, "the chat never tells its views that a build it sent ended"

"""
The per-run text log is written off the event loop, in order, and is
complete when the engine closes it.
"""
import threading


async def test_lines_land_in_order_and_off_the_loop(monkeypatch):
    from core.orchestration.logger import OrchestrationLogger

    threads: set[str] = set()
    real_open = open

    def spying_open(*a, **k):
        threads.add(threading.current_thread().name)
        return real_open(*a, **k)

    monkeypatch.setattr("builtins.open", spying_open)
    log = OrchestrationLogger(run_id="run_log", orchestration_id="o1",
                              orchestration_name="Test", user_input="hello")
    for n in range(200):
        log.step_start(f"s{n}", f"Step {n}", "print")
    log.run_end("completed")
    await log.aclose()

    text = log.path.read_text(encoding="utf-8")
    positions = [text.index(f"Step ID        : s{n}\n") for n in range(200)]
    assert positions == sorted(positions)
    assert "RUN FINISHED" in text and text.index("RUN FINISHED") > positions[-1]
    assert threading.current_thread().name not in threads
    assert threads and all(t.startswith("synapse-journal") for t in threads)


async def test_close_publishes_the_complete_log():
    from core.orchestration.logger import OrchestrationLogger, _blob_key
    from core.storage import get_blob_store

    log = OrchestrationLogger(run_id="run_pub", orchestration_id="o1",
                              orchestration_name="Test", user_input="hi")
    log.step_start("s1", "One", "print")
    log.run_end("completed")
    log.close()   # the synchronous form must also wait for the writer

    published = get_blob_store().get(_blob_key("run_pub"))
    assert "STEP START: One" in published and "RUN FINISHED" in published


async def test_an_engine_run_leaves_a_complete_log(fake_llm):
    import types
    from _fakes import seed as S
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.logger import _blob_key
    from core.storage import get_blob_store

    orch = S.make_orchestration(id="orch_log", entry_step_id="p", steps=[
        {"id": "p", "name": "Only", "type": "print", "print_content": "x",
         "output_key": "o", "next_step_id": None}])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    events = [e async for e in engine.run("go", run_id="run_engine_log")]
    assert any(e.get("type") == "orchestration_complete" for e in events)
    published = get_blob_store().get(_blob_key("run_engine_log"))
    assert "STEP START: Only" in published
    assert "Status   : completed" in published


async def test_a_warning_and_the_branch_it_explains_both_reach_the_transcript():
    """The archive is the record that outlives the run, and it had neither.

    `step_warning` was emitted by every failing expression, every exhausted
    loop and every unconfigured branch, and rendered by nothing — so a run that
    took the False path because its condition would not evaluate read exactly
    like one whose condition was false. With the expression evaluator refusing
    an unsafe condition rather than running it, that difference is the whole
    report an owner gets.
    """
    from core.orchestration.logger import OrchestrationLogger, _blob_key
    from core.storage import get_blob_store

    log = OrchestrationLogger(run_id="run_warn", orchestration_id="o1",
                              orchestration_name="Test", user_input="hi")
    log.step_start("s1", "Branch", "if_else")
    log.log_event({
        "type": "step_warning", "orch_step_id": "s1",
        "message": "Condition eval error (treating as False): unsafe expression — "
                   "'.__subclasses__' is not readable",
    })
    log.log_event({
        "type": "if_decision", "orch_step_id": "s1", "step_name": "Branch",
        "condition": "().__class__.__bases__[0].__subclasses__()", "result": "false",
    })
    log.log_event({
        "type": "switch_decision", "orch_step_id": "s2", "step_name": "Route",
        "expression": "state.category", "value": "None", "matched_case": None,
    })
    log.run_end("completed")
    log.close()

    published = get_blob_store().get(_blob_key("run_warn"))
    assert "⚠️ WARNING:" in published
    assert "unsafe expression" in published
    assert "IF: ().__class__.__bases__[0].__subclasses__() → false" in published
    assert "SWITCH: state.category → None (case: default)" in published


async def test_the_two_events_that_share_a_name_are_both_rendered():
    """`if_decision` and `switch_decision` each name two different events.

    The step executor emits one carrying the expression and what it came to;
    `engine._resolve_next` emits a second carrying the branch actually taken.
    They share no field. Rendering only the first shape printed an empty line
    for the second — which is what the live run log did too, as
    `If/Else:  → undefined`, on every if_else run there has ever been.
    """
    from core.orchestration.logger import OrchestrationLogger, _blob_key
    from core.storage import get_blob_store

    log = OrchestrationLogger(run_id="run_shapes", orchestration_id="o1",
                              orchestration_name="Test", user_input="hi")
    log.step_start("s1", "Branch", "if_else")
    log.log_event({"type": "if_decision", "orch_step_id": "s1",
                   "decision": "false", "target_step_id": "s_false"})
    log.log_event({"type": "switch_decision", "orch_step_id": "s2",
                   "matched_case": None, "target_step_id": "s_default"})
    log.run_end("completed")
    log.close()

    published = get_blob_store().get(_blob_key("run_shapes"))
    assert "↪ BRANCH: false → s_false" in published
    assert "↪ BRANCH: case default → s_default" in published
    assert "→ undefined" not in published
    assert "IF:  →" not in published

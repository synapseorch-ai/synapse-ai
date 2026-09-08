"""
The engine narrates itself through `logging`, not `print`.

At INFO — the default — an orchestration run emits nothing from the engine's
runtime modules but warnings; at DEBUG the step-by-step narration is there.
And a tripwire keeps `print` from creeping back into the migrated modules.
"""
import ast
import logging
import pathlib
import types

import pytest

from _fakes import seed as S

MIGRATED = [
    "core/llm_providers.py", "core/react_engine.py", "core/mcp_client.py", "core/tools.py",
    "core/compaction.py", "core/scale/worker_engine_adapter.py", "core/scale/worker.py",
    "core/scale/worker_server_module.py",
    *sorted(f"core/orchestration/{p.name}" for p in
            (pathlib.Path(__file__).resolve().parents[2] / "core" / "orchestration").glob("*.py")),
]
BACKEND = pathlib.Path(__file__).resolve().parents[2]


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, run_id="run_logs"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


def _three_prints():
    return S.make_orchestration(
        id="orch_logs", entry_step_id="a",
        steps=[{"id": "a", "name": "A", "type": "print", "print_content": "a", "output_key": "oa", "next_step_id": "b"},
               {"id": "b", "name": "B", "type": "print", "print_content": "b", "output_key": "ob", "next_step_id": "c"},
               {"id": "c", "name": "C", "type": "print", "print_content": "c", "output_key": "oc", "next_step_id": None}],
    )


class TestNoPrintInTheRuntimeModules:
    def test_the_migrated_modules_have_no_print_calls(self):
        offenders = []
        for rel in MIGRATED:
            tree = ast.parse((BACKEND / rel).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                    offenders.append(f"{rel}:{node.lineno}")
        assert offenders == [], "print() is back in a runtime module:\n" + "\n".join(offenders)

    def test_every_migrated_module_logs_under_the_core_tree(self):
        for rel in MIGRATED:
            src = (BACKEND / rel).read_text()
            if "_log." in src:
                assert "_log = get_logger(__name__)" in src, rel


class TestQuietByDefault:
    async def test_a_run_at_info_emits_no_narration(self, caplog):
        caplog.set_level(logging.INFO, logger="core")
        events = await _run(_three_prints())
        assert any(e.get("type") == "orchestration_complete" for e in events)
        loud = [r for r in caplog.records if r.name.startswith("core") and r.levelno >= logging.INFO]
        assert loud == [], [f"{r.name}: {r.getMessage()}" for r in loud]

    async def test_the_same_run_at_debug_narrates(self, caplog):
        caplog.set_level(logging.DEBUG, logger="core")
        await _run(_three_prints(), run_id="run_logs_debug")
        narration = [r.getMessage() for r in caplog.records if r.name == "core.orchestration.engine"]
        assert any(m.startswith("ENGINE: ") for m in narration), narration[:5]
        assert len(narration) >= 3

    async def test_a_worker_resume_at_info_is_quiet(self, caplog, fake_redis):
        """The adapter used to print one line per SSE event it forwarded."""
        from core.models_orchestration import Orchestration
        from core.scale.pubsub import RunEventPublisher
        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        from core.store import get_store

        pausing = S.make_orchestration(
            id="orch_logs_pause", entry_step_id="h",
            steps=[{"id": "h", "name": "Approve", "type": "human", "human_prompt": "ok?",
                    "output_key": "approval", "next_step_id": "after"},
                   {"id": "after", "name": "After", "type": "print", "print_content": "{state.approval}",
                    "output_key": "final", "next_step_id": None}],
        )
        await S.seed_orchestrations([pausing])
        adapter = WorkerEngineAdapter(
            orch=Orchestration.model_validate(pausing), run_id="run_logs_worker",
            worker_server_module=_server(), publisher=RunEventPublisher(fake_redis, "run_logs_worker"),
            session_factory=await get_store(), redis_client=fake_redis, worker_id="w", job_id="j",
        )
        assert await adapter.run(initial_input="go") == "paused"

        caplog.set_level(logging.INFO, logger="core")
        caplog.clear()
        assert await adapter.resume({"decision": "yes"}) == "completed"
        loud = [r for r in caplog.records if r.name.startswith("core") and r.levelno >= logging.INFO]
        assert len(loud) <= 3, [r.getMessage() for r in loud]


class TestConfigure:
    def test_level_comes_from_the_environment_and_is_idempotent(self, monkeypatch):
        import core.log as log_mod
        monkeypatch.setenv("SYNAPSE_LOG_LEVEL", "debug")
        root_handlers_before = list(logging.getLogger().handlers)
        log_mod.configure()
        log_mod.configure()
        assert logging.getLogger("core").level == logging.DEBUG
        # A second call adds nothing to the root.
        assert len(logging.getLogger().handlers) <= len(root_handlers_before) + 1

    def test_an_explicit_level_wins_and_nonsense_falls_back_to_info(self):
        import core.log as log_mod
        log_mod.configure("warning")
        assert logging.getLogger("core").level == logging.WARNING
        log_mod.configure("not-a-level")
        assert logging.getLogger("core").level == logging.INFO

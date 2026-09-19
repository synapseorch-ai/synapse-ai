"""No response body starts work (F49) — one guard over every route module.

Work started inside a streaming response's body belongs to that response: the
body is cancelled when its client hangs up, and whatever it was advancing dies
with it. A route handler starts the work (a run through `spawn_engine_run`, a
chat turn through `core.chat_turns`) and the body only reads it.

`tests/api_app/test_outlives_the_client.py` proves the behaviour for the routes
that exist, against a real server. This guard is for the next route: it reads
the source, because a route written the old way passes every ASGITransport
test there is.

Four rules, each over `core/routes/*.py`:

A. A generator that is not a producer never starts work or calls a producer.
   (A response body is a generator.)
B. A producer — the generator a chat turn's task drives — is called only by
   another producer, or handed straight to a turn's `.start(...)`.
C. `run_react_loop` / `run_agent_step` run only inside a producer.
D. No `asyncio.create_task(...)` whose task is thrown away: nothing holds it, so
   it can be collected mid-run. `core.background.spawn_background` holds it.
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

#: Calls that start work that must outlive whoever asked for it.
WORK = frozenset({
    "spawn_engine_run", "stream_engine_events", "run_react_loop", "run_agent_step",
    "run_orchestrator_agent", "answer_paused_run",
})
#: `engine.run(...)`, `OrchestrationEngine.resume(...)` and friends.
ENGINE_RECEIVERS = frozenset({"engine", "OrchestrationEngine"})
ENGINE_METHODS = frozenset({"run", "resume", "resume_failed", "resume_paused"})
#: The generators a chat turn's task drives.
PRODUCERS = frozenset({
    "_event_stream", "_v1_turn_events", "run_builder_stream", "run_builder_stream_compat",
})
LOOP = frozenset({"run_react_loop", "run_agent_step"})

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _called(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_work(call: ast.Call) -> bool:
    name = _called(call)
    if name in WORK:
        return True
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in ENGINE_METHODS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in ENGINE_RECEIVERS
    )


def _is_generator(fn) -> bool:
    """Whether `fn` itself yields (a nested function's yield is its own)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, _FUNCTIONS):
            stack.extend(ast.iter_child_nodes(node))
    return False


def violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def enclosing(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, _FUNCTIONS):
                return node
        return None

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if _called(node.value) in ("create_task", "ensure_future"):
                found.append(f"{filename}:{node.lineno}: D — a task nothing holds "
                             "(use core.background.spawn_background)")
        if not isinstance(node, ast.Call):
            continue
        fn = enclosing(node)
        fn_name = getattr(fn, "name", None)
        in_producer = fn_name in PRODUCERS
        name = _called(node)

        if fn is not None and not in_producer and _is_generator(fn):
            if _is_work(node) or name in PRODUCERS:
                found.append(f"{filename}:{node.lineno}: A — `{name}` inside the generator "
                             f"`{fn_name}`: a response body that starts work loses it to a hang-up")
        if name in PRODUCERS and not in_producer:
            outer = parent.get(node)
            if not (isinstance(outer, ast.Call) and _called(outer) == "start" and node in outer.args):
                found.append(f"{filename}:{node.lineno}: B — the producer `{name}` is called "
                             "outside a turn's `.start(...)`")
        if name in LOOP and not in_producer:
            found.append(f"{filename}:{node.lineno}: C — `{name}` outside a producer "
                         "runs a turn no registry can see or stop")
    return found


def _route_modules() -> list[pathlib.Path]:
    import core.routes
    return sorted(pathlib.Path(core.routes.__file__).parent.glob("*.py"))


def test_no_route_module_starts_work_in_a_response_body():
    found = []
    for path in _route_modules():
        found += violations(path.read_text(), f"core/routes/{path.name}")
    assert not found, "\n".join(found)


class TestTheGuardSeesTheShapesItIsFor:
    """The guard is only worth its rules if it fails on the code F49 fixed."""

    def test_a_turn_driven_from_inside_the_response(self):
        # The chat route before F49: the body drove the turn.
        src = textwrap.dedent('''
            def _guarded_stream(request):
                async def event_generator():
                    async for chunk in _event_stream(request, _server):
                        yield chunk
                return event_generator()
        ''')
        assert [v.split(": ")[1][0] for v in violations(src)] == ["A", "B"]

    def test_a_run_spawned_lazily_inside_the_response(self):
        # v1 run/stream before F49.
        src = textwrap.dedent('''
            async def v1_run_stream(body):
                async def event_stream():
                    async for event in iter_with_heartbeat(
                        stream_engine_events(engine.run(body.message, run_id), run_id)):
                        yield event
                return StreamingResponse(event_stream())
        ''')
        assert {v.split(": ")[1][0] for v in violations(src)} == {"A"}

    def test_a_loop_run_by_the_handler_itself(self):
        # POST /chat before F49: no registry could see or stop it.
        src = textwrap.dedent('''
            async def chat(request):
                async for event in run_react_loop(request, _server):
                    pass
        ''')
        assert [v.split(": ")[1][0] for v in violations(src)] == ["C"]

    def test_a_task_nothing_holds(self):
        src = "async def f():\n    asyncio.create_task(work())\n"
        assert [v.split(": ")[1][0] for v in violations(src)] == ["D"]

    @pytest.mark.parametrize("src", [
        # The handler starts the turn and the body reads it.
        "def _start(r):\n    with claim(r) as turn:\n        turn.start(_event_stream(r, s))\n",
        # A producer may call another producer.
        "async def _event_stream(r, s):\n    async for c in run_builder_stream_compat(r, s):\n        yield c\n",
        # A handler spawns a run; its body only drains the queue.
        "async def h():\n    _t, q = spawn_engine_run(engine.run(m, i), i)\n"
        "    async def body():\n        yield await q.get()\n    return body()\n",
        # A held background task.
        "async def f():\n    task = asyncio.create_task(work())\n",
    ])
    def test_the_shapes_that_are_right_pass(self, src):
        assert violations(src) == []

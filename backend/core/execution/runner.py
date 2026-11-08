"""Who runs the code, and how a deployment says so.

One registry, one accessor, and a rule: **`get_runner()` never guesses.**

## `SYNAPSE_CODE_RUNNER`

A deployment names its runner the same way it already names a document resolver
or a session binder — an import path, `package.module:factory`, read from the
environment (see `core/tool_server.py`, where that pattern is set out at
length). The factory is called once and its result cached.

Unset, the answer is `DockerRunner`, which is what the shipped product has
always done.

**Set and unresolvable is an error, not a fallback.** A hosted deployment that
means to run customer code in a Lambda and instead finds a Docker socket has
lost the entire isolation story, silently, in the one direction where silence is
worst. So an import that fails raises here, at the call, rather than being
logged and stepped over at startup.
"""
from __future__ import annotations

import importlib
import os
from typing import Protocol, runtime_checkable

from .types import ExecRequest, ExecResult

#: The environment variable a deployment sets. Named here rather than inlined
#: because the tests and the cloud tree both have to spell it.
RUNNER_ENV = "SYNAPSE_CODE_RUNNER"


@runtime_checkable
class CodeRunner(Protocol):
    """Runs a script somewhere, and says what happened.

    A runner may not widen a request: asked for `Network.NONE` it either
    guarantees no egress or raises `ExecUnsupported`. It may narrow one — a
    deployment policy that forbids the internet outright is free to serve every
    request with no network — but never the other way round.
    """

    async def run(self, request: ExecRequest) -> ExecResult: ...


_registered: CodeRunner | None = None
_resolved_from_env: bool = False


def register_runner(runner: CodeRunner | None) -> None:
    """Install the runner this process uses. `None` clears it (tests)."""
    global _registered, _resolved_from_env
    _registered = runner
    _resolved_from_env = False


def _import_named(spec: str) -> object:
    """Resolve a `package.module:factory` spec. Raises if it does not resolve."""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise RuntimeError(
            f"{RUNNER_ENV} must look like 'package.module:factory', got {spec!r}"
        )
    return getattr(importlib.import_module(module_name), attribute)


def get_runner() -> CodeRunner:
    """The runner for this process.

    Order: whatever `register_runner` was given, then `SYNAPSE_CODE_RUNNER`,
    then `DockerRunner`.
    """
    global _registered, _resolved_from_env

    if _registered is not None:
        return _registered

    spec = os.getenv(RUNNER_ENV, "").strip()
    if spec:
        try:
            factory = _import_named(spec)
            runner = factory() if callable(factory) else factory
        except Exception as exc:  # noqa: BLE001 — re-raised with the cause named
            raise RuntimeError(
                f"{RUNNER_ENV}={spec!r} could not be loaded, and there is no safe "
                f"fallback: refusing to run customer code under a runner this "
                f"deployment did not ask for. ({exc})"
            ) from exc
        _registered = runner
        _resolved_from_env = True
        return runner

    from .docker_runner import DockerRunner

    _registered = DockerRunner()
    return _registered


def describe_runner() -> str:
    """A one-line name for the log at startup.

    A registration that failed and one that never happened look identical in a
    log otherwise — the same argument `narrow_native_tools` makes for saying out
    loud what it removed.
    """
    runner = get_runner()
    source = f"{RUNNER_ENV}={os.getenv(RUNNER_ENV, '').strip()}" if _resolved_from_env else "default"
    return f"{type(runner).__name__} ({source})"

"""The escape hatch: run the code as a subprocess of this process. No sandbox.

`transform_runtime = "host"` exists because a 512 MB container with no GPU
cannot load a transformers model, and some people run this engine on one machine
to do exactly that. The script inherits `os.environ` and the interpreter that is
running the engine, so `CUDA_VISIBLE_DEVICES`, `HF_HOME` and the rest flow
through — which is the feature, and also the reason this is a named runner
rather than a branch hidden inside the transform step.

**A multi-tenant deployment must not register this.** The process it forks from
holds decrypted credentials for whoever is running right now. Making it a runner
with a name is what lets such a deployment refuse it in one line instead of
hoping nobody sets the setting.

The timeout is deliberately far longer than the sandbox's: model loading is
minutes, and a 60-second cap here would make the escape hatch useless. The
step's own `timeout_seconds` still applies.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .types import ExecRequest, ExecResult

#: 30 minutes, to bound a runaway load rather than to bound the work.
MAX_TIMEOUT = 1800


class HostRunner:
    """`sys.executable script.py`, with this process's environment."""

    async def run(self, request: ExecRequest) -> ExecResult:
        timeout = min(max(int(request.timeout_s), 1), MAX_TIMEOUT)

        tmp_dir = tempfile.mkdtemp(prefix="synapse_exec_host_")
        script_path = str(Path(tmp_dir) / "script.py")
        try:
            Path(script_path).write_text(request.code, encoding="utf-8")

            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout + request.grace_s
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:  # noqa: BLE001 — the process is already gone
                    pass
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Transform (host) timed out after {timeout}s",
                    timed_out=True,
                )

            # No output cap here, unlike the sandbox: the caller asked for the
            # host runtime precisely because it is producing something large.
            return ExecResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

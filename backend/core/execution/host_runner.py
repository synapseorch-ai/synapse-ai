"""The escape hatch: run the code as a subprocess of this process. No sandbox.

The host runtime exists because a 512 MB container with no GPU cannot load a
transformers model, and some people run this engine on one machine to do exactly
that. Only the server's operator chooses it, by starting the server with
`SYNAPSE_TRANSFORM_RUNTIME=host` — never a stored setting (F968). **It runs
transforms as the server, able to read everything the server can, including
stored keys: only for a single-user machine you trust every workflow on.** It
cannot block the network.

The script runs on the interpreter that runs the engine, with a short allowlist of
its environment (`child_env`): what model loading needs (`CUDA_VISIBLE_DEVICES`,
`HF_HOME` and the rest), never the server's tokens, keys, secrets or URLs (F968).
It used to get `os.environ` whole — the internal token, the session-signing secret,
the database URL.

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

from core.process_env import WINDOWS_START

from .types import ExecRequest, ExecResult

#: 30 minutes, to bound a runaway load rather than to bound the work.
MAX_TIMEOUT = 1800

#: The environment a host transform keeps; everything else is dropped (F968). An allowlist: nothing named
#: `SYNAPSE_*`, no token, key, secret, password or URL, no AWS/S3/Redis settings, no PYTHONPATH.
ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
    # What the host runtime is for: models on this machine's GPU.
    "CUDA_VISIBLE_DEVICES", "CUDA_HOME", "LD_LIBRARY_PATH", "HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
    "TRANSFORMERS_CACHE", "TORCH_HOME", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "TORCH_NUM_THREADS",
}) | WINDOWS_START  # Windows needs these to start a process at all (core.process_env, F1533).
ENV_ALLOW_PREFIXES = ("LC_", "NVIDIA_")


def child_env() -> dict[str, str]:
    """This process's environment, cut to `ENV_ALLOW`."""
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child and reap it, shielded so a cancellation in flight
    cannot leave the wait half-done."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — the process is already gone
        return
    try:
        await asyncio.shield(proc.wait())
    except Exception:  # noqa: BLE001 — reaping is best-effort; it is already dead
        pass


class HostRunner:
    """`sys.executable script.py`, with `child_env()`."""

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
                env=child_env(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout + request.grace_s
                )
            except asyncio.TimeoutError:
                await _kill(proc)
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Transform (host) timed out after {timeout}s",
                    timed_out=True,
                )
            except BaseException:
                # The caller gave up rather than us — a step-level timeout
                # cancels this coroutine mid-await. Only our own timeout used to
                # kill the child, so an externally cancelled transform left the
                # process running: this runner exists for model loading, so an
                # orphan can hold a GPU until someone notices.
                await _kill(proc)
                raise

            # No output cap here, unlike the sandbox: the caller asked for the
            # host runtime precisely because it is producing something large.
            return ExecResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

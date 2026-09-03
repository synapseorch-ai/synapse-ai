"""The shipped runner: one container per call, via the Docker CLI.

This is the code that used to live — five times over, with small unexplained
differences between the copies — in `react_engine.py`, `orchestration/steps.py`
and `tools/sandbox.py`. It is **moved, not rewritten**: same image, same limits,
same read-only root with two tmpfs, same output caps, same "parse stdout as JSON
or wrap it" contract at the callers.

Two things did change, and both are the bug this package exists for:

* `--network none` is actually passed when `Network.NONE` is asked for. It never
  was; `allow_network` was computed, passed as a keyword argument, and dropped.
* asking for something this runner cannot do raises `ExecUnsupported` instead of
  quietly doing something else.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from .types import ExecRequest, ExecResult, ExecUnsupported, Network

#: The image `backend/tools/sandbox.Dockerfile` builds, and the API's
#: `/api/tools/python/build` route builds on demand.
DOCKER_IMAGE = "sandbox-python:latest"

#: Where the script is mounted inside the container. The old copies used
#: `/sandbox/script.py` and `/sandbox/transform.py` for no reason beyond which
#: file they happened to write; the path is invisible to the code being run.
SCRIPT_PATH = "/sandbox/script.py"

#: The tenant's vault, read-only. Callers pass a path that exists or none at all.
VAULT_PATH = "/data"


class DockerRunner:
    """`docker run --rm …`, awaited."""

    image: str = DOCKER_IMAGE

    def build_command(self, request: ExecRequest, script_path: str) -> list[str]:
        """The argv, split out so a test can assert it without a Docker daemon."""
        cmd = [
            "docker", "run", "--rm",
            "--memory", f"{request.memory_mb}m",
            "--cpus", request.cpus,
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={request.tmpfs_mb}m",
            "--tmpfs", f"/root:rw,size={request.tmpfs_mb}m",
        ]

        # The whole reason this package has a required `network` field.
        if request.network is Network.NONE:
            cmd += ["--network", "none"]

        cmd += ["-v", f"{script_path}:{SCRIPT_PATH}:ro"]
        if request.vault_path is not None and Path(request.vault_path).exists():
            cmd += ["-v", f"{request.vault_path}:{VAULT_PATH}:ro"]

        cmd.append(self.image)

        if request.packages:
            # Only names pip could plausibly accept, so a package list from a
            # document cannot become a shell fragment.
            safe = " ".join(
                p for p in request.packages
                if all(c.isalnum() or c in "-_.[]=<>!" for c in p)
            )
            inner = []
            if safe:
                inner.append(
                    "pip install --quiet --disable-pip-version-check "
                    "--root-user-action=ignore --no-warn-script-location " + safe
                )
            inner.append(f"python {SCRIPT_PATH}")
            cmd += ["sh", "-c", " && ".join(inner)]
        else:
            cmd += ["python", SCRIPT_PATH]

        return cmd

    async def run(self, request: ExecRequest) -> ExecResult:
        if not shutil.which("docker"):
            raise ExecUnsupported("Docker is not available. Cannot execute Python code.")
        if request.packages and request.network is Network.NONE:
            raise ExecUnsupported(
                "Installing packages needs a network, and this request asked for none."
            )

        tmp_dir = tempfile.mkdtemp(prefix="synapse_exec_")
        script_path = f"{tmp_dir}/script.py"
        try:
            Path(script_path).write_text(request.code, encoding="utf-8")

            proc = await asyncio.create_subprocess_exec(
                *self.build_command(request, script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=request.timeout_s + request.grace_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Execution timed out after {request.timeout_s}s",
                    timed_out=True,
                )

            return ExecResult(
                exit_code=proc.returncode or 0,
                stdout=stdout_b.decode("utf-8", errors="replace")[: request.max_stdout],
                stderr=stderr_b.decode("utf-8", errors="replace")[: request.max_stderr],
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

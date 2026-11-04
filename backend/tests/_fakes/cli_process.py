"""A CLI provider's process, faked where `call_cli_provider` runs it (F1320).

The CLI runs off the server's loop through `core.llm_providers._run_cli`, no longer through
`asyncio.create_subprocess_exec`; a fixture's `fake_exec(executable, *argv, **kwargs)` and its process with an
awaitable `communicate()` are kept as they were, and called from that seam: `stdin` is the prompt's file opened
(None when there is none), `cwd` and `env` as the CLI is given them.
"""
from __future__ import annotations


def fake_cli_process(monkeypatch, fake_exec) -> None:
    from core import llm_providers

    async def run(argv, *, stdin_path, cwd, env, deadline):
        stdin = open(stdin_path, "rb") if stdin_path else None
        try:
            proc = await fake_exec(argv[0], *argv[1:], stdin=stdin, cwd=cwd, env=env)
            out, err = await proc.communicate()
        finally:
            if stdin is not None:
                stdin.close()
        return llm_providers.CliRun(proc.returncode, out or b"", err or b"", False)

    monkeypatch.setattr(llm_providers, "_run_cli", run)

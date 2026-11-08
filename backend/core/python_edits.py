"""A Python tool's code edited by a change, and tested before the yes (F1325; Naveen 11-04).

* `apply_edits(code, edits)`: each passage quoted exactly as it stands in the code, replaced by its new text, in order
  (F1258's way for a document). A passage that is not there, or is there more than once, is said, never guessed at.
* `diff(old, new)`: what the review shows, before and after.
* `run_samples(code, globals_, samples, network)`: the code run on each sample input as the editor's Test button runs
  it (`routes.tools.test_python_tool`: the same runner, limits and Vault), but with the network the caller asks for.
  Before the yes it is `Network.NONE` (Naveen: no internet before the yes); a runner that can't guarantee none raises
  `ExecUnsupported`, as `core.execution` requires, and the change offers "Test this fix" instead.
* `needed_network(results)`: whether a run with no network failed for want of one, which "Test this fix" answers.
* `digest(code, samples)`: what a passing press is recorded under, so the yes makes exactly the code that passed.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any

#: The most sample inputs one change runs, and what of each run's output the review shows.
MAX_SAMPLES, SHOWN = 3, 400
#: A failure that says only that the network was not there (a refused or unreachable connection, a name not resolved).
_NO_NETWORK = re.compile(r"(?i)network is unreachable|errno 101|name or service not known|temporary failure in name "
                         r"resolution|gaierror|nodename nor servname|connection refused|urlerror|connectionerror|"
                         r"failed to establish a new connection")
#: Where a press's pass is kept: a passing run of this exact code on these samples.
PASSES = "code_tests"


def apply_edits(code: str, edits: list[dict]) -> tuple[str, str | None]:
    """`code` with each edit made, or why one can't be: its passage not in the code as it stands, or more than once."""
    out = code or ""
    for edit in edits or []:
        find, replace = str(edit.get("find") or ""), str(edit.get("replace") or "")
        count = out.count(find) if find else 0
        if count == 0:
            return code, f"the passage {json.dumps(find)} is not in its code as it stands: quote it exactly"
        if count > 1:
            return code, f"the passage {json.dumps(find)} is in its code {count} times: quote more of it, once"
        out = out.replace(find, replace, 1)
    return out, None


def diff(old: str, new: str) -> str:
    """Before and after, as a unified diff without its file headers."""
    lines = difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=2)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++")))


def code_sha(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def digest(code: str, samples: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"code": code, "samples": samples}, sort_keys=True).encode()).hexdigest()


async def run_samples(code: str, globals_: list[str] | None, samples: list[dict], network) -> list[dict]:
    """Each sample's run: `{args, ok, printed, error}`, the output's tail cut to `SHOWN`. Raises `ExecUnsupported`
    when the runner can't give the network asked for."""
    from core import variables
    from core.execution import ExecRequest, declared_globals, get_runner, wrap_args
    from core.routes.tools import CPU_LIMIT, MEMORY_LIMIT, _vault_root

    values = declared_globals({"globals": globals_}, await variables.load()) if globals_ else {}
    out: list[dict] = []
    for args in samples[:MAX_SAMPLES]:
        result = await get_runner().run(ExecRequest(
            code=wrap_args(code, args or {}, values), network=network, timeout_s=30,
            memory_mb=int(MEMORY_LIMIT.rstrip("m")), cpus=CPU_LIMIT, vault_path=Path(_vault_root())))
        error = (result.stderr or "").strip()
        out.append({"args": args, "ok": result.ok, "printed": (result.stdout or "").strip()[-SHOWN:],
                    **({} if result.ok else {"error": error[-SHOWN:] or ("timed out" if result.timed_out
                                                                         else f"exit {result.exit_code}")})})
    return out


def needed_network(results: list[dict]) -> bool:
    return any(not r["ok"] and _NO_NETWORK.search(r.get("error") or "") for r in results)


def last_line(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


async def declared_values(globals_: list[str] | None) -> dict:
    """The values of the workspace variables a tool declares, name to value: what its output is masked by."""
    from core import variables
    from core.execution import declared_globals

    return declared_globals({"globals": globals_}, await variables.load()) if globals_ else {}


async def masked_results(results: list[dict], globals_: list[str] | None) -> list[dict]:
    """Each sample's run with what it printed and raised masked (`secret_scrub.masked_output`, F1381)."""
    from core.secret_scrub import masked_output

    values = await declared_values(globals_)
    return [{**r, "printed": masked_output(r.get("printed"), values),
             **({"error": masked_output(r["error"], values)} if "error" in r else {})} for r in results]


def said(results: list[dict]) -> list[str]:
    """Each sample's line for the review: what it was given and what it printed."""
    return [f"tested with {json.dumps(r['args'], ensure_ascii=False)}: printed {r['printed'] or '(nothing)'}"
            for r in results]


async def passed(code: str, samples: list[dict]) -> bool:
    """Whether a press of "Test this fix" passed with this exact code on these samples."""
    from core.store import collections

    return any(isinstance(row, dict) and row.get("digest") == digest(code, samples) and row.get("passed")
               for row in await collections.load(PASSES))


async def keep_pass(code: str, samples: list[dict]) -> None:
    from core.store import collections

    key = digest(code, samples)
    rows = [r for r in await collections.load(PASSES) if isinstance(r, dict) and r.get("id") != key][-49:]
    await collections.save(PASSES, [*rows, {"id": key, "digest": key, "passed": True}])


def is_tested(fields: dict) -> Any:
    return fields.get("_test") if isinstance(fields.get("_test"), dict) else None

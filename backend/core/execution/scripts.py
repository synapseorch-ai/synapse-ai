"""The two script wrappers the engine uses, written once.

Both were duplicated at three call sites, character for character, including the
escaping. They moved here **unchanged** — the point of this package is that the
five places that ran code stop being five places, not that the wrapping gets a
redesign at the same time. One change since, on purpose: a transform's `state`
answers `state.key` (see `_STATE_CLASS`).
"""
from __future__ import annotations

import json
import re
from typing import Any


def _embed(payload: Any) -> str:
    """JSON, escaped for a triple-quoted Python string literal."""
    return json.dumps(payload, default=str).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def wrap_args(
    code: str, args: dict[str, Any], globals_: dict[str, Any] | None = None
) -> str:
    """A custom Python tool: the call's arguments arrive as `_args`, and the
    globals the tool declares as `_globals` (empty when it declares none)."""
    return (
        f'import json\n_args = json.loads("""{_embed(args)}""")\n'
        f'_globals = json.loads("""{_embed(globals_ or {})}""")\n\n'
    ) + code


def declared_globals(tool: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """The workspace variables a Python tool names in `globals`, and no others.

    **This is the only way a variable reaches customer code** (F28, D104). The
    arguments a model writes are never rendered with `{{ global.* }}` — they
    were, in both the TOOL-step and the agent branch, so a prompt-injected page
    could name any variable and have its plaintext (a secret, on a hosted
    deployment) handed to code running with internet access. A tool now says
    which it needs, the person who wrote it decides, and a declared name that is
    not set is simply absent.
    """
    names = tool.get("globals") or []
    if not isinstance(names, list):
        return {}
    return {name: variables[name] for name in names if isinstance(name, str) and name in variables}


#: `state.key` reads a key, as it does in a condition, a prompt and `{{state.key}}`.
#: A plain dict made the spelling the rest of the product teaches an
#: AttributeError that named `dict` rather than the step. Nested objects answer
#: the same way; a key that shadows a dict method (`items`, `get`) still needs
#: `state["items"]`. It is still a dict, so `result = state.x` prints as JSON.
_STATE_CLASS = '''class _SynapseState(dict):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"no key {name!r} here; the keys are: {', '.join(sorted(map(str, self))) or 'none'}"
            ) from None
'''


#: `read_vault(path)` in a transform (F1227): a Vault file's text, read by the engine before the step ran — the
#: runner mounts nothing it can rely on (cloud's Lambda mounts no Vault at all), so the files ride in as `state` does.
_READ_VAULT = '''
def read_vault(path):
    """A Vault file's text. The path is written out in the code, so the step reads it before it runs."""
    key = str(path).strip().lstrip("/")
    if key not in _vault_files:
        raise FileNotFoundError(f"read_vault({path!r}): only a path written out in the code is read, as in "
                                f"read_vault('reports/sales.csv')")
    return _vault_files[key]
'''


def _transform_head(state: dict[str, Any], files: dict[str, str] | None = None) -> str:
    """What a transform's script runs before the person's code."""
    return (
        f'import json\n{_STATE_CLASS}\nstate = json.loads("""{_embed(state)}""", object_hook=_SynapseState)\n'
        f'_vault_files = json.loads("""{_embed(files or {})}"""){_READ_VAULT}'
        'result = None\n\n'
    )


def wrap_transform(code: str, state: dict[str, Any], files: dict[str, str] | None = None) -> str:
    """A transform step: `state` in, `result` out, printed as JSON. `files`: the Vault files its code names with
    `read_vault`, by path, read before it runs (F1227)."""
    return (
        _transform_head(state, files)
        + code
        + '\n\nif result is not None:\n    print(json.dumps({"result": result}, default=str))\n'
        'else:\n    print(json.dumps({"result": None}))\n'
    )


def transform_line_offset(state: dict[str, Any], files: dict[str, str] | None = None) -> int:
    """How many script lines come before the person's first: their line N is the
    script's N + this (F407)."""
    return _transform_head(state, files).count("\n")


def vault_paths_named(code: str) -> tuple[list[str], list[int]]:
    """The paths a transform's code reads with `read_vault("…")`, written out, and the lines of any call whose path is
    not written out (F1227). Code that does not parse names none: the step fails on it as before."""
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [], []
    paths: list[str] = []
    unnamed: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "read_vault":
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.strip():
                key = arg.value.strip().lstrip("/")
                if key not in paths:
                    paths.append(key)
            else:
                unnamed.append(node.lineno)
    return paths, unnamed


#: A traceback's frame in the wrapped script, whatever the runner named its file:
#: `/sandbox/script.py` (Docker), a temp dir's (the host, a Lambda).
_SCRIPT_FRAME = re.compile(r'File "[^"]*script\.py", line (\d+)')
_NOT_THE_ERROR = ("Traceback (most recent call last)", "During handling of the above exception",
                  "The above exception was the direct cause")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def transform_failure(stderr: str, code: str, offset: int, exit_code: int, *,
                      label: str = "Transform") -> str:
    """Why a transform failed, in one line a person can act on (F407).

    The sandbox's traceback named the wrapper's line (the state loader sits above
    the code: the user's line 2 read "line 17"), the runner's temp path, and ran
    to several lines. Now: the deepest frame in the person's own code, its
    source and what it raised — "Transform failed on line 2
    (`raise ValueError('boom')`): ValueError: boom". A frame in the wrapper (the
    state class answering `state.missing`) is not theirs; the line that read it
    is. Python has no backticks, so they mark the source unambiguously. No
    traceback at all (killed, out of memory) says the exit and stderr's tail.
    """
    text = stderr or ""
    start = text.rfind("Traceback (most recent call last):")
    if start >= 0:
        text = text[start:]
    lines = code.splitlines() or [""]
    line = None
    for match in _SCRIPT_FRAME.finditer(text):
        n = int(match.group(1)) - offset
        if 1 <= n <= len(lines):
            line = n
    raised = next((ln.strip() for ln in reversed(text.splitlines())
                   if ln.strip() and not ln[:1].isspace() and not ln.startswith(_NOT_THE_ERROR)), "")
    if line is None and start < 0 and not _SCRIPT_FRAME.search(text):
        tail = _clip(" ".join((stderr or "").split()[-40:]), 300)
        return f"{label} failed (exit {exit_code})" + (f": {tail}" if tail else "")
    where = ""
    if line is not None:
        source = lines[line - 1].strip()
        where = f" on line {line}" + (f" (`{_clip(source, 120)}`)" if source and "`" not in source else "")
    return f"{label} failed{where}: {_clip(raised, 300) or f'exit {exit_code}'}"

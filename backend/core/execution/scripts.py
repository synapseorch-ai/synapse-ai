"""The two script wrappers the engine uses, written once.

Both were duplicated at three call sites, character for character, including the
escaping. They moved here **unchanged** — the point of this package is that the
five places that ran code stop being five places, not that the wrapping gets a
redesign at the same time. One change since, on purpose: a transform's `state`
answers `state.key` (see `_STATE_CLASS`).
"""
from __future__ import annotations

import json
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


def wrap_transform(code: str, state: dict[str, Any]) -> str:
    """A transform step: `state` in, `result` out, printed as JSON."""
    return (
        f'import json\n{_STATE_CLASS}\nstate = json.loads("""{_embed(state)}""", object_hook=_SynapseState)\n'
        'result = None\n\n'
        + code
        + '\n\nif result is not None:\n    print(json.dumps({"result": result}, default=str))\n'
        'else:\n    print(json.dumps({"result": None}))\n'
    )

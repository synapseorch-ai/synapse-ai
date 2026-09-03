"""The two script wrappers the engine uses, written once.

Both were duplicated at three call sites, character for character, including the
escaping. They are kept here **unchanged** — the point of this package is that
the five places that ran code stop being five places, not that the wrapping gets
a redesign at the same time.
"""
from __future__ import annotations

import json
from typing import Any


def _embed(payload: Any) -> str:
    """JSON, escaped for a triple-quoted Python string literal."""
    return json.dumps(payload, default=str).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def wrap_args(code: str, args: dict[str, Any]) -> str:
    """A custom Python tool: the call's arguments arrive as `_args`."""
    return f'import json\n_args = json.loads("""{_embed(args)}""")\n\n' + code


def wrap_transform(code: str, state: dict[str, Any]) -> str:
    """A transform step: `state` in, `result` out, printed as JSON."""
    return (
        f'import json\nstate = json.loads("""{_embed(state)}""")\nresult = None\n\n'
        + code
        + '\n\nif result is not None:\n    print(json.dumps({"result": result}, default=str))\n'
        'else:\n    print(json.dumps({"result": None}))\n'
    )

"""Turning a custom HTTP tool plus its arguments into one request.

**One implementation, because there were two and they drifted twice.**

`react_engine.run_agent_step` and `orchestration/steps.py::ToolStepExecutor`
both executed HTTP tools, from copies of the same code. D69 fixed three things
in the agent's copy — `{{global.key}}` rendering, the request-body template, and
arguments consumed by the URL no longer being sent in the body as well — and the
orchestration copy got none of them. So the same tool, called by an agent and
called by a TOOL step, behaved differently: one resolved an organisation's
variables and one sent a literal `{{global.api_base}}` to the server.

That is exactly the shape `core/execution` was created to end for `docker run`,
and this is the same medicine. Both call sites now build their request here.

What stays at the call sites, because it genuinely differs: the agent vaults a
large response and filters it through `outputSchema`; the step returns it whole.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core import templating

#: `{data}`, `{data.key}` and `{key}` — the single-brace forms that predate
#: `{{ … }}`. Applied after the double-brace pass, so a URL may hold either.
_SINGLE = re.compile(r"\{([^{}]+)\}")


def _walk(obj: Any, path: str) -> str | None:
    """Follow a dot-separated path into `obj`, rendered as text."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return json.dumps(cur) if isinstance(cur, (dict, list)) else str(cur)


def render_url(url: str, args: dict[str, Any], global_variables: dict[str, Any]) -> tuple[str, set[str]]:
    """The URL with its references filled in, and the argument keys it used.

    Two passes, and the order matters. `{{ … }}` first, so `{{global.base}}` can
    expand into something that itself contains `{id}`; then the single-brace
    forms, so a URL written before this syntax existed — or pasted from an
    OpenAPI spec, whose path templating *is* single-brace — keeps working.

    The consumed keys are the point of the return value: an argument the URL
    already used must not be sent again as a query parameter or in the body,
    which is what the agent's copy did until D69 and this one did until now.
    """
    consumed: set[str] = set()

    context = {"global": global_variables, "args": args}
    for reference in templating.references(url):
        head = reference.split(".")[0]
        if head not in templating.NAMESPACES and head in args:
            consumed.add(head)
    url = templating.render(url, context, bare="args")

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr == "data":
            # The whole argument payload. Legacy, and deliberately kept: there
            # is no `{{ … }}` spelling of it, so removing it would delete a
            # capability rather than rename one.
            return json.dumps(args)
        if expr.startswith("data."):
            path = expr[len("data.") :]
            value = _walk(args, path)
            if value is not None:
                consumed.add(path.split(".")[0])
                return value
            return match.group(0)
        if expr in args:
            consumed.add(expr)
            value = args[expr]
            return json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        return match.group(0)  # unrecognised — left visible, never blanked

    return _SINGLE.sub(_sub, url), consumed


def build_request(
    tool: dict[str, Any], args: dict[str, Any], global_variables: dict[str, Any]
) -> dict[str, Any]:
    """Everything `httpx.request` needs: `method`, `url`, `headers`, and one of
    `params` / `json` / `content`.

    Raises `ValueError` when the tool has no URL, which is the one thing about a
    tool document this cannot work around.
    """
    method = str(tool.get("method") or "POST").upper()
    url = tool.get("url") or ""
    if not url:
        raise ValueError("No URL configured for this tool.")

    context = {"global": global_variables, "args": args}
    url, consumed = render_url(url, args, global_variables)
    headers = templating.render_mapping(dict(tool.get("headers") or {}), context, bare="args")

    body, body_consumed = templating.request_body(
        tool.get("body"), args, {"global": global_variables}
    )
    consumed |= body_consumed

    request: dict[str, Any] = {"method": method, "url": url, "headers": headers}

    if method in ("GET", "DELETE"):
        remaining = {k: v for k, v in args.items() if k not in consumed}
        request["params"] = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
            for k, v in remaining.items()
        }
        return request

    if not (tool.get("body") or "").strip():
        # No template: the arguments, minus the ones the URL already took.
        body = {k: v for k, v in args.items() if k not in consumed}
    if isinstance(body, str):
        request["content"] = body
    else:
        request["json"] = body
    return request


def decode(response: Any, tool: dict[str, Any]) -> tuple[Any, str]:
    """`(parsed JSON or None, the text to hand back)`.

    `outputSchema` narrows a JSON object to the properties it names — but only
    when that leaves something, so a schema that matches nothing returns the
    response rather than an empty object.
    """
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON response is normal
        text = response.text or json.dumps(
            {"error": f"Empty response (Status: {response.status_code})"}
        )
        return None, text

    schema = tool.get("outputSchema") or {}
    if schema.get("properties") and isinstance(parsed, dict):
        narrowed = {k: parsed[k] for k in schema["properties"] if k in parsed}
        if narrowed:
            parsed = narrowed
    return parsed, json.dumps(parsed)

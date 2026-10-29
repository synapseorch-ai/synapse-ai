"""The person's own API and own MCP server in a plan (F1225, BLD1; CAP3 stage 1, P2).

A person who said "here is our orders API's spec" or "use our tracker's MCP server at …" got a plan that made
neither: the planner wrote tool names nothing here had, and the lookup went looking for a provider. Now a plan names
them as parts.

* `apis`: the spec the person gave, by its URL or its Vault path. `read_api_spec` (a planner read) parses it as the
  Tools screen's import does, dry, and gives the tools its operations become; `examine` reads it again and holds the
  plan to it. Each operation the plan uses is a read API's tool to the rest of the build (F944): made after the yes,
  its base locked, and its key asked for on a Connect card before the save. A spec URL is fetched as every model-written
  URL is (`safe_fetch`: the egress guard at each hop, a deadline, a cap) and held to the host's rule as well; a Vault
  path is read as F1227's read_vault reads one. A spec that cannot be read, is not OpenAPI, or takes its key in a way
  the card cannot keep goes back to the planner, said.
* `mcp_servers`: a remote server's URL, held to F1201's one rule. Its tools are known only once it is connected, so
  its Connect card comes before the plan (Naveen, 10-29: the one exception to F949's plan-then-connect), and the
  card's "Connected …" plans again on its real tools.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from core.builder_tools import register_builder_tool
from core.native_builder import objects as O

#: How many of a spec's tools `read_api_spec` lists by name; the rest are counted.
LISTED = 60

#: The schemes the Connect card keeps a key in (`/api/tools/connect`): a header, with Bearer or nothing before it.
_SIGN_IN = {"oauth2": "OAuth", "openIdConnect": "OpenID Connect"}

#: Where a spec's key goes when the card cannot keep it there, and where the person sets it instead.
_ELSEWHERE = "import the spec on the Tools screen and set its key there"


def _is_url(spec: str) -> bool:
    return bool(re.match(r"(?i)^https?://", spec))


def _cap() -> int:
    from core.vault_tools import _MAX_BYTES

    return _MAX_BYTES


async def spec_text(spec: str) -> tuple[str | None, str]:
    """The spec's text, from its URL or its Vault path, or `(None, why)`."""
    spec = (spec or "").strip()
    if not spec:
        return None, "no spec was given: give its https URL or its Vault path"
    if _is_url(spec):
        from core.mcp_client import host_url_refusal
        from core.safe_fetch import FetchRefused, fetch_bytes

        if (why := await host_url_refusal(spec)) is not None:
            return None, why
        try:
            raw = await fetch_bytes(spec, max_bytes=_cap())
        except FetchRefused as exc:
            return None, f"its spec could not be read: {exc}"
        try:
            return raw.decode("utf-8"), ""
        except UnicodeDecodeError:
            return None, "its spec is not text: an OpenAPI spec is JSON or YAML"
    if "://" in spec:
        return None, "a spec is given by its https URL or its Vault path"
    from core.vault_tools import _get_text

    text, error = _get_text(spec.lstrip("/"))
    return (text, "") if error is None else (None, error)


def key_of(spec: dict) -> tuple[dict | None, str]:
    """Where the API takes its key, as the Connect card keeps one: `({"in", "name", "scheme"}, "")`; `(None, "")` for
    an API that takes none; `(None, why)` for one the card cannot keep."""
    schemes = (spec.get("components") or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
    schemes = {n: s for n, s in schemes.items() if isinstance(s, dict)} if isinstance(schemes, dict) else {}
    if not schemes:
        return None, ""
    for s in schemes.values():
        kind = str(s.get("type") or "")
        if kind == "apiKey" and s.get("in") == "header" and s.get("name"):
            return {"in": "header", "name": str(s["name"]), "scheme": ""}, ""
        if kind == "http" and str(s.get("scheme") or "").lower() == "bearer":
            return {"in": "header", "name": "Authorization", "scheme": "Bearer"}, ""
    kinds = [str(s.get("type") or "") for s in schemes.values()]
    if signs := [_SIGN_IN[k] for k in kinds if k in _SIGN_IN]:
        return None, f"it signs in with {signs[0]}, which a build's Connect card can't do yet: {_ELSEWHERE}"
    return None, f"it takes its key in a way a build's Connect card can't keep yet (only in a header): {_ELSEWHERE}"


def prefix_of(name: str) -> str:
    """What each of an API's tools is named with, so one never takes another tool's name here."""
    from core.openapi_import import _sanitize_name

    return _sanitize_name(name)


def _path(url: str, base: str) -> str:
    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", r"{\1}", url[len(base):] or "/")


async def read(name: str, spec: str) -> dict:
    """The spec as the build uses it: `{"tools": [the parser's tool], "base", "auth", "problem"}`."""
    from core.builder_guard import places_calls
    from core.builder_tools import _refuse_inward_url
    from core.openapi_import import _base_url_from_spec, parse_openapi_spec
    from core.routes.tools import _parse_spec_text

    out: dict[str, Any] = {"tools": [], "base": "", "auth": None, "problem": ""}
    text, why = await spec_text(spec)
    if text is None:
        return {**out, "problem": why}
    try:
        parsed = _parse_spec_text(text)
    except Exception as exc:  # HTTPException from the shared parser
        return {**out, "problem": f"its spec could not be read as OpenAPI: {getattr(exc, 'detail', exc)}"}
    base = _base_url_from_spec(parsed, None)
    got = urlsplit(base)
    if got.scheme not in ("http", "https") or not got.hostname or "{" in base:
        return {**out, "problem": "its spec names no server URL a tool can call (servers[0].url, with no "
                                  "{variables}): give a spec that does"}
    if (refused := await _refuse_inward_url(base)) is not None:
        return {**out, "problem": str(refused.get("error") or refused)}
    auth, why = key_of(parsed)
    if why:
        return {**out, "problem": why}
    try:
        tools = parse_openapi_spec(parsed, name_prefix=prefix_of(name))
    except ValueError as exc:
        return {**out, "problem": f"its spec could not be read as OpenAPI: {exc}"}
    # An operation that starts a phone call is never made (F497), as the import leaves it out.
    tools = [t for t in tools if not places_calls(t.get("url") or "", t.get("method"))]
    if not tools:
        return {**out, "problem": "its spec has no operation a build can make a tool of"}
    return {**out, "tools": tools, "base": base, "auth": auth}


def _status(part: O.ApiPart, tool: dict, base: str, auth: dict | None) -> O.ToolStatus:
    """One operation as a read API's tool is to the build (F944): made after the yes, its base locked."""
    return O.ToolStatus(name=tool["name"], status="found", provider=part.name, via="http_tool", api={
        "base_url": base, "auth": auth, "host_params": [], "docs_url": part.spec if _is_url(part.spec) else "",
        "endpoint": {"method": tool["method"], "path": _path(tool["url"], base), "does": tool["description"],
                     "tool": tool["name"]},
        # The parser's own tool, so what is made takes every argument the operation has, as the import's would.
        "tool": {k: tool[k] for k in ("url", "inputSchema", "description")},
        "own": True, "keyless": auth is None})


async def api_parts(plan: O.Plan) -> tuple[list[str], dict[str, O.ToolStatus]]:
    """Each API part read and held to the plan (code sets its `tools` and `problem`): the planner's errors, and each
    tool the plan uses from one, as `fill` takes a found tool."""
    errors: list[str] = []
    found: dict[str, O.ToolStatus] = {}
    used = O.tool_names(plan)
    seen: set[str] = set()
    for part in plan.apis:
        if part.name.strip().lower() in seen:
            errors.append(f"apis: '{part.name}' is in the plan twice; name each API once")
            continue
        seen.add(part.name.strip().lower())
        got = await read(part.name, part.spec)
        part.problem, part.tools = got["problem"], []
        if part.problem:
            errors.append(f"apis '{part.name}': {part.problem}")
            continue
        ops = {t["name"]: t for t in got["tools"]}
        listed = ", ".join(list(ops)[:LISTED])
        part.tools = [n for n in used if n in ops]
        wrong = [n for n in used if n.startswith(prefix_of(part.name) + "_") and n not in ops]
        if wrong:
            errors.append(f"apis '{part.name}': its spec has no operation {', '.join(wrong)}; its tools are: {listed}")
        elif not part.tools:
            errors.append(f"apis '{part.name}': no step or agent uses it; its tools are: {listed}")
        for n in part.tools:
            found[n] = _status(part, ops[n], got["base"], got["auth"])
    return errors, found


def _same_url(a: Any, b: Any) -> bool:
    def norm(u: Any) -> str:
        got = urlsplit(str(u or "").strip())
        return f"{got.scheme.lower()}://{(got.netloc or '').lower()}{got.path.rstrip('/')}"

    return bool(a) and bool(b) and norm(a) == norm(b)


async def server_parts(plan: O.Plan, server_module: Any) -> list[str]:
    """Each MCP server part held to F1201's rule, and `connected` set: a server added here at that URL whose tools
    the run has. A connected one takes the name it was added under, in its agents' `servers` too; an agent's server
    that is not connected yet is left for the plan drawn again once it is."""
    from core.builder_connect import _outward
    from core.native_builder.build_plan import _servers
    from core.store.resources import load_mcp_servers

    errors: list[str] = []
    here = _servers(server_module)
    records = await load_mcp_servers() if plan.mcp_servers else []
    renamed: dict[str, str | None] = {}
    for part in plan.mcp_servers:
        part.connected = False
        if (why := await _outward(part.url)) is not None:
            errors.append(f"mcp_servers '{part.name}': {why}")
            continue
        record = next((r for r in records if _same_url(r.get("url"), part.url)), None)
        part.connected = record is not None and str(record.get("name") or "").lower() in here
        renamed[part.name.lower()] = str(record["name"]) if part.connected else None
        if part.connected:
            part.name = str(record["name"])
    if renamed:
        for a in plan.agents:
            a.servers = [renamed.get(s.lower()) or s for s in a.servers if renamed.get(s.lower(), s) is not None]
    return errors


# ── the planner's read ────────────────────────────────────────────────────────────────────────────────────────────

SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_api_spec",
        "description": (
            "Read the OpenAPI spec of the person's own API, by the https URL or the Vault path they gave, and get the "
            "tools its operations become here, by name. Put it in the plan's `apis` and use those names in steps and "
            "agents: each one is made with the build, and its key is asked for on a card after the yes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "The spec's https URL, or its Vault path"},
                "name": {"type": "string", "description": "A short name for the API, e.g. Orders API: its tools are "
                                                          "named with it"},
            },
            "required": ["spec", "name"],
        },
    },
}


async def read_api_spec(args: dict, server_module: Any, events: list[dict]) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "Name the API, e.g. Orders API: its tools are named with it."}
    got = await read(name, str(args.get("spec") or ""))
    if got["problem"]:
        why = got["problem"].rstrip(". ")
        return {"error": f"{why[0].upper()}{why[1:]}."}
    tools = [{"tool": t["name"], "call": f"{t['method']} {_path(t['url'], got['base'])}",
              "does": t["description"][:160]} for t in got["tools"]]
    auth = got["auth"]
    return {"api": name, "tools": tools[:LISTED], "more": max(0, len(tools) - LISTED),
            "key": (f"asked for on a card after the yes, sent in its {auth['name']} header" if auth else
                    "none: it takes no key")}


register_builder_tool(SCHEMA, read_api_spec)

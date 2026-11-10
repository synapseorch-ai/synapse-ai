"""What the AI builder may not save: a tool that nothing provides (F489, BWA1, D274).

SW1 saw it in every Agent Builder translation, and VOX1e met it too: `create_agent` took any tool name — the pasted
code's own variables, a tool the model expected to exist — and saved the agent with it. At run time the agent silently
lacked the tool, and a forced `tool` step naming one failed ("Tool '…' not found in available tools"). The builder's
validator never looked.

Every name is checked with the engine's own resolver (`core.tools.resolve_one_tool`), the lookup a run makes, so
"exists" means exactly "a run could call it":

- `unknown(names)` — the names in a list that nothing provides; `create_agent(s)` and `update_agent` refuse them,
  with the nearest names that do exist, at the moment the builder writes them;
- `missing(names)` — the same check for a person (F526): an agent saved through its editor or the API is told,
  not refused, and its page marks the names;
- `missing_in(workflows, agents)` — for `validate_orchestration`: a forced tool step naming one is an issue (it fails
  on the first run, certainly); an agent naming one is a warning, since a workspace's own older agents may name a
  tool from a server that has gone, and refusing every build that uses them would be a new failure.

And one thing it may not make at all (F497): `places_calls(url, method)` — an HTTP tool that starts an outbound phone
call, which waits for VOX2's consent model.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

#: Not tool names: what a tool list may hold besides one.
_NOT_A_NAME = ("all",)


def _engine_provides() -> frozenset[str]:
    """Tools the engine itself provides, which no server or custom tool does: the task list, skills, its retired
    alias, and "Can send helpers"' `spawn_subtask` (SUB1a). `resolve_one_tool` resolves none of them, so each read as
    a tool nothing provides — every agent that sends helpers was told so (F549). From the engine's own names, as
    cloud's `unknown_tool_names` counts them."""
    from core.boards import TOOL_NAMES as BOARD_TOOLS
    from core.boards.config import boards_enabled
    from core.spawn_subtask import SPAWN_SUBTASK_TOOL_NAME
    from core.task_list import LEGACY_TASK_TOOL_ALIAS, TASK_TOOL_NAME
    from core.visitor import ENGINE_TOOLS

    # While Boards are on, every agent step in a workflow has the board tools on its workflow's own board
    # (`boards.resolve.turn_boards`): no catalogue lists them, and a build that asked to post to a board read them as
    # missing and invented a "Board" provider (F866b, QA1c).
    boards = BOARD_TOOLS if boards_enabled() else ()
    return frozenset({*ENGINE_TOOLS, TASK_TOOL_NAME, LEGACY_TASK_TOOL_ALIAS, SPAWN_SUBTASK_TOOL_NAME, *boards})


async def _canonical(server_module: Any):
    """`canon(name)`: the name a run calls a tool by, for any spelling of it the builder's own listings teach, or
    None when nothing provides it (F1035).

    The listings and the catalogue spelled one tool differently: `list_server_tools` prints an external server's
    tools bare (`firecrawl_scrape`) while the catalogue, the resolver and every saved agent call it
    `firecrawl__firecrawl_scrape`; and a model writes a native server's tool as `web_scraper.scrape_url`. Every
    one of those read as "nothing provides it", so a plan using the built-in scraper, or a Firecrawl already
    connected, was sent to research, which offered to connect Firecrawl (gethubservice, prod rc4.2).

    Accepted, in order: the name itself (the run's own lookup); `server.tool` and `server__tool`; a bare tool name
    exactly one server answers. A bare name two servers answer is never guessed: it reads as missing, and the
    suggestions name both."""
    from core.routes.tools import load_custom_tools
    from core.tool_router import SEP
    from core.tools import resolve_one_tool

    custom = await load_custom_tools()
    cache: dict[str, str | None] = {}
    engine = _engine_provides()
    known: list[str] | None = None

    async def runs(name: str) -> bool:
        try:
            return await resolve_one_tool(server_module, name, {"tools": [name]}, custom) is not None
        except Exception:  # noqa: BLE001 — a lookup that cannot run is not evidence a tool is missing
            return True

    async def canon(name: str) -> str | None:
        nonlocal known
        if name in _NOT_A_NAME or name in engine or name.startswith("group:"):
            return name
        if name in cache:
            return cache[name]
        found: str | None = name if await runs(name) else None
        if found is None and "." in name:
            server, _, tool = name.partition(".")
            key = f"{server}{SEP}{tool}"
            router = getattr(server_module, "tool_router", None)
            # The catalogue's own name: bare when that server answers the bare name (a built-in's), else its key.
            # Never another server's tool that happens to share the bare name.
            if tool and hasattr(router, "resolve") and router.resolve(tool) == key:
                found = tool
            elif tool and await runs(key):
                found = key
        if found is None and SEP not in name and "." not in name:
            if known is None:
                known = await _known_names(server_module)
            answering = [k for k in known if k.endswith(f"{SEP}{name}")]
            if len(answering) == 1:
                found = answering[0]
        cache[name] = found
        return found

    return canon


async def _resolver(server_module: Any):
    canon = await _canonical(server_module)

    async def exists(name: str) -> bool:
        return await canon(name) is not None

    return exists


async def canonical_names(names: list[Any], server_module: Any) -> list[Any]:
    """Each name as a run calls it (F1035): what the builder saves, so an agent never holds a spelling no run
    resolves. A name nothing provides, and anything that is not a name, is left as it was (the refusals say it)."""
    canon = await _canonical(server_module)
    out: list[Any] = []
    for name in names:
        fixed = await canon(name) if isinstance(name, str) and name else None
        out.append(fixed or name)
    return out


async def _known_names(server_module: Any) -> list[str]:
    """Every tool name this workspace offers, for suggesting the nearest real one."""
    try:
        from core.routes.tools import load_custom_tools
        from core.tools import aggregate_all_tools

        tools = (await aggregate_all_tools(server_module, {"tools": ["all"]}, await load_custom_tools()))[0]
        return sorted({getattr(t, "name", "") for t in tools if getattr(t, "name", "")})
    except Exception:  # noqa: BLE001 — suggestions are a courtesy
        return []


async def missing(names: list[Any], server_module: Any, *, cutoff: float = 0.75) -> list[tuple[str, list[str]]]:
    """Each name nothing provides, with the nearest names that do exist, for a person to read (F526, VOX1f): an
    agent saved through its editor or the API, or shown on its page. `all` and `group:` are lists, not names.
    The cutoff is closer than `unknown()`'s for the builder model: at 0.5 `smtp_send_mail` came back
    "nearest: list_schedules" (BWA1, F492)."""
    exists = await _resolver(server_module)
    gone = [n for n in dict.fromkeys(names) if isinstance(n, str) and n and not await exists(n)]
    if not gone:
        return []
    known = await _known_names(server_module)
    return [(n, difflib.get_close_matches(n, known, n=3, cutoff=cutoff)) for n in gone]


async def unknown(names: list[Any], server_module: Any) -> dict | None:
    """The refusal for a tool list naming tools nothing provides, or None when every name is real. Worded for the
    builder model, with looser suggestions than a person reads (`missing()`'s own cutoff). Synapse AI's own steps are
    refused first: only its own run takes them (F1315)."""
    from core.builder_access import run_only_named

    if (own := run_only_named(names)) is not None:
        return {"error": own}
    gone = await missing(names, server_module, cutoff=0.5)
    if not gone:
        return None
    hints = "; ".join(f"'{n}' (did you mean {', '.join(repr(c) for c in near)}?)" if near else f"'{n}'"
                      for n, near in gone)
    return {"error": f"No tool here is called {hints}. Give the agent only tools that exist: read them with "
                     "`get_tools_detail` or `list_server_tools`, or create the tool first (`create_http_tool`). If "
                     "the plan needs a tool nothing provides, say so in your answer instead of naming it."}


async def missing_in(workflows: list[dict], agents: dict[str, dict], server_module: Any) -> tuple[list[str], list[str]]:
    """`(issues, warnings)` for tools the workflows' steps or agents name that nothing provides."""
    from core.boards import TOOL_NAMES as board_tools  # an agent's turn has them, never a Tool step's

    exists = await _resolver(server_module)
    issues: list[str] = []
    warnings: list[str] = []
    seen_agents: set[str] = set()
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            label = step.get("name") or step.get("id")
            forced = step.get("forced_tool")
            if isinstance(forced, str) and forced in board_tools:
                issues.append(f"Step '{label}' calls the board tool '{forced}' as a Tool step, which has no board: "
                              "board tools are an agent's. Give it to the agent of an agent step.")
            elif isinstance(forced, str) and forced and not await exists(forced):
                issues.append(f"Step '{label}' calls the tool '{forced}', which nothing here provides: its first run "
                              "would fail. Use a tool that exists, or create it first.")
            agent_id = step.get("agent_id") or ""
            agent = agents.get(agent_id)
            if not agent or agent_id in seen_agents:
                continue
            seen_agents.add(agent_id)
            for name in agent.get("tools") or []:
                if isinstance(name, str) and not await exists(name):
                    warnings.append(f"The agent '{agent.get('name')}' (step '{label}') names the tool '{name}', which "
                                    "nothing here provides; it will run without it.")
    return issues, warnings


async def argument_issues(workflows: list[dict], server_module: Any,
                          made: dict[str, dict] | None = None) -> list[str]:
    """Issues for Tool steps whose set arguments (E1) do not fit their tool's own schema: a name it does not take,
    or a required one left out, and below the top level (F1357) what `_misfits` finds. With no model to repair them,
    each fails every run. A tool nothing provides is `missing_in`'s to say; a tool whose schema names no properties
    takes anything. `made` is what each tool a build makes after the yes will take, by name (F1295): held to that
    while it is not here yet."""
    from core.routes.tools import load_custom_tools
    from core.tools import resolve_one_tool

    custom = await load_custom_tools()
    issues: list[str] = []
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            arguments, name = step.get("tool_arguments"), step.get("forced_tool")
            if step.get("type") != "tool" or not isinstance(arguments, dict) or not isinstance(name, str) or not name:
                continue
            try:
                tool = await resolve_one_tool(server_module, name, {"tools": [name]}, custom)
            except Exception:  # noqa: BLE001 — a lookup that cannot run is not evidence either way
                tool = None
                if name not in (made or {}):
                    continue
            schema = getattr(tool, "inputSchema", None) if tool is not None else (made or {}).get(name)
            props = (schema or {}).get("properties") if isinstance(schema, dict) else None
            if not isinstance(props, dict) or not props:
                continue
            label = step.get("name") or step.get("id")
            unknown = [a for a in arguments if a not in props]
            if unknown:
                issues.append(f"Step '{label}' sets {', '.join(repr(a) for a in unknown)}, which `{name}` does not "
                              f"take: it takes {', '.join(props)}.")
            left_out = [r for r in schema.get("required") or [] if r not in arguments]
            if left_out:
                issues.append(f"Step '{label}' leaves out {', '.join(repr(r) for r in left_out)}, which `{name}` "
                              "needs: set it on the step, or let the model write the arguments.")
            issues += _misfits(label, name, schema, arguments)
    return issues


#: What `_misfits` holds a step's arguments to: what a schema declares of them, never a `format` or a bound.
_HELD_TO = ("required", "type", "additionalProperties")


def _where(path) -> str:
    """`steps[1].url` for jsonschema's path `["steps", 1, "url"]`."""
    said = ""
    for part in path:
        said += f"[{part}]" if isinstance(part, int) else (f".{part}" if said else str(part))
    return said


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    return {str: "a string", list: "an array", dict: "an object"}.get(type(value), "null")


def _misfits(label: str, name: str, schema: dict, arguments: dict) -> list[str]:
    """What in `arguments` does not fit `schema` below its top level (F1357, cap-01: a browser step in
    `scrape_with_session`'s `steps` with no `url`, which every item needs), said with where it is. The top level is
    `argument_issues`'s own two checks; here a required name left out, a value of the wrong type, and a name an
    object setting `additionalProperties` does not take. A value that is or holds a state reference is state's to
    fill, never held to a type. A `$ref` is followed within the schema only (an empty registry: nothing is fetched),
    and a schema that is not one, or a reference it cannot follow, says nothing: never evidence a step is wrong."""
    try:
        import referencing
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for
    except ImportError:  # mcp's own dependency; without it there is nothing to hold the arguments to
        return []
    from core.templating import _OLD_STATE_REF, STATE_REF

    def filled_by_state(value: Any) -> bool:
        return isinstance(value, str) and bool(STATE_REF.search(value) or _OLD_STATE_REF.search(value))

    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        errors = list(cls(schema, registry=referencing.Registry()).iter_errors(arguments))
    except SchemaError:
        return []
    except Exception:  # noqa: BLE001 — a reference it cannot follow is not evidence either way
        return []
    said: list[str] = []
    for error in errors:
        path, rule = list(error.absolute_path), error.validator
        if rule not in _HELD_TO or filled_by_state(error.instance):
            continue
        where = _where(path)
        if rule == "required" and path and isinstance(error.instance, dict):
            left_out = [r for r in error.validator_value if r not in error.instance]
            said.append(f"Step '{label}' gives `{name}` {where} without {', '.join(repr(r) for r in left_out)}, "
                        "which it needs there: set it on the step, or let the model write the arguments.")
        elif rule == "type":
            wanted = error.validator_value if isinstance(error.validator_value, list) else [error.validator_value]
            said.append(f"Step '{label}' gives `{name}` {where or 'its arguments'} as {_json_type(error.instance)}, "
                        f"where it takes {' or '.join(map(str, wanted))}: set it as one, or let the model write the "
                        "arguments.")
        elif rule == "additionalProperties" and path and isinstance(error.instance, dict):
            known = (error.schema.get("properties") or {}) if isinstance(error.schema, dict) else {}
            extra = [k for k in error.instance if k not in known]
            said.append(f"Step '{label}' sets {', '.join(repr(k) for k in extra)} in {where}, which `{name}` does not "
                        f"take there{': it takes ' + ', '.join(known) if known else ''}.")
    return said


# ── The plan, before the person approves it (F492) ──────────────────────────
#
# gpt-oss-120b's BWA1 baseline: 37 of 45 saver runs stopped because the plan the person had approved named tools
# nothing here provides ("Tool not found") or the agents it needed were never made (the agent creator refused the
# same tools). The check above comes after the approval; this one reads the plan the drafter wrote, in the shape its
# prompt asks for, and checks each name with the run's own lookup.

#: A new agent's `tools: [a, b]` line (the drafter's "exact shape so the Agent Creator can parse it").
_AGENT_TOOLS = re.compile(r"^[ \t>*`-]*tools:\s*\[([^\]\n]*)\]", re.M | re.I)
#: A tool step's forced tool: `**Tool:** `name``, `Tool: `name``, "forced tool `name`".
_STEP_TOOL = re.compile(r"(?:\*\*tool:?\*\*:?|\btool:|forced[ _]tool(?:\s+name)?(?:\s+is)?:?)\s*`([^`\s(]+)", re.I)
_AGENT_ID = re.compile(r"\b(agent_[A-Za-z0-9_]{6,})\b")
#: The phrasings gpt-4.1-mini writes instead of the format (F666: 4 of 4 plans): "using the `x` tool" (or "MCP
#: tool"), a flow line's "(tool: x)" with or without backticks, and the approval lists' "- `x`: …" lines — one tool
#: each, by the drafter's own format, so only inside those two sections.
_PROSE_TOOL = re.compile(r"`([A-Za-z][\w.\-:/]*)`\s+(?:MCP\s+)?tool\b", re.I)
_FLOW_TOOL = re.compile(r"\(tool:\s*`?([A-Za-z][\w.\-:/]*)`?\s*\)", re.I)
_LISTED_TOOL = re.compile(r"^\s*[-*]\s*`([A-Za-z][\w.\-:/]*)`\s*:", re.M)
_APPROVAL_SECTIONS = ("## asks you first", "## acts without asking")
_A_NAME = re.compile(r"[A-Za-z][\w.\-:/]*")


def _approval_lists(plan: str) -> str:
    """The text of the plan's "Asks you first" and "Acts without asking" sections, each up to the next heading."""
    lower, parts = plan.lower(), []
    for heading in _APPROVAL_SECTIONS:
        start = lower.find(heading)
        if start >= 0:
            end = lower.find("\n## ", start + len(heading))
            parts.append(plan[start:end if end > start else None])
    return "\n".join(parts)


def names_in_plan(plan: str) -> dict[str, list[str]]:
    """`{"tools": [...], "reused_agents": [...]}`: the tool names a plan's agents and tool steps use, and the ids of
    the existing agents it reuses (only from its "Existing agents reused" list: a new agent's placeholder id is not
    one). The patterns are the fallback: `check_plan` also takes the tools the check step's model read (F666)."""
    tools: list[str] = []
    for listed in _AGENT_TOOLS.findall(plan or ""):
        tools += [t.strip().strip("'\"`") for t in listed.split(",")]
    tools += _STEP_TOOL.findall(plan or "")
    tools += _PROSE_TOOL.findall(plan or "") + _FLOW_TOOL.findall(plan or "")
    tools += _LISTED_TOOL.findall(_approval_lists(plan or ""))
    seen: set[str] = set()
    tools = [t for t in tools if re.search(r"[A-Za-z]", t) and t not in seen and not seen.add(t)]
    reused: list[str] = []
    text = plan or ""
    start = text.lower().find("existing agents reused")
    if start >= 0:
        end = text.lower().find("new agents to create", start)
        reused = sorted(set(_AGENT_ID.findall(text[start:end if end > start else None])))
    return {"tools": tools, "reused_agents": reused}


def _listed_in_plan(listed: Any, plan: str) -> list[str]:
    """The names the check step's model listed that the plan itself mentions: a model's guess is not the plan's."""
    out: list[str] = []
    for name in listed if isinstance(listed, list) else []:
        name = name.strip().strip("'\"`") if isinstance(name, str) else ""
        if _A_NAME.fullmatch(name) and name not in out and _named(name, plan):
            out.append(name)
    return out


#: The parts of a plan a build uses: its steps, its agents, its flow and the approval lists. A name only in the
#: overview or the Open questions is mentioned, not used (Naveen, 2026-10-16: a redraft that moved a step to Open
#: questions still named its tool in the overview, and "Missing here" said the build would stop there).
_USE_SECTIONS = ("steps", "agents", "new agents", "existing agents", "asks you first", "acts without asking", "flow")


def _used_text(plan: str) -> str:
    """The text of a plan's using sections; the whole plan when it has no headings to tell them apart."""
    chunks = re.split(r"^#{2,3}[ \t]+", plan, flags=re.M)
    if len(chunks) == 1:
        return plan
    return "\n".join(c for c in chunks[1:] if c.lower().startswith(_USE_SECTIONS))


def _named(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w.\-]){re.escape(name)}(?![\w\-])", text) is not None


async def check_plan(plan: str, server_module: Any, *, checks: int = 0, listed: Any = None) -> dict:
    """What a plan names that nothing here provides, before the person sees it.

    `redraft` is true the first time anything is missing, so the drafter gets one chance to use real names; after
    that, what is still missing goes in front of the person (`note`) instead of stopping the build after approval.
    `listed` is the tools the check step's model read from the plan, whatever its phrasing (F666); the plan's own
    format, read by `names_in_plan`, is the fallback when a model lists none.
    """
    from core.routes.agents import load_user_agents

    names = names_in_plan(plan)
    names["tools"] += [t for t in _listed_in_plan(listed, plan) if t not in names["tools"]]
    gone = await missing(names["tools"], server_module)
    missing_tools = [t for t, _ in gone]
    have = {a.get("id") for a in await load_user_agents()}
    missing_agents = [a for a in names["reused_agents"] if a not in have]
    used = _used_text(plan)
    # What the build would call: these go back to the drafter, and stop an approved build at the saver. A tool only
    # mentioned is named to the person, and asks for no redraft (the drafter already did what a redraft asks).
    acting = [t for t in missing_tools if _named(t, used)] + missing_agents
    mentioned = [t for t in missing_tools if t not in acting]
    checks += 1
    out: dict[str, Any] = {"checks": checks, "missing_tools": missing_tools, "missing_agents": missing_agents,
                           "redraft": bool(acting) and checks == 1, "note": "",
                           # The tools the build would call that nothing provides, looked up by the Researcher once,
                           # before the redraft (F763): who provides them is found, not guessed or dropped.
                           "research": [t for t in acting if t in missing_tools] if checks == 1 else []}
    if gone:
        # `missing()`'s close cutoff: a far match ("list_schedules" for `smtp_send_mail`) would steer the drafter to
        # a tool that does something else.
        out["nearest"] = {t: near for t, near in gone if near and t in acting}
    if out["redraft"]:
        out["note"] = ("Nothing in this workspace provides " + ", ".join(f"`{t}`" for t in acting)
                       + ". Redraft using only tools and agents that exist" + (
                           " (nearest: " + "; ".join(f"`{t}` → {', '.join(f'`{c}`' for c in n)}"
                                                    for t, n in out.get("nearest", {}).items()) + ")"
                           if out.get("nearest") else "")
                       + (". Who provides each missing tool is looked up first (`plan_findings`): keep a step whose "
                          "provider has a way in (`mcp` or `api`) and name what it needs connected under Open "
                          "questions; only what nobody provides becomes an Open question, said plainly."
                          if out["research"] else
                          ". What has no real tool here becomes an Open question, said plainly, not a step."))
    out["section"] = ""
    if not out["redraft"] and (missing_tools or missing_agents):
        said = []
        if acting:
            said.append("This plan uses " + ", ".join(f"`{t}`" for t in acting)
                        + ", which nothing in this workspace provides yet. Connect the app or service it needs, or "
                        "tell me to leave that part out; approved as it is, the build will stop there.")
        if mentioned:
            said.append(("It also mentions " if acting else "This plan mentions ")
                        + ", ".join(f"`{t}`" for t in mentioned) + ", which nothing here provides yet.")
        out["note"] = " ".join(said)
        # The plan card reads it as its own section (`plan.ts`), drawn as needing the person.
        out["section"] = "## Missing here\n" + out["note"]
    return out


async def offer_connections(plan: str, findings: Any, offered: list[str], events: list[dict]) -> tuple[str, list[str]]:
    """What the build's research found for the plan, for the person, and a Connect card for each app it found in the
    Apps catalog (F763): the build still waits until the app is connected, and the card makes that one click.

    Only a provider the plan names, with a way in (`mcp` or `api`). An app's card goes up once per build (`offered`
    holds the slugs already shown); a provider not in the catalog, or a host with no catalog, is named with the page
    the research read."""
    from core.builder_connect import _read_options, prepare_connection
    from core.builder_research import _words

    offered = list(offered or [])
    if not isinstance(findings, dict):
        return "", offered
    text = (plan or "").lower()
    catalog = (await _read_options()).get("catalog") or []
    said = []
    for option in findings.get("options") or []:
        provider = str(option.get("provider") or "").strip()
        how = option.get("how")
        if not provider or how not in ("mcp", "api") or provider.lower() not in text:
            continue
        words = set(_words(provider))
        app = next((e for e in catalog if words & set(_words(f"{e.get('name')} {e.get('slug')}"))), None)
        if app is not None:
            slug = str(app.get("slug"))
            if slug not in offered:
                shown = await prepare_connection({"kind": "app", "catalog_slug": slug, "title": f"Connect {app.get('name')}",
                                                  "why": f"This plan's {provider} steps need it."}, None, events)
                if shown.get("status") == "card_shown":
                    offered.append(slug)
            said.append(f"{app.get('name')} is in the Apps catalog: connect it with the card above, then approve.")
            continue
        page = next(iter(option.get("sources") or []), "")
        way = "its own MCP server" if how == "mcp" else "a public API"
        said.append(f"{provider} has {way}" + (f" ({page})" if page else "") + ": connect it, then approve.")
    return " ".join(said), offered


CHECK_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_plan",
        "description": (
            "Check the plan this build has just drafted: every tool and reused agent it names, with the lookup a run "
            "makes. Reads the plan from the run itself; list the tools it uses in `tools`."
        ),
        "parameters": {"type": "object", "properties": {"tools": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Every tool the plan uses, exactly as the plan writes it: each tool step's tool, each new agent's "
                "tools, and each tool under Asks you first and Acts without asking. Tools only: never a step, an "
                "agent, a state key or an output. [] when it uses none."),
        }}},
    },
}


async def check_plan_tool(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The builder's `step_check_plan`: the plan and the checks so far come from the run's last checkpoint, which
    the engine takes at every step boundary, so nothing copies the plan through a model; the model only names the
    tools it read in it (`tools`, F666)."""
    from core.builder_tools import _current_run
    from core.orchestration.state import SharedState

    run_id = _current_run.get()
    if run_id is None:
        return {"error": "check_plan runs inside a build."}
    state = (await SharedState.restore(run_id)).run.shared_state or {}
    prior = state.get("plan_check")
    checks = int(prior.get("checks") or 0) if isinstance(prior, dict) else 0
    plan = str(state.get("plan_draft") or "")
    out = await check_plan(plan, server_module, checks=checks, listed=args.get("tools"))
    # After the build's research (F763): what it found for what is still missing, with a Connect card for an app.
    offered = list(prior.get("offered") or []) if isinstance(prior, dict) else []
    if state.get("plan_findings") and not out["redraft"] and (out["missing_tools"] or out["missing_agents"]):
        said, offered = await offer_connections(plan, state.get("plan_findings"), offered, events)
        if said:
            out["note"] = f"{out['note']} {said}".strip()
            out["section"] = f"{out['section']}\n\n{said}" if out["section"] else f"## Missing here\n{said}"
    out["offered"] = offered
    return out


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(CHECK_PLAN_SCHEMA, check_plan_tool)


_register()


# ── Nothing that acts outside runs unasked (decision 4, BWA1) ────────────────
#
# BWA1's baselines: a booking API's POST tool and a payment step ran in builds with no approval anywhere. The plan
# the person approves now lists what asks first ("## Asks you first") and what the person said may act without
# asking ("## Acts without asking"); the builder's validator refuses a build of its own in which anything else that
# changes something outside can run with nobody approving it.

_ACTS_WITHOUT_ASKING = re.compile(r"^#{1,6}\s*acts without asking\s*$", re.I | re.M)


def exempt_in_plan(plan: str) -> set[str]:
    """The tools the approved plan lets act without asking: the names in backticks under '## Acts without asking'."""
    match = _ACTS_WITHOUT_ASKING.search(plan or "")
    if not match:
        return set()
    rest = plan[match.end():]
    end = re.search(r"^#{1,6}\s", rest, re.M)
    return set(re.findall(r"`([^`\s]+)`", rest[:end.start()] if end else rest))


def acts(name: str, tool: Any) -> bool:
    """Whether the tool `name`, declared as `tool`, changes something outside this workspace: the plan check's one test
    (`acting_tool`), and a reply's cut where it was written as text (F1523, `react_engine._reply_kept`). The builder's
    own reads, and a tool nothing provides, do not; anything undeclared does, as the policy treats it (TAG1)."""
    from core.builder_access import READS
    from core.tools import tool_hints

    if name in READS or tool is None:
        return False
    hints = tool_hints(tool, trusted=getattr(tool, "trusted", True))
    return not (hints.declared and hints.read_only)


async def acting_tool(name: str, server_module: Any, custom: list | None = None):
    """The tool `name` when it changes something outside this workspace, else None: one that declares it only reads,
    one nothing provides (F489's to say), or one a lookup cannot reach. Anything undeclared acts, as the policy
    treats it; an outside server's own claim to only read is not believed (TAG1). What acts is `acts`."""
    from core.builder_access import READS
    from core.tools import resolve_one_tool

    if name in READS:
        # The builder's own reads (the role door's list): they read, and change nothing. Undeclared under TAG1, they
        # read as acting, and a plan reading the week's runs was held to ask a person first (SAI2-P3's after-batch).
        return None
    if custom is None:
        from core.routes.tools import load_custom_tools

        custom = await load_custom_tools()
    try:
        tool = await resolve_one_tool(server_module, name, {"tools": [name]}, custom)
    except Exception:  # noqa: BLE001 — a lookup that cannot run is not evidence either way
        return None
    return tool if acts(name, tool) else None


async def unapproved(workflows: list[dict], agents: dict[str, dict], server_module: Any, *,
                     made_agents: set[str], exempt: set[str], strict: bool = True,
                     own_steps: set[str] | None = None,
                     to_make: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """`(issues, warnings)`: tools that change something outside this workspace and can run with nobody approving.

    An approval is a Human step before a tool step on every path from the entry, or the agent's own policy asking
    first (`tool:<name>: confirm`, or its category). A tool reads when it declares read-only; anything undeclared
    is treated as acting, as the policy treats it. `strict` is a build this run made: its own agents' gaps are issues;
    an agent it reuses, and every gap in a workflow it only edits, is a warning — never a new refusal of what runs
    today.
    """
    from core.builder_tools import step_successors
    from core.config import load_settings
    from core.routes.tools import load_custom_tools
    from core.tool_policy import decide, resolve_policy

    settings = load_settings()
    custom = await load_custom_tools()
    issues: list[str] = []
    warnings: list[str] = []

    async def acting(name: str):
        if name in (to_make or {}):
            # A tool the build makes after the yes (F944): not here to read yet, so held by its method, as its tags
            # will be: a GET only reads; anything else acts and is approved like any tool that acts.
            from types import SimpleNamespace

            return None if to_make[name] == "GET" else SimpleNamespace(name=name, annotations=None, trusted=True)
        return await acting_tool(name, server_module, custom)

    def say(message: str, own: bool) -> None:
        (issues if strict and own else warnings).append(message)

    for workflow in workflows:
        steps = [s for s in workflow.get("steps") or [] if isinstance(s, dict)]
        by_id = {s.get("id"): s for s in steps if s.get("id")}
        # The steps a run can reach without passing a person: past a Human step, the person has seen what follows.
        unasked: set[str] = set()
        queue = [workflow.get("entry_step_id")]
        while queue:
            cur = queue.pop()
            if cur in unasked or cur not in by_id:
                continue
            unasked.add(cur)
            if by_id[cur].get("type") != "human":
                queue += step_successors(by_id[cur], by_id)
        for step in steps:
            label = step.get("name") or step.get("id")
            agent = agents.get(step.get("agent_id") or "") or None
            if step.get("type") == "tool" and step.get("forced_tool"):
                name = step["forced_tool"]
                if name in exempt or step.get("id") not in unasked or (tool := await acting(name)) is None:
                    continue
                if decide(tool, resolve_policy(agent, settings))[0] == "allow":
                    # `own_steps`: on an update, only the steps it adds or changes are the build's own; what runs
                    # today and is left as it is gets a warning, never a refusal (SAI2's review, 1).
                    say(f"Step '{label}' runs `{name}`, which changes something outside this workspace, with nobody "
                        "approving it first. Put a Human step before it; or, if the person said it may act without "
                        "asking, the plan lists it under 'Acts without asking'.",
                        own_steps is None or step.get("id") in own_steps)
            elif step.get("type") == "agent" and agent:
                own = agent.get("id") in made_agents
                # What it holds at run time (F1301): a Browser agent the browser's tools too, listed or not; read_only,
                # it sends nothing at all (the component's network gate), so its presses are not held here.
                from core.browser.gate import ALLOWED, held_by

                tools = [t for t in held_by(agent.get("tools") or [], agent.get("type")) if isinstance(t, str)]
                if agent.get("type") == "browser" and (agent.get("browser") or {}).get("mode") != "co_drive":
                    tools = [t for t in tools if not (t in ALLOWED and not ALLOWED[t].get("read_only"))]
                if any(t == "all" or t.startswith("group:") for t in tools):
                    say(f"The agent '{agent.get('name')}' (step '{label}') has every tool or a group, so what it may "
                        "do without asking cannot be checked: give it the tools it needs by name.", own)
                    continue
                policy = resolve_policy(agent, settings)
                for name in tools:
                    if name in exempt or (tool := await acting(name)) is None:
                        continue
                    if decide(tool, policy)[0] == "allow":
                        say(f"The agent '{agent.get('name')}' (step '{label}') can run `{name}`, which changes "
                            "something outside this workspace, without asking. Give it the policy "
                            f"{{\"tool:{name}\": \"confirm\"}} (update_agent's tool_policy); or, if the person said it "
                            "may act without asking, the plan lists it under 'Acts without asking'.", own)
    return issues, warnings


# ── A tool that places phone calls (F497) ────────────────────────────────────────────────────────────────────────

#: The endpoints that start an outbound call, as (host, path) patterns: a POST to one dials a number. Outbound calls
#: wait for VOX2, which brings the consent an AI voice calling someone needs (V20: TCPA/FCC 24-17, AB 2905, SHAKEN/
#: STIR, TRAI); until then the AI builder never makes such a tool. A tool a person saves is theirs (D204).
_CALL_PLACING = tuple((re.compile(host, re.I), re.compile(path, re.I)) for host, path in (
    (r"^api\.twilio\.com$", r"/calls(\.json)?/?$"),
    (r"^api\.telnyx\.com$", r"^/v2/calls/?$"),
    (r"(^|\.)(nexmo|vonage)\.com$", r"^/v1/calls/?$"),
    (r"^api\.plivo\.com$", r"/call/?$"),
    (r"^voice\.bandwidth\.com$", r"/calls/?$"),
    (r"(^|\.)api\.sinch\.com$", r"/callouts/?$"),
    (r"^api\.vapi\.ai$", r"^/call(/phone)?/?$"),
    (r"^api\.bland\.ai$", r"^/v1/calls/?$"),
    (r"^api\.retellai\.com$", r"/create-phone-call/?$"),
))

#: What the builder says instead, in the can't-do answer's words (`builder_help`'s capabilities).
PLACES_CALLS = ("This product does not place phone calls yet: its phone line answers incoming calls, and calling out "
                "waits until it can record each person's consent to be called by an AI voice. The nearest thing that "
                "works: an agent drafts the message and sends it by email or on a chat channel, and a person can "
                "approve it first.")


def places_calls(url: str, method: str | None) -> bool:
    """Whether an HTTP tool with this URL and method starts a phone call (F497). A GET only reads a call log."""
    from urllib.parse import urlsplit

    if (method or "GET").upper() in ("GET", "HEAD", "OPTIONS"):
        return False
    parts = urlsplit(str(url or "").strip())
    host, path = (parts.hostname or ""), parts.path or ""
    return any(h.search(host) and p.search(path) for h, p in _CALL_PLACING)

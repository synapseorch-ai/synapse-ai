"""Every door a key has, from the terminal: any product tool, any route, and the workspace's activity (D294).

`synapse tools call` reaches every product tool the key is offered over MCP, a
named command or not; `synapse api` reaches every route, as `gh api` does. So
whatever a key can do the CLI can, the day it ships, before a command is
written for it. `activity` and `usage` are the workspace's summaries.
"""
from __future__ import annotations

import json
import sys

from synapse.client import errors
from synapse.client.commands import Tree, _handler, _json_file, _pairs, confirmed_call, show_value
from synapse.client.errors import ApiError, CliError
from synapse.client.http import ApiClient, _body, _detail
from synapse.client.mcp import Mcp
from synapse.client.output import Output


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    end = text.find(". ")
    return text if end < 0 else text[: end + 1]


@_handler
def tools_list(args, api: ApiClient, out: Output):
    tools = Mcp(api).tools()
    if args.match:
        tools = [t for t in tools if args.match.lower() in (t["name"] + " " + (t.get("description") or "")).lower()]
    if out.json_mode:
        return out.document(tools)

    def marks(t: dict) -> str:
        hints = t.get("annotations") or {}
        return ("reads" if hints.get("readOnlyHint") else "asks first" if hints.get("destructiveHint") else "acts")

    out.table(tools, [("TOOL", lambda t: t["name"]), ("", marks),
                      ("WHAT IT DOES", lambda t: _first_sentence(t.get("description") or ""))],
              empty="This key is offered no tools.")


@_handler
def tools_show(args, api: ApiClient, out: Output):
    tool = next((t for t in Mcp(api).tools() if t["name"] == args.name), None)
    if tool is None:
        raise CliError(f"There is no tool '{args.name}'. `synapse tools list` names them.", errors.NOT_FOUND)
    if out.json_mode:
        return out.document(tool)
    out.out.print(f"[bold]{tool['name']}[/bold]", markup=True, highlight=False)
    out.text(tool.get("description") or "")
    schema = tool.get("inputSchema") or {}
    required = set(schema.get("required") or [])
    rows = [{"name": name, **spec} for name, spec in (schema.get("properties") or {}).items()]
    out.text("")
    out.table(rows, [("ARGUMENT", lambda r: r["name"] + (" *" if r["name"] in required else "")),
                     ("TYPE", lambda r: r.get("type") or ("one of " + "/".join(r["enum"]) if r.get("enum") else "")),
                     ("WHAT", lambda r: _first_sentence(r.get("description") or ""))],
              empty="It takes no arguments.")
    out.note(f"Call it with: synapse tools call {tool['name']} --arg name=value …")


@_handler
def tools_call(args, api: ApiClient, out: Output):
    arguments = {}
    if args.args_file:
        arguments = _json_file(args.args_file, "--args-file")
        if not isinstance(arguments, dict):
            raise CliError("--args-file holds one JSON object of arguments.", errors.USAGE)
    arguments.update(_pairs(args.arg, "--arg"))
    value = confirmed_call(Mcp(api), out, args.name, arguments, args.yes, f"Do {args.name}")
    if value is None:
        return errors.OK
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    show_value(out, value)


@_handler
def api_call(args, api: ApiClient, out: Output):
    """Any route a key reaches, under /api/v1 by default: its JSON on stdout, its status in the exit code."""
    method = args.method.upper()
    path = args.path if args.path.startswith("/") else "/" + args.path
    url = None
    if path.startswith("/api/"):
        url = api.target.origin + path  # another API version, or /api/v1 given in full
    body = None
    if args.data is not None:
        raw = args.data
        if raw.startswith("@"):
            body = _json_file(raw[1:], "-d")
        else:
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise CliError(f"-d is not JSON: {exc}", errors.USAGE)
    params = _pairs(args.field, "-F") or None
    if args.stream:
        stream_path = path if url is None else path.removeprefix(api.target.api_base.removeprefix(api.target.origin))
        for frame in api.stream(method, stream_path, body=body, params=params):
            out.event(frame if args.with_ids else frame["data"])
        return errors.OK
    resp = api.send(method, path, url=url, check=False, params=params,
                    **({"json": body} if body is not None else {}))
    value = _body(resp)
    if value is not None:
        if isinstance(value, str):
            sys.stdout.write(value + "\n")
        else:
            out.document(value)
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, _detail(resp))
    return errors.OK


@_handler
def activity(args, api: ApiClient, out: Output):
    """What happened in the workspace: runs by status, what failed and why, what waits, what ran."""
    from rich.markup import escape

    from synapse.client.commands import _when

    value = _parsed(Mcp(api).call("summarise_activity", {"days": args.days}))
    if out.json_mode or not isinstance(value, dict):
        return show_value(out, value)
    say = out.out.print
    say(f"[bold]In {escape(str(value.get('window') or 'the last day'))}[/bold]", markup=True, highlight=False)
    counts = value.get("runs") or {}
    out.text("Runs: " + (", ".join(f"{n} {status}" for status, n in counts.items() if n) or "none"))
    for title, key, style in (("Failed", "failed", "red"), ("Waiting for a person", "waiting_for_a_person", "yellow"),
                              ("Running", "running", "dim")):
        rows = value.get(key) or []
        if not rows:
            continue
        out.text("")
        say(f"[{style}]{title}[/{style}]", markup=True, highlight=False)
        for row in rows:
            why = f": {row['why']}" if row.get("why") else ""
            since = row.get("since") or row.get("started_at")
            out.text(f"  {row.get('workflow')}{why}  {row.get('run_id') or ''}"
                     + (f"  ({_when(since)})" if since else ""))
    ran = [s for s in value.get("schedules_that_ran") or [] if s.get("last_ran_at")]
    if ran:
        out.text("")
        out.text("Schedules that ran: " + ", ".join(f"{s['schedule']} ({_when(s['last_ran_at'])})" for s in ran))
    out.text("")
    out.text(f"Spent on models: ${float(value.get('model_dollars') or 0):.4f}")
    out.note("Workflow runs only. Conversations: synapse agents activity NAME. What waits: synapse runs waiting.")


@_handler
def usage(args, api: ApiClient, out: Output):
    """What the workspace spent on models: dollars and tokens, by day, agent or model."""
    try:
        value = _parsed(Mcp(api).call("get_usage", {"days": args.days, "by": args.by}))
    except CliError as exc:
        if exc.code == errors.NOT_FOUND and "unknown tool" in exc.message.lower():
            raise CliError("This Synapse does not report usage to a key yet: it is older than the CLI. "
                           "`synapse runs list` shows each run's cost.", errors.NOT_FOUND) from None
        raise
    if out.json_mode:
        return out.document(value)
    rows = value.get("rows") if isinstance(value, dict) else None
    if rows is None:
        return show_value(out, value)
    out.note(value.get("window") or "", style="")
    out.table(rows, [(args.by.upper(), lambda r: r.get("key")),
                     ("USD", lambda r: f"${float(r.get('cost_usd') or 0):.4f}"),
                     ("TOKENS IN", lambda r: f"{int(r.get('input_tokens') or 0):,}"),
                     ("TOKENS OUT", lambda r: f"{int(r.get('output_tokens') or 0):,}"),
                     ("CALLS", lambda r: r.get("calls"))],
              empty="Nothing was spent in that window.")
    total = value.get("total") or {}
    if rows:
        out.note(f"In all: ${float(total.get('cost_usd') or 0):.4f} over {total.get('calls') or 0} model calls.")


def _parsed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def register(tree: Tree) -> None:
    add, group = tree.command, tree.group
    tools = group(tree.sub, "tools", "Every product tool a key is offered: list, read and call them")
    p = add(tools, "list", tools_list, "The tools this key may call")
    p.add_argument("match", nargs="?", help="only those whose name or description has this")
    add(tools, "show", tools_show, "A tool's description and arguments").add_argument("name")
    p = add(tools, "call", tools_call, "Call a tool")
    p.add_argument("name")
    p.add_argument("--arg", action="append", metavar="NAME=VALUE", help="an argument; VALUE read as JSON when it is")
    p.add_argument("--args-file", metavar="FILE", help="a JSON object of arguments; - reads stdin")
    p.add_argument("--yes", action="store_true", help="do not ask first, for a tool that confirms (a delete)")

    p = add(tree.sub, "api", api_call, "Call any route a key reaches, as `gh api` does")
    p.add_argument("method", metavar="METHOD", help="GET, POST, PATCH, PUT or DELETE")
    p.add_argument("path", metavar="PATH", help="under /api/v1 (e.g. /agents), or a full /api/v2/… path")
    p.add_argument("-d", "--data", metavar="JSON|@FILE", help="the body, as JSON or @file (@- reads stdin)")
    p.add_argument("-F", "--field", action="append", metavar="NAME=VALUE", help="a query parameter")
    p.add_argument("--stream", action="store_true", help="the route answers SSE: one JSON line per event")
    p.add_argument("--with-ids", action="store_true", help="with --stream, each line as {id, data}")

    p = add(tree.sub, "activity", activity, "What happened in the workspace: runs, failures, what waits")
    p.add_argument("--days", type=int, default=1, help="how many days back (default 1)")
    p = add(tree.sub, "usage", usage, "What the workspace spent on models, by day, agent or model")
    p.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    p.add_argument("--by", choices=["day", "agent", "model"], default="day")

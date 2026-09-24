"""The API commands: agents, orchestrations, run, runs, schedules, chat, login, mcp (D126).

Every one takes `--url`, `--api-key` and `--json` (see `target.py` for how the
first two resolve) and leaves with an exit code from `errors.py`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from collections.abc import Iterable, Iterator

from rich.markup import escape

from synapse.client import errors
from synapse.client.errors import CliError
from synapse.client.http import ApiClient
from synapse.client.output import Output
from synapse.client.target import Target, resolve


# ── plumbing ────────────────────────────────────────────────────────────────


def _client_flags() -> argparse.ArgumentParser:
    flags = argparse.ArgumentParser(add_help=False)
    group = flags.add_argument_group("target")
    group.add_argument("--url", help="Synapse origin (default: SYNAPSE_API_URL, else this machine's backend)")
    group.add_argument("--api-key", help="API key (default: SYNAPSE_API_KEY; not needed for this machine)")
    group.add_argument("--json", action="store_true", help="print JSON (one event per line for streams)")
    return flags


def _handler(fn):
    """Build the output, target and client a command needs, and turn errors into exit codes."""
    def run(args) -> int:
        out = Output(json_mode=getattr(args, "json", False))
        api = None
        try:
            target = resolve(getattr(args, "url", None), getattr(args, "api_key", None))
            api = ApiClient(target)
            return fn(args, api, out) or errors.OK
        except CliError as exc:
            out.error(exc.message)
            return exc.code
        except KeyboardInterrupt:
            out.note("Interrupted.")
            return 130
        finally:
            if api is not None:
                api.close()
    return run


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _pick(items: list[dict], ident: str, kind: str) -> dict:
    """An exact id, else a name in any case; more than one match is an error that lists them."""
    by_id = [i for i in items if i.get("id") == ident]
    if by_id:
        return by_id[0]
    matches = [i for i in items if (i.get("name") or "").lower() == ident.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CliError(f"No {kind} is named or has the id '{ident}'.")
    listed = ", ".join(f"{m['id']}" for m in matches)
    raise CliError(f"{len(matches)} {kind}s are named '{ident}': {listed}. Use the id.")


def _when(started: str | None) -> str:
    return (started or "").replace("T", " ")[:16]


def _cost(value) -> str:
    return "" if value in (None, "") else f"${float(value):.4f}"


# ── how a run reads ─────────────────────────────────────────────────────────


class Outcome:
    def __init__(self):
        self.run_id: str | None = None
        self.status: str | None = None
        self.response: str | None = None
        self.error: str | None = None
        self.pause: dict | None = None


class RunFollower:
    """Prints a run's events as it goes and remembers how it ended."""

    def __init__(self, out: Output, show_questions: bool = True):
        self.out = out
        self.names: dict[str, str] = {}
        # Off while following a run: the question is asked, or named, once, by `_drive`.
        self.show_questions = show_questions

    def line(self, event: dict) -> None:
        """One event, as a person reads it on stderr."""
        kind = event.get("type")
        note = self.out.note
        if kind == "orchestration_start":
            note(f"Running {event.get('orchestration_name') or event.get('orchestration_id')} · {event.get('run_id')}",
                 style="bold")
        elif kind == "step_start":
            self.names[event.get("orch_step_id")] = event.get("step_name") or event.get("orch_step_id")
            note(f"  ▸ {event.get('step_name')} ({event.get('step_type')})")
        elif kind == "step_complete":
            took = event.get("duration_seconds")
            note(f"  ✓ {event.get('step_name')}" + (f"  {float(took):.1f}s" if took is not None else ""),
                 style="green")
        elif kind == "step_error":
            name = self.names.get(event.get("orch_step_id"), event.get("orch_step_id"))
            note(f"  ✗ {name}: {event.get('error')}", style="red")
        elif kind == "tool_execution":
            args = json.dumps(event.get("args") or {}, default=str)
            note(f"      ↳ {event.get('tool_name')} {args[:100]}")
        elif kind == "human_input_required" and self.show_questions:
            note(f"  ? {event.get('prompt') or 'Waiting for an answer.'}", style="yellow")
        elif kind == "orchestration_error" and event.get("error") != "Cancelled":
            note(f"Failed: {event.get('error')}", style="red")

    def follow(self, frames: Iterable[dict], detach: bool = False) -> Outcome:
        outcome = Outcome()
        for frame in frames:
            event = frame["data"]
            if not isinstance(event, dict):
                continue
            if self.out.json_mode:
                self.out.event(event)
            else:
                self.line(event)
            kind = event.get("type")
            if event.get("run_id") and not outcome.run_id:
                outcome.run_id = event["run_id"]
            if kind == "orchestration_start" and detach:
                return outcome
            if kind == "human_input_required":
                outcome.pause = event
                outcome.status = "paused"
            elif kind == "orchestration_complete":
                outcome.status = event.get("status") or "completed"
            elif kind == "orchestration_error":
                outcome.error = event.get("error")
                outcome.status = "cancelled" if event.get("error") == "Cancelled" else "failed"
            elif kind == "final" and not event.get("orch_step_id"):
                outcome.response = event.get("response")
            elif kind in ("done", "stream_complete"):
                break
        return outcome


def _ask(pause: dict, out: Output):
    """Answer a paused step at the terminal: an approval, its fields, or a line of text."""
    prompt = pause.get("prompt") or "The run is waiting for an answer."
    out.err.print(f"\n[bold yellow]?[/] {escape(prompt)}")
    if pause.get("kind") == "tool_approval":
        allowed = _yes_no("Allow it?")
        reason = "" if allowed else input("Why not? (optional) ").strip()
        return {"approve": allowed, "reason": reason}
    fields = pause.get("fields") or []
    if not fields:
        return input("> ").strip()
    answer: dict = {}
    for field in fields:
        label = (field.get("label") or "").strip() or field["name"]
        kind = (field.get("type") or "string").lower()
        if kind in ("boolean", "bool"):
            answer[field["name"]] = _yes_no(label)
        elif kind in ("number", "int", "integer", "float"):
            answer[field["name"]] = _number(label)
        else:
            answer[field["name"]] = input(f"{label}: ").strip()
    return answer


def _yes_no(label: str) -> bool:
    while True:
        reply = input(f"{label} [y/N] ").strip().lower()
        if reply in ("", "n", "no"):
            return False
        if reply in ("y", "yes"):
            return True


def _number(label: str):
    while True:
        reply = input(f"{label}: ").strip()
        if reply == "":
            return None
        try:
            value = float(reply)
        except ValueError:
            continue
        return int(value) if value.is_integer() else value


def _drive(api: ApiClient, out: Output, frames: Iterator[dict], detach: bool = False) -> int:
    """Follow a run to its end, answering each question here when a person is present."""
    follower = RunFollower(out, show_questions=False)
    outcome = follower.follow(frames, detach=detach)
    if detach:
        if not outcome.run_id:
            raise CliError("The run did not start.")
        if out.json_mode:
            return errors.OK
        out.text(outcome.run_id)
        out.note(f"Follow it with: synapse runs logs {outcome.run_id} -f")
        return errors.OK

    while outcome.status == "paused" and outcome.pause:
        run_id = outcome.pause.get("run_id") or outcome.run_id
        if out.json_mode or not _interactive():
            if not out.json_mode:
                out.note(f"  ? {outcome.pause.get('prompt') or 'Waiting for an answer.'}", style="yellow")
            out.note(f"Waiting for an answer. Give it with: synapse runs answer {run_id} …", style="yellow")
            return errors.WAITING
        answer = _ask(outcome.pause, out)
        frames = api.stream("POST", f"/orchestrations/runs/{run_id}/resume/stream", {"response": answer})
        outcome = follower.follow(frames)
        outcome.run_id = outcome.run_id or run_id

    if outcome.status in ("failed", "cancelled"):
        if not out.json_mode:
            out.note(f"Run {outcome.status}.", style="red")
        return errors.RUN_FAILED
    if not out.json_mode:
        if outcome.response:
            out.markdown(outcome.response)
        else:
            out.note(f"Run {outcome.status or 'ended'}.")
    return errors.OK


def _inputs(args) -> dict:
    state: dict = {}
    if args.input_file:
        raw = sys.stdin.read() if args.input_file == "-" else open(args.input_file).read()
        try:
            state = json.loads(raw)
        except ValueError as exc:
            raise CliError(f"--input-file is not JSON: {exc}")
        if not isinstance(state, dict):
            raise CliError("--input-file must hold a JSON object.")
    for pair in args.input or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CliError(f"-i takes key=value, not '{pair}'.")
        try:
            state[key] = json.loads(value)
        except ValueError:
            state[key] = value
    return state


# ── agents and orchestrations ───────────────────────────────────────────────


@_handler
def agents_list(args, api: ApiClient, out: Output):
    agents = api.get("/agents")
    if out.json_mode:
        return out.document(agents)
    out.table(agents, [("ID", lambda a: a["id"]), ("NAME", lambda a: a["name"]),
                       ("TYPE", lambda a: a.get("type")), ("MODEL", lambda a: a.get("model"))],
              empty="No agents yet.")


@_handler
def agents_show(args, api: ApiClient, out: Output):
    agent = api.get(f"/agents/{_pick(api.get('/agents'), args.agent, 'agent')['id']}")
    if out.json_mode:
        return out.document(agent)
    out.fields([("Name", agent["name"]), ("ID", agent["id"]), ("Type", agent.get("type")),
                ("Model", agent.get("model")), ("Description", agent.get("description")),
                ("Capabilities", ", ".join(agent.get("capabilities") or []))])


@_handler
def orchestrations_list(args, api: ApiClient, out: Output):
    orchs = api.get("/orchestrations")
    if out.json_mode:
        return out.document(orchs)
    out.table(orchs, [("ID", lambda o: o["id"]), ("NAME", lambda o: o.get("name")),
                      ("STEPS", lambda o: o.get("steps")), ("DESCRIPTION", lambda o: o.get("description"))],
              empty="No orchestrations yet.")


@_handler
def orchestrations_show(args, api: ApiClient, out: Output):
    orch = api.get(f"/orchestrations/{_pick(api.get('/orchestrations'), args.orchestration, 'orchestration')['id']}")
    if out.json_mode:
        return out.document(orch)
    out.fields([("Name", orch.get("name")), ("ID", orch.get("id")), ("Description", orch.get("description")),
                ("Starts at", orch.get("entry_step_id"))])
    out.text("")
    out.table(orch.get("steps") or [], [("STEP", lambda s: s["id"]), ("NAME", lambda s: s.get("name")),
                                        ("TYPE", lambda s: s.get("type")), ("NEXT", lambda s: s.get("next_step_id"))],
              empty="It has no steps.")


# ── runs ────────────────────────────────────────────────────────────────────


@_handler
def run_orchestration(args, api: ApiClient, out: Output):
    orch = _pick(api.get("/orchestrations"), args.orchestration, "orchestration")
    body = {"message": args.message or ""}
    state = _inputs(args)
    if state:
        body["initial_state"] = state
    frames = api.stream("POST", f"/orchestrations/{orch['id']}/run/stream", body)
    return _drive(api, out, frames, detach=args.detach)


@_handler
def runs_list(args, api: ApiClient, out: Output):
    orch_id = None
    orchs: list[dict] = []
    if args.orchestration or not args.json:
        orchs = api.get("/orchestrations")
    if args.orchestration:
        orch_id = _pick(orchs, args.orchestration, "orchestration")["id"]
    runs = api.get("/orchestrations/runs", limit=args.limit, status=args.status, orchestration_id=orch_id)
    if out.json_mode:
        return out.document(runs)
    names = {o["id"]: o.get("name") for o in orchs}
    out.table(runs, [
        ("RUN", lambda r: r["run_id"]),
        ("ORCHESTRATION", lambda r: names.get(r.get("orchestration_id"), r.get("orchestration_id"))),
        ("STATUS", lambda r: "waiting" if r.get("waiting_for_human") else r.get("status")),
        ("STARTED", lambda r: _when(r.get("started_at"))),
        ("STEPS", lambda r: r.get("steps_completed")),
        ("COST", lambda r: _cost(r.get("total_cost_usd"))),
    ], empty="No runs yet.")


@_handler
def runs_show(args, api: ApiClient, out: Output):
    run = api.get(f"/orchestrations/runs/{args.run_id}")
    if out.json_mode:
        return out.document(run)
    out.fields([("Run", run["run_id"]), ("Status", run["status"])])
    question = run.get("human_input_required")
    if question:
        out.text("")
        out.fields([("Question", question.get("prompt")),
                    ("Fields", ", ".join(f"{f['name']} ({f.get('type') or 'string'})"
                                         for f in question.get("fields") or []))])
        out.note(f"Answer it with: synapse runs answer {run['run_id']} …")
    elif run.get("response"):
        out.text("")
        out.markdown(run["response"])
    return errors.WAITING if question else errors.OK


def _final_answer(out: Output, event: dict) -> None:
    if event.get("type") == "final" and not event.get("orch_step_id") and event.get("response"):
        out.markdown(event["response"])


@_handler
def runs_logs(args, api: ApiClient, out: Output):
    follower = RunFollower(out)
    if args.follow:
        for frame in api.stream("GET", f"/orchestrations/runs/{args.run_id}/events/stream"):
            event = frame["data"]
            if out.json_mode:
                out.event({"id": frame["id"], "event": event})
            elif isinstance(event, dict):
                follower.line(event)
                _final_answer(out, event)
            if isinstance(event, dict) and event.get("type") == "stream_complete":
                break
        return errors.OK

    after = 0
    while True:
        page = api.get(f"/orchestrations/runs/{args.run_id}/events", after=after)
        for entry in page["events"]:
            if out.json_mode:
                out.event(entry)
            else:
                follower.line(entry["event"])
                _final_answer(out, entry["event"])
        if not page["events"] or page["last_id"] == after:
            break
        after = page["last_id"]
    return errors.OK


@_handler
def runs_answer(args, api: ApiClient, out: Output):
    if args.approve or args.reject:
        response: object = {"approve": bool(args.approve), "reason": args.reason or ""}
    elif args.field:
        response = {}
        for pair in args.field:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                raise CliError(f"--field takes name=value, not '{pair}'.")
            try:
                response[key] = json.loads(value)
            except ValueError:
                response[key] = value
    elif args.text is not None:
        response = args.text
    else:
        run = api.get(f"/orchestrations/runs/{args.run_id}")
        question = run.get("human_input_required")
        if not question:
            raise CliError(f"Run {args.run_id} is {run['status']} and is not waiting for an answer.")
        if not _interactive():
            raise CliError("Give the answer: TEXT, --field name=value, --approve or --reject.",
                           errors.USAGE)
        response = _ask({**question, "run_id": args.run_id}, out)
    frames = api.stream("POST", f"/orchestrations/runs/{args.run_id}/resume/stream", {"response": response})
    return _drive(api, out, frames)


@_handler
def runs_cancel(args, api: ApiClient, out: Output):
    result = api.post(f"/orchestrations/runs/{args.run_id}/cancel")
    if out.json_mode:
        return out.document(result)
    out.text("Stopped." if result.get("stopped") else "It had already ended; nothing to stop.")


# ── schedules ───────────────────────────────────────────────────────────────

_EVERY = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.I)


def _every(text: str) -> tuple[int, str]:
    match = _EVERY.match(text)
    if not match:
        raise CliError(f"--every takes a number and a unit, such as 30m, 2h or 1d; not '{text}'.")
    unit = match.group(2).lower()[0]
    return int(match.group(1)), {"m": "minutes", "h": "hours", "d": "days"}[unit]


def _schedule_when(s: dict) -> str:
    if s.get("schedule_type") == "cron":
        return f"cron {s.get('cron_expression')}"
    return f"every {s.get('interval_value')} {s.get('interval_unit')}"


def _schedule_names(api: ApiClient) -> dict:
    return {**{a["id"]: a["name"] for a in api.get("/agents")},
            **{o["id"]: o.get("name") for o in api.get("/orchestrations")}}


@_handler
def schedules_list(args, api: ApiClient, out: Output):
    schedules = api.get("/schedules")
    if out.json_mode:
        return out.document(schedules)
    names = _schedule_names(api) if schedules else {}
    out.table(schedules, [
        ("ID", lambda s: s["id"]), ("NAME", lambda s: s.get("name")),
        ("RUNS", lambda s: f"{s.get('target_type')} {names.get(s.get('target_id'), s.get('target_id'))}"),
        ("WHEN", _schedule_when),
        ("STATE", lambda s: "on" if s.get("enabled") else "paused"),
        ("NEXT RUN", lambda s: _when(s.get("next_run_at")) if s.get("enabled") else ""),
    ], empty="No schedules yet.")


def _show_schedule(out: Output, s: dict, names: dict | None = None) -> None:
    names = names or {}
    out.fields([("Name", s.get("name")), ("ID", s.get("id")),
                ("Runs", f"{s.get('target_type')} {names.get(s.get('target_id'), s.get('target_id'))}"),
                ("When", _schedule_when(s)), ("State", "on" if s.get("enabled") else "paused"),
                ("Next run", _when(s.get("next_run_at")) if s.get("enabled") else None),
                ("Last run", _when(s.get("last_run_at"))), ("Prompt", s.get("prompt"))])


@_handler
def schedules_show(args, api: ApiClient, out: Output):
    s = api.get(f"/schedules/{args.schedule_id}")
    if out.json_mode:
        return out.document(s)
    _show_schedule(out, s, _schedule_names(api))


@_handler
def schedules_create(args, api: ApiClient, out: Output):
    if args.orchestration:
        target = _pick(api.get("/orchestrations"), args.orchestration, "orchestration")
        target_type = "orchestration"
    else:
        target = _pick(api.get("/agents"), args.agent, "agent")
        target_type = "agent"
    body = {
        "name": args.name or f"{target.get('name') or target['id']} schedule",
        "target_type": target_type,
        "target_id": target["id"],
        "prompt": args.prompt or "",
        "enabled": not args.paused,
    }
    if args.cron:
        body.update(schedule_type="cron", cron_expression=args.cron)
    else:
        value, unit = _every(args.every)
        body.update(schedule_type="interval", interval_value=value, interval_unit=unit)
    made = api.post("/schedules", body)
    if out.json_mode:
        return out.document(made)
    _show_schedule(out, made, {target["id"]: target.get("name")})


def _set_enabled(enabled: bool):
    @_handler
    def command(args, api: ApiClient, out: Output):
        s = api.patch(f"/schedules/{args.schedule_id}", {"enabled": enabled})
        if out.json_mode:
            return out.document(s)
        out.text(f"{s.get('name')}: " + (f"on, next run {_when(s.get('next_run_at'))}" if enabled else "paused"))
    return command


@_handler
def schedules_trigger(args, api: ApiClient, out: Output):
    result = api.post(f"/schedules/{args.schedule_id}/run")
    if out.json_mode:
        return out.document(result)
    out.text(result.get("run_id") or "Started.")


@_handler
def schedules_delete(args, api: ApiClient, out: Output):
    if not args.yes:
        if not _interactive():
            raise CliError("Deleting a schedule asks first. Pass --yes to skip the question.", errors.USAGE)
        s = api.get(f"/schedules/{args.schedule_id}")
        if not _yes_no(f"Delete the schedule '{s.get('name')}'?"):
            out.note("Kept.")
            return errors.OK
    result = api.delete(f"/schedules/{args.schedule_id}")
    if out.json_mode:
        return out.document(result)
    out.text("Deleted.")


# ── login, chat, mcp ────────────────────────────────────────────────────────


@_handler
def login(args, api: ApiClient, out: Output):
    import httpx
    import os

    from synapse.client import target as _target

    t: Target = api.target
    if not t.is_local or not t.managed:
        raise CliError("`synapse login` is for this machine's Synapse. A remote one takes an API key.")
    settings = _target._login_settings()
    if settings is None:
        out.text("Login is off for this Synapse; the CLI needs nothing more.")
        return errors.OK
    # No default: the browser's login page does not say who the user is either.
    username = args.username or input("Username: ").strip()
    password = getpass.getpass("Password: ")
    headers = {}
    if os.getenv("SYNAPSE_INTERNAL_TOKEN"):
        headers["X-Synapse-Internal"] = os.environ["SYNAPSE_INTERNAL_TOKEN"]
    try:
        resp = httpx.post(f"{t.origin}/api/auth/login", json={"username": username, "password": password},
                          headers=headers, timeout=30.0)
    except httpx.TransportError as exc:
        raise api._unreachable(exc) from None
    if resp.status_code == 401:
        raise CliError("Wrong username or password.")
    if resp.status_code >= 400:
        raise CliError(f"Login failed (HTTP {resp.status_code}).")
    t.save_session(resp.json().get("token"))
    out.text(f"Logged in as {username}. It lasts 7 days, as it does in the browser.")


@_handler
def logout(args, api: ApiClient, out: Output):
    api.target.save_session(None)
    out.text("Logged out.")


@_handler
def chat(args, api: ApiClient, out: Output):
    from synapse.client.chat import run_chat
    return run_chat(args, api, out)


@_handler
def mcp(args, api: ApiClient, out: Output):
    """Serve this Synapse to an MCP client over stdio. stdout is the protocol's."""
    if api.target.managed:
        api.target.ensure_key()
    try:
        from synapse.mcp_bridge import run as serve
    except ImportError:
        raise CliError("The MCP bridge is not in this build yet.")
    return serve(api.target.origin, api.target.api_key)


# ── the tree ────────────────────────────────────────────────────────────────


def register(sub) -> None:
    """Add the API commands to `synapse`'s subparsers."""
    flags = _client_flags()

    def add(parent, name, func, help, **kw):
        p = parent.add_parser(name, parents=[flags], help=help, description=help, **kw)
        p.set_defaults(func=func)
        return p

    def group(name, help, **kw):
        p = sub.add_parser(name, help=help, description=help, **kw)
        verbs = p.add_subparsers(dest="verb", metavar="COMMAND")
        verbs.required = True
        return verbs

    p = add(sub, "login", login, "Log the CLI in, when this machine's Synapse has login turned on")
    p.add_argument("--username")
    add(sub, "logout", logout, "Log the CLI out")

    agents = group("agents", "List and inspect agents")
    add(agents, "list", agents_list, "List agents")
    add(agents, "show", agents_show, "Show one agent").add_argument("agent", help="id or name")

    orchs = group("orchestrations", "List and inspect orchestrations", aliases=["orch"])
    add(orchs, "list", orchestrations_list, "List orchestrations")
    add(orchs, "show", orchestrations_show, "Show one orchestration and its steps").add_argument(
        "orchestration", help="id or name")

    p = add(sub, "run", run_orchestration, "Run an orchestration and follow it to the end")
    p.add_argument("orchestration", help="id or name")
    p.add_argument("-m", "--message", help="what to work on (the run's user_input)")
    p.add_argument("-i", "--input", action="append", metavar="KEY=VALUE",
                   help="an input for the run's state; VALUE is read as JSON when it is JSON")
    p.add_argument("--input-file", metavar="FILE", help="a JSON object of inputs; - reads stdin")
    p.add_argument("--detach", action="store_true", help="print the run id once it starts, and leave it running")

    runs = group("runs", "List, read, answer and stop runs")
    p = add(runs, "list", runs_list, "Recent runs, newest first")
    p.add_argument("--status", choices=["running", "paused", "completed", "failed", "cancelled"])
    p.add_argument("--orchestration", help="id or name")
    p.add_argument("--limit", type=int, default=20)
    add(runs, "show", runs_show, "A run's state, its question or its result").add_argument("run_id")
    p = add(runs, "logs", runs_logs, "A run's events")
    p.add_argument("run_id")
    p.add_argument("-f", "--follow", action="store_true", help="keep following until the run ends")
    p = add(runs, "answer", runs_answer, "Answer the question a paused run is asking")
    p.add_argument("run_id")
    p.add_argument("text", nargs="?", help="the answer, for a question with no fields")
    p.add_argument("--field", action="append", metavar="NAME=VALUE", help="a field's answer")
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="allow the tool call it is asking about")
    decision.add_argument("--reject", action="store_true", help="refuse the tool call it is asking about")
    p.add_argument("--reason", help="why, with --reject")
    add(runs, "cancel", runs_cancel, "Stop a run").add_argument("run_id")

    scheds = group("schedules", "List, create, pause and run schedules")
    add(scheds, "list", schedules_list, "List schedules")
    add(scheds, "show", schedules_show, "Show one schedule").add_argument("schedule_id")
    p = add(scheds, "create", schedules_create, "Create a schedule")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--orchestration", help="id or name")
    what.add_argument("--agent", help="id or name")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--every", help="an interval: 30m, 2h, 1d")
    when.add_argument("--cron", help='a cron expression, such as "0 9 * * 1"')
    p.add_argument("--prompt", help="what each run is asked")
    p.add_argument("--name")
    p.add_argument("--paused", action="store_true", help="create it paused")
    add(scheds, "pause", _set_enabled(False), "Pause a schedule").add_argument("schedule_id")
    add(scheds, "resume", _set_enabled(True), "Turn a paused schedule back on").add_argument("schedule_id")
    add(scheds, "trigger", schedules_trigger, "Run a schedule now, as a test").add_argument("schedule_id")
    p = add(scheds, "delete", schedules_delete, "Delete a schedule")
    p.add_argument("schedule_id")
    p.add_argument("--yes", action="store_true", help="do not ask first")

    p = add(sub, "chat", chat, "Chat with an agent")
    p.add_argument("--agent", help="id or name (default: the first agent)")
    p.add_argument("--session", help="continue a conversation by its id")
    p.add_argument("-m", "--message", help="send one message, print the answer and leave")
    p.add_argument("--verbose", action="store_true", help="show the agent's tool calls and results")

    add(sub, "mcp", mcp, "Serve this Synapse to an MCP client (Claude, Cursor, …) over stdio")

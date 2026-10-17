"""The API commands: agents, workflows, run, runs, schedules, chat, login, mcp (D126, D294).

Every one takes `--url`, `--api-key` and `--json` (see `target.py` for how the
first two resolve) and leaves with an exit code from `errors.py`. What a key
reaches over v1 is called there; what it reaches only through the product
tools (building, deletes, Boards, what waits, activity) goes through the MCP
door, `mcp.py` — the same rules, one registry (D294).
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from collections.abc import Iterable, Iterator
from urllib.parse import quote

from rich.markup import escape
from rich.text import Text

from synapse.client import errors
from synapse.client.errors import ApiError, CliError, StreamBroken
from synapse.client.http import ApiClient
from synapse.client.mcp import Mcp, needs_confirmation
from synapse.client.output import Output
from synapse.client.target import Target, resolve


# ── plumbing ────────────────────────────────────────────────────────────────


def _client_flags() -> argparse.ArgumentParser:
    flags = argparse.ArgumentParser(add_help=False)
    group = flags.add_argument_group("target")
    group.add_argument("--url", help="Synapse origin (default: SYNAPSE_API_URL, else this machine's backend)")
    group.add_argument("--api-key", help="API key (default: SYNAPSE_API_KEY, else one kept by `synapse login "
                                         "--api-key-stdin`; this machine's needs none)")
    group.add_argument("--json", action="store_true", help="print JSON (one event per line for streams)")
    return flags


def _handler(fn):
    """Build the output, target and client a command needs, and turn errors into exit codes."""
    def run(args) -> int:
        out = Output(json_mode=getattr(args, "json", False))
        api = None
        try:
            # `login --api-key-stdin` brings the key it keeps; nothing is asked of the target first.
            keeping = getattr(args, "api_key_to_keep", None) is not None
            target = resolve(getattr(args, "url", None), getattr(args, "api_key", None), need_key=not keeping)
            api = ApiClient(target)
            return fn(args, api, out) or errors.OK
        except CliError as exc:
            out.error(exc.message, exc.code, exc.status)
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
        raise CliError(f"No {kind} is named or has the id '{ident}'.", errors.NOT_FOUND)
    listed = ", ".join(f"{m['id']}" for m in matches)
    raise CliError(f"{len(matches)} {kind}s are named '{ident}': {listed}. Use the id.")


#: Where each kind a person names lives on v1.
_COLLECTIONS = {"agent": "/agents", "workflow": "/orchestrations"}


def _find(api: ApiClient, kind: str, ident: str) -> dict:
    """The agent or workflow `ident` names: read by id in one call, else found by its name (F714).

    Before, every lookup listed the whole collection, an exact id included. A
    name costs the read that 404s and the list.
    """
    path = _COLLECTIONS[kind]
    if ident and ident.strip() == ident:
        try:
            return api.get(f"{path}/{quote(ident, safe='')}")
        except ApiError as exc:
            if exc.status != 404:
                raise
    # The list's entries are a summary (a workflow's `steps` is a count there): read the one named whole.
    return api.get(f"{path}/{quote(_pick(api.get(path), ident, kind)['id'], safe='')}")


def _when(started) -> str:
    """A time as a person scans a table, in this machine's time zone: `2026-10-17 14:05`.

    From epoch seconds or ISO text; an ISO time with no zone is the server's UTC. Every time the CLI prints
    reads the same way, whichever route it came from.
    """
    from datetime import datetime, timezone

    if started in (None, ""):
        return ""
    try:
        if isinstance(started, (int, float)):
            moment = datetime.fromtimestamp(started, timezone.utc)
        else:
            moment = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return str(started).replace("T", " ")[:16]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _cost(value) -> str:
    return "" if value in (None, "") else f"${float(value):.4f}"


def _quiet_flag(p) -> None:
    p.add_argument("-q", "--quiet", action="store_true", help="print only the ids, one a line")


def _pairs(pairs: Iterable[str] | None, flag: str) -> dict:
    """`name=value` arguments as a dict; each value read as JSON when it is JSON."""
    values: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CliError(f"{flag} takes name=value, not '{pair}'.", errors.USAGE)
        try:
            values[key] = json.loads(value)
        except ValueError:
            values[key] = value
    return values


def _json_file(path: str, flag: str):
    """A JSON file's contents; `-` reads stdin."""
    try:
        raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    except OSError as exc:
        raise CliError(f"{flag}: {exc.strerror}: {path}", errors.USAGE)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CliError(f"{flag} is not JSON: {exc}", errors.USAGE)


def show_value(out: Output, value) -> None:
    """A tool's answer for a person: text as Markdown, anything else as indented lines (JSON with --json)."""
    if out.json_mode:
        return out.document(value)
    if isinstance(value, str):
        return out.markdown(value)
    for line in _lines(value):
        out.text(line)


def _lines(value, indent: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            if isinstance(item, (dict, list)):
                yield f"{indent}{key}:"
                yield from _lines(item, indent + "  ")
            else:
                yield f"{indent}{key}: {item}"
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                inner = list(_lines(item, indent + "  "))
                if inner:
                    yield f"{indent}- {inner[0].strip()}"
                    yield from inner[1:]
            else:
                yield f"{indent}- {item}"
    else:
        yield f"{indent}{value}"


def confirmed_call(mcp: Mcp, out: Output, name: str, arguments: dict, yes: bool, what: str):
    """An act the registry confirms (a delete): its preview, then the act once a person, or `--yes`, agrees.

    None when the person said no. A script with no `--yes` is refused before
    anything is done, as `schedules delete` always was.
    """
    value = mcp.call(name, arguments)
    if not needs_confirmation(value):
        return value
    if not yes:
        if not _interactive():
            raise CliError(f"{what} asks first. Pass --yes to skip the question.", errors.USAGE)
        preview = value.get("preview")
        if preview:
            for line in _lines(preview):
                out.note(line, style="")
        if not _yes_no(f"{what}?"):
            out.note("Kept.")
            return None
    return mcp.call(name, {**arguments, "confirm_token": value["confirm_token"]})


# ── how a run reads ─────────────────────────────────────────────────────────


class Outcome:
    def __init__(self):
        self.run_id: str | None = None
        self.status: str | None = None
        self.response: str | None = None
        self.error: str | None = None
        self.pause: dict | None = None
        #: The run kept going after a team member's answer was taken (`answered`): read on, nothing to ask.
        self.answered_live = False


#: Engine bookkeeping that v1's run stream holds back and its events stream does not.
def _held_back(event: dict) -> bool:
    return str(event.get("type") or "").startswith("_log_")


class RunFollower:
    """Prints a run's events as it goes and remembers how it ended."""

    def __init__(self, out: Output, show_questions: bool = True):
        self.out = out
        self.names: dict[str, str] = {}
        # Off while following a run: the question is asked, or named, once, by `_drive`.
        self.show_questions = show_questions
        #: How many of the run's journaled events this process has shown, so a reattach skips them (F716).
        self.seen = 0
        #: Events to pass over on a reattached stream: the ones already shown.
        self.skip = 0
        #: The last frame's id, when the stream names them (cloud's do): a reattach reads on from it.
        self.last_id: str | None = None
        #: A person at a terminal sees the run drawn: a spinner on the step at work, one line per step done, a
        #: summary at the end. Piped, every event is its own plain line, as scripts and logs read it.
        self.tty = not out.json_mode and out.err.is_terminal
        self._spinner = None
        self._types: dict[str, str] = {}
        self._steps = 0
        self._seconds = 0.0
        #: Why the run failed, from its last step error, for the summary.
        self._why: str | None = None

    # ── at a terminal ───────────────────────────────────────────────────────

    def _spin(self, text: str) -> None:
        from synapse.client import look

        if self._spinner is None:
            self._spinner = look.working(self.out.err, text)
        else:
            self._spinner.update(text)

    def stop(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _say(self, text: str) -> None:
        self.out.err.print(text, highlight=False, soft_wrap=True)

    def _drawn(self, event: dict) -> bool:
        """Draw what a terminal shows differently from a pipe; False for the lines both show alike."""
        kind = event.get("type")
        step = event.get("orch_step_id")
        from synapse.client import look

        if kind == "orchestration_start":
            name = event.get("orchestration_name") or event.get("orchestration_id")
            self._say(f"[bold {look.ACCENT}]{look.MARK}[/] [bold]{escape(str(name))}[/bold]  "
                      f"[dim]{escape(str(event.get('run_id') or ''))}[/dim]")
        elif kind == "tool_execution":
            self.out.err.print(Text("    ").append_text(look.call_line(str(event.get("tool_name")),
                                                                       event.get("args"))))
        elif kind == "step_start":
            self.names[step] = event.get("step_name") or step
            self._types[step] = event.get("step_type") or ""
            self._spin(f"{event.get('step_name') or step}…")
        elif kind == "step_complete":
            self.stop()
            took = event.get("duration_seconds")
            self._steps += 1
            self._seconds += float(took or 0)
            name = str(event.get("step_name") or self.names.get(step) or step)
            kind_of = self._types.get(step) or event.get("step_type") or ""
            self._say(f"  [green]✓[/green] {escape(name):<30} [dim]{escape(kind_of):<10}"
                      + (f" {float(took):6.1f}s" if took is not None else "") + "[/dim]")
        elif kind == "step_error":
            self.stop()
            self._why = event.get("error") or self._why
            name = str(self.names.get(step, step))
            self._say(f"  [red]✗ {escape(name)}[/red]  {escape(str(event.get('error') or ''))}")
        elif kind == "human_input_required":
            self.stop()
            if self.show_questions:
                self._say(f"  [yellow]? {escape(str(event.get('prompt') or 'Waiting for an answer.'))}[/yellow]")
        elif kind == "orchestration_complete":
            self.stop()
            steps = f"{self._steps} step{'s' if self._steps != 1 else ''}"
            status = event.get("status") or "completed"
            if status == "completed":
                self._say(f"[green]{look.DOT}[/green] Completed [dim]· {steps} · {self._seconds:.1f}s[/dim]\n")
            elif status == "cancelled":
                self._say(f"[dim]{look.DOT} Stopped.[/dim]")
            else:  # the engine ends a failed run here too, with its status
                why = self._why or status
                self._say(f"[red]{look.DOT} Failed[/red] [dim]after {steps} ·[/dim] {escape(str(why))}")
        elif kind == "orchestration_error":
            self.stop()
            if event.get("error") == "Cancelled":
                self._say(f"[dim]{look.DOT} Stopped.[/dim]")
            else:
                steps = f"{self._steps} step{'s' if self._steps != 1 else ''}"
                self._say(f"[red]{look.DOT} Failed[/red] [dim]after {steps} ·[/dim] "
                          f"{escape(str(event.get('error')))}")
        else:
            return False
        return True

    def line(self, event: dict) -> None:
        """One event, as a person reads it on stderr."""
        if self.tty and self._drawn(event):
            return
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
        elif kind == "step_warning" and event.get("message"):
            note(f"  ! {event['message']}", style="yellow")
        elif kind == "tool_execution":
            args = json.dumps(event.get("args") or {}, default=str)
            note(f"      ↳ {event.get('tool_name')} {args[:100]}")
        elif kind in ("routing_decision", "evaluator_decision", "switch_decision"):
            went = (event.get("decision") or event.get("matched_case")
                    or str(event.get("tool_name") or "").removeprefix("route_"))
            if went:
                note(f"    → {went}")
        elif kind == "parallel_start":
            note(f"    ⇉ {event.get('branch_count')} branches at once")
        elif kind == "loop_iteration" and event.get("iteration") is not None:
            total = event.get("total")
            note(f"    ↻ {event['iteration']}" + (f" of {total}" if total else ""))
        # A team's members and an agent's helpers: one line each as they start and end (F713, TEAM1b).
        elif kind in ("member_started", "helper_started"):
            who = event.get("member_name") or event.get("helper_name")
            task = str(event.get("task") or "").strip().splitlines()
            note(f"    · {who}" + (f": {task[0][:80]}" if task else "") + (" (resumed)" if event.get("resumed") else ""))
        elif kind in ("member_finished", "helper_finished"):
            who = event.get("member_name") or event.get("helper_name")
            status = event.get("status") or "done"
            took = event.get("seconds")
            style = "green" if status in ("completed", "done") else "red" if status == "failed" else "dim"
            note(f"    {'✓' if style == 'green' else '✗' if style == 'red' else '·'} {who} {status}"
                 + (f"  {float(took):.1f}s" if took is not None else ""), style=style)
        elif kind == "member_answered":
            note(f"    · {event.get('member_name')}: {'allowed' if event.get('approved') else 'refused'}")
        elif kind == "human_input_required" and self.show_questions:
            note(f"  ? {event.get('prompt') or 'Waiting for an answer.'}", style="yellow")
        elif kind == "orchestration_error" and event.get("error") != "Cancelled":
            note(f"Failed: {event.get('error')}", style="red")

    def follow(self, frames: Iterable[dict], detach: bool = False, journal: bool = False) -> Outcome:
        """Show a run's frames and remember how it ended.

        `journal`: the frames are the run's events stream, which carries what
        v1's run stream holds back; those are passed over, so a script reads one
        shape either way.
        """
        try:
            return self._follow(frames, detach, journal)
        finally:
            self.stop()

    def _follow(self, frames: Iterable[dict], detach: bool, journal: bool) -> Outcome:
        outcome = Outcome()
        for frame in frames:
            event = frame["data"]
            if frame.get("id"):
                self.last_id = str(frame["id"])
            if not isinstance(event, dict):
                continue
            if frame.get("id") is None and event.get("status") == "answered":
                # A working team took the answer at once; the run carries on (TEAM1c).
                outcome.answered_live = True
                outcome.run_id = outcome.run_id or event.get("run_id")
                if self.out.json_mode:
                    self.out.event(event)
                break
            if journal and _held_back(event):
                continue
            kind = event.get("type")
            if journal and kind in ("run_stream_end",):
                continue
            if kind not in ("done", "stream_complete", "heartbeat"):
                if self.skip:
                    self.skip -= 1
                    continue
                self.seen += 1
            if self.out.json_mode:
                self.out.event(event)
            else:
                self.line(event)
            if event.get("run_id") and not outcome.run_id:
                outcome.run_id = event["run_id"]
            if kind == "orchestration_start" and detach:
                return outcome
            if kind == "human_input_required":
                outcome.pause = event
                outcome.status = "paused"
                if journal:
                    break
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


def questions_of(pause: dict) -> list[dict]:
    """The questions a pause asks: a team's several, each with its id (TEAM1c, TEAM1e), or its one."""
    return [q for q in pause.get("questions") or [] if q.get("question_id")]


def _question_line(q: dict) -> str:
    what = q.get("prompt") or f"{q.get('tool_name')} {json.dumps(q.get('tool_args') or {}, default=str)[:80]}"
    return f"{q.get('member_name') or 'A member'}: {what}"


def _ask(pause: dict, out: Output):
    """Answer a paused step at the terminal: an approval, its fields, or a line of text."""
    from synapse.client import look

    prompt = pause.get("prompt") or "The run is waiting for an answer."
    if out.err.is_terminal:
        out.err.print()
        look.question_box(out.err, "Waiting for you", [prompt])
    else:
        out.err.print(f"\n[bold yellow]?[/] {escape(prompt)}")
    if pause.get("kind") == "tool_approval" or pause.get("tool_name"):
        allowed = _yes_no("  Allow it?")
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
            answer[field["name"]] = _yes_no(f"  {label}")
        elif kind in ("number", "int", "integer", "float"):
            answer[field["name"]] = _number(f"  {label}")
        else:
            answer[field["name"]] = input(f"  {label}: ").strip()
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


def _reattach(api: ApiClient, follower: RunFollower, run_id: str, after: str | None = None) -> Iterator[dict]:
    """The run's events stream, read on from where this process is (F716).

    With `after` (the run's last event id before an answer was given), from
    just after it. Without, from the run's start with every event this
    process has shown passed over: `seen` counts the run's, across every cut.
    """
    after = after if after is not None else follower.last_id
    if after is not None:
        follower.skip = 0
        return api.stream("GET", f"/orchestrations/runs/{run_id}/events/stream",
                          headers={"Last-Event-ID": str(after)})
    follower.skip = follower.seen
    return api.stream("GET", f"/orchestrations/runs/{run_id}/events/stream")


def _journal_end(api: ApiClient, run_id: str) -> str | None:
    """The id of a run's last event so far, to read on after it; None when it has none."""
    after, last = 0, None
    while True:
        page = api.get(f"/orchestrations/runs/{run_id}/events", after=after, limit=5000)
        events = page.get("events") or []
        if events:
            last = page.get("last_id", events[-1].get("id"))
        if len(events) < 5000 or page.get("last_id") == after:
            return None if last is None else str(last)
        after = page["last_id"]


def _follow(api: ApiClient, out: Output, follower: RunFollower, frames: Iterator[dict], *,
            detach: bool = False, journal: bool = False, owns_run: bool = True) -> Outcome:
    """`follower.follow`, reading on from the events stream when the stream is cut (F716).

    Only a run this process has shown from its start can skip exactly what it
    has shown; another (`runs answer` in a new shell) says how to read on.
    """
    attempts = 0
    run_id = None
    while True:
        try:
            outcome = follower.follow(frames, detach=detach, journal=journal)
            outcome.run_id = outcome.run_id or run_id
            return outcome
        except StreamBroken as exc:
            run_id = run_id or getattr(follower, "run_id", None)
            if not owns_run or not run_id or attempts >= 3:
                where = f" Read on with: synapse runs logs {run_id} -f" if run_id else ""
                raise StreamBroken(f"{exc.message}{where}") from None
            attempts += 1
            out.note(f"  … the connection dropped; reading on ({attempts}).", style="yellow")
            frames = _reattach(api, follower, run_id)
            journal = True


class _Tracking(RunFollower):
    """A follower that keeps the run's id as soon as an event names it, for a reattach."""

    run_id: str | None = None

    def line(self, event: dict) -> None:
        self.run_id = self.run_id or event.get("run_id")
        super().line(event)

    def follow(self, frames, detach=False, journal=False):
        def noted():
            for frame in frames:
                data = frame.get("data")
                if isinstance(data, dict) and data.get("run_id"):
                    self.run_id = self.run_id or data["run_id"]
                yield frame
        return super().follow(noted(), detach=detach, journal=journal)


def _answer_for(question: dict, out: Output):
    if question.get("question_id"):
        from synapse.client import look

        if out.err.is_terminal:
            out.err.print()
            look.question_box(out.err, "Allow this call?", [_question_line(question)])
        else:
            out.err.print(f"\n[bold yellow]?[/] {escape(_question_line(question))}")
        allowed = _yes_no("  Allow it?")
        reason = "" if allowed else input("Why not? (optional) ").strip()
        return {"approve": allowed, "reason": reason}
    return _ask(question, out)


def _waiting_note(out: Output, run_id: str, pause: dict) -> None:
    asked = questions_of(pause)
    if not asked:
        if not out.json_mode:
            out.note(f"  ? {pause.get('prompt') or 'Waiting for an answer.'}", style="yellow")
        out.note(f"Waiting for an answer. Give it with: synapse runs answer {run_id} …", style="yellow")
        return
    out.note(f"Waiting for {len(asked)} answers. Give each with:", style="yellow")
    for q in asked:
        out.note(f"  {_question_line(q)}", style="yellow")
        out.note(f"    synapse runs answer {run_id} --question {q['question_id']} --approve|--reject",
                 style="yellow")


def _drive(api: ApiClient, out: Output, frames: Iterator[dict], detach: bool = False,
           owns_run: bool = True, run_id: str | None = None, after: str | None = None) -> int:
    """Follow a run to its end, answering each question here when a person is present."""
    follower = _Tracking(out, show_questions=False)
    follower.run_id = run_id
    outcome = _follow(api, out, follower, frames, detach=detach, owns_run=owns_run)
    if detach:
        if not outcome.run_id:
            raise CliError("The run did not start.")
        if out.json_mode:
            return errors.OK
        out.text(outcome.run_id)
        out.note(f"Follow it with: synapse runs logs {outcome.run_id} -f")
        return errors.OK

    while (outcome.status == "paused" and outcome.pause) or outcome.answered_live:
        run_id = (outcome.pause or {}).get("run_id") or outcome.run_id or follower.run_id
        if outcome.answered_live:
            # A team member took the answer; the run never stopped. Read on from its events.
            start = None if owns_run else after
            outcome = _follow(api, out, follower, _reattach(api, follower, run_id, start), journal=True)
            after = None
            outcome.run_id = outcome.run_id or run_id
            continue
        if out.json_mode or not _interactive():
            _waiting_note(out, run_id, outcome.pause)
            return errors.WAITING
        asked = questions_of(outcome.pause) or [outcome.pause]
        question = asked[0]
        body = {"response": _answer_for(question, out)}
        if question.get("question_id"):
            body["question_id"] = question["question_id"]
        frames = api.stream("POST", f"/orchestrations/runs/{run_id}/resume/stream", body)
        outcome = _follow(api, out, follower, frames, owns_run=owns_run)
        outcome.run_id = outcome.run_id or run_id

    if outcome.status in ("failed", "cancelled"):
        if not out.json_mode and not follower.tty:  # at a terminal the follower's summary said so
            out.note(f"Run {outcome.status}.", style="red")
        return errors.RUN_FAILED
    if not out.json_mode:
        if outcome.response and out.out.is_terminal:
            from synapse.client import look
            look.said(out.out, "Answer", outcome.response)
        elif outcome.response:
            out.markdown(outcome.response)
        elif not follower.tty:
            out.note(f"Run {outcome.status or 'ended'}.")
    return errors.OK


def _inputs(args) -> dict:
    state: dict = {}
    if args.input_file:
        state = _json_file(args.input_file, "--input-file")
        if not isinstance(state, dict):
            raise CliError("--input-file must hold a JSON object.", errors.USAGE)
    state.update(_pairs(args.input, "-i"))
    return state


# ── agents and workflows ────────────────────────────────────────────────────


@_handler
def agents_list(args, api: ApiClient, out: Output):
    agents = api.get("/agents")
    if args.quiet:
        return out.ids(agents)
    if out.json_mode:
        return out.document(agents)
    out.table(agents, [("ID", lambda a: a["id"]), ("NAME", lambda a: a["name"]),
                       ("TYPE", lambda a: a.get("type")), ("MODEL", lambda a: a.get("model"))],
              empty="No agents yet.")


@_handler
def agents_show(args, api: ApiClient, out: Output):
    agent = _find(api, "agent", args.agent)
    if out.json_mode:
        return out.document(agent)
    out.fields([("Name", agent["name"]), ("ID", agent["id"]), ("Type", agent.get("type")),
                ("Model", agent.get("model")), ("Description", agent.get("description")),
                ("Capabilities", ", ".join(agent.get("capabilities") or []))])


@_handler
def workflows_list(args, api: ApiClient, out: Output):
    orchs = api.get("/orchestrations")
    if args.quiet:
        return out.ids(orchs)
    if out.json_mode:
        return out.document(orchs)
    out.table(orchs, [("ID", lambda o: o["id"]), ("NAME", lambda o: o.get("name")),
                      ("STEPS", lambda o: o.get("steps")), ("DESCRIPTION", lambda o: o.get("description"))],
              empty="No workflows yet.")


@_handler
def workflows_show(args, api: ApiClient, out: Output):
    orch = _find(api, "workflow", args.workflow)
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
def run_workflow(args, api: ApiClient, out: Output):
    orch = _find(api, "workflow", args.workflow)
    body = {"message": args.message or ""}
    state = _inputs(args)
    if state:
        body["initial_state"] = state
    if args.webhook:
        return _run_with_webhook(api, out, args, orch, body)
    frames = api.stream("POST", f"/orchestrations/{orch['id']}/run/stream", body)
    return _drive(api, out, frames, detach=args.detach)


def _v2(api: ApiClient, path: str) -> str:
    base = api.target.api_base
    return (base[: -len("/v1")] + "/v2" if base.endswith("/v1") else api.target.origin + "/api/v2") + path


def _run_with_webhook(api: ApiClient, out: Output, args, orch: dict, body: dict) -> int:
    """v2's run: queued at once, and a signed POST to `--webhook` when it ends (the one thing only v2 does)."""
    body = {**body, "webhook_url": args.webhook}
    if args.webhook_secret:
        body["webhook_secret"] = args.webhook_secret
    try:
        queued = _body_of(api.send("POST", "", url=_v2(api, f"/orchestrations/{orch['id']}/run"), json=body))
    except ApiError as exc:
        if exc.status == 503 and "scale mode" in exc.detail.lower():
            raise CliError("A webhook needs Synapse's async API, which this install serves only in scale mode "
                           "(Settings → Scale). Without --webhook the run reports back here.",
                           errors.UNAVAILABLE, 503) from None
        raise
    run_id = queued["run_id"]
    if args.detach:
        if out.json_mode:
            return out.document(queued)
        out.text(run_id)
        out.note(f"Queued; {args.webhook} is told when it ends. Follow it with: synapse runs logs {run_id} -f")
        return errors.OK
    follower = _Tracking(out, show_questions=False)
    follower.run_id = run_id
    frames = api.stream("GET", f"/orchestrations/runs/{run_id}/events/stream")
    return _drive_from(api, out, follower, frames, run_id)


def _drive_from(api: ApiClient, out: Output, follower, frames, run_id: str) -> int:
    outcome = _follow(api, out, follower, frames, journal=True)
    outcome.run_id = outcome.run_id or run_id
    if outcome.status == "paused" and outcome.pause:
        _waiting_note(out, run_id, outcome.pause)
        return errors.WAITING
    if outcome.status in ("failed", "cancelled"):
        if not out.json_mode:
            out.note(f"Run {outcome.status}.", style="red")
        return errors.RUN_FAILED
    if not out.json_mode:
        out.markdown(outcome.response) if outcome.response else out.note(f"Run {outcome.status or 'ended'}.")
    return errors.OK


def _body_of(resp):
    from synapse.client.http import _body
    return _body(resp)


@_handler
def runs_list(args, api: ApiClient, out: Output):
    orch_id = None
    orchs: list[dict] = []
    if args.workflow:
        orch_id = _find(api, "workflow", args.workflow)["id"]
    runs = api.get("/orchestrations/runs", limit=args.limit, status=args.status, orchestration_id=orch_id)
    if args.quiet:
        return out.ids(runs, "run_id")
    if out.json_mode:
        return out.document(runs)
    if runs:
        orchs = api.get("/orchestrations")
    names = {o["id"]: o.get("name") for o in orchs}
    out.table(runs, [
        ("RUN", lambda r: r["run_id"]),
        ("WORKFLOW", lambda r: names.get(r.get("orchestration_id"), r.get("orchestration_id"))),
        ("STATUS", lambda r: "waiting" if r.get("waiting_for_human") else r.get("status")),
        ("STARTED", lambda r: _when(r.get("started_at"))),
        ("STEPS", lambda r: r.get("steps_completed")),
        ("COST", lambda r: _cost(r.get("total_cost_usd"))),
    ], empty="No runs yet.")


@_handler
def runs_show(args, api: ApiClient, out: Output):
    run = api.get(f"/orchestrations/runs/{args.run_id}")
    if out.json_mode:
        out.document(run)
        return errors.WAITING if run.get("human_input_required") else errors.OK
    out.fields([("Run", run["run_id"]), ("Status", run["status"]), ("Error", run.get("error"))])
    question = run.get("human_input_required")
    if question:
        asked = questions_of(question)
        out.text("")
        if asked:
            for q in asked:
                out.fields([("Question", _question_line(q)), ("Its id", q["question_id"])])
            out.note(f"Answer each with: synapse runs answer {run['run_id']} --question ID --approve|--reject")
        else:
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
        last = None
        attempts = 0
        while True:
            try:
                headers = {"Last-Event-ID": last} if last else None
                for frame in api.stream("GET", f"/orchestrations/runs/{args.run_id}/events/stream", headers=headers):
                    event = frame["data"]
                    if frame.get("id"):
                        last = frame["id"]
                    if out.json_mode:
                        out.event({"id": frame["id"], "event": event})
                    elif isinstance(event, dict):
                        follower.line(event)
                        _final_answer(out, event)
                    if isinstance(event, dict) and event.get("type") == "stream_complete":
                        return errors.OK
                return errors.OK
            except StreamBroken:
                # Each frame names its id, so the stream picks up after the last one shown (F716).
                attempts += 1
                if attempts > 3:
                    raise
                out.note(f"  … the connection dropped; reading on ({attempts}).", style="yellow")

    # `last_id` is a cursor, not always a number: a run a scale-mode worker ran is
    # read from its Redis stream (ids like `1790-0`), all of it on one page. So a
    # short page is the end, and the cursor is only ever handed back as it came.
    after, page_size = 0, 5000
    while True:
        page = api.get(f"/orchestrations/runs/{args.run_id}/events", after=after, limit=page_size)
        for entry in page["events"]:
            if out.json_mode:
                out.event(entry)
            else:
                follower.line(entry["event"])
                _final_answer(out, entry["event"])
        if len(page["events"]) < page_size or page["last_id"] == after:
            break
        after = page["last_id"]
    return errors.OK


@_handler
def runs_answer(args, api: ApiClient, out: Output):
    question_id = args.question
    if args.approve or args.reject:
        response: object = {"approve": bool(args.approve), "reason": args.reason or ""}
    elif args.field:
        response = _pairs(args.field, "--field")
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
        asked = questions_of(question)
        if asked:
            chosen = next((q for q in asked if q["question_id"] == question_id), None) if question_id else asked[0]
            if chosen is None:
                raise CliError(f"Run {args.run_id} is not asking the question {question_id}.", errors.NOT_FOUND)
            question_id = chosen["question_id"]
            response = _answer_for(chosen, out)
        else:
            response = _ask({**question, "run_id": args.run_id}, out)
    body = {"response": response}
    if question_id:
        body["question_id"] = question_id
    # Where the run's events end now: a team that takes the answer while it works never pauses, and the CLI
    # reads on from just after this, showing nothing from before the answer again.
    after = _journal_end(api, args.run_id)
    frames = api.stream("POST", f"/orchestrations/runs/{args.run_id}/resume/stream", body)
    return _drive(api, out, frames, owns_run=False, run_id=args.run_id, after=after)


@_handler
def runs_cancel(args, api: ApiClient, out: Output):
    result = api.post(f"/orchestrations/runs/{args.run_id}/cancel")
    if out.json_mode:
        return out.document(result)
    out.text("Stopped." if result.get("stopped") else "It had already ended; nothing to stop.")


@_handler
def runs_waiting(args, api: ApiClient, out: Output):
    """What waits on a person: runs' questions and tool calls agents stopped on (MCP `list_waiting`)."""
    mcp = Mcp(api)
    items: list[dict] = []
    cursor = None
    while True:
        page = mcp.call("list_waiting", {"kind": args.kind, "cursor": cursor, "limit": 50})
        items += page.get("items") or []
        cursor = page.get("next_cursor")
        if not cursor or (args.limit and len(items) >= args.limit):
            break
    items = items[: args.limit] if args.limit else items
    if out.json_mode:
        return out.document(items)

    def answer(i: dict) -> str:
        if i.get("kind") == "run_question":
            q = f" --question {i['question_id']}" if i.get("question_id") else ""
            return f"synapse runs answer {i['run_id']}{q} …"
        if i.get("schedule_id"):
            return f"synapse schedules approve {i['schedule_id']} --approve|--reject"
        q = f" --question {i['question_id']}" if i.get("question_id") else ""
        return f"synapse chat --session {i.get('session_id')}{q} --approve|--reject"

    if not items:
        out.note("Nothing waits on a person.")
        return errors.OK
    # A list, not a table: each command is copied whole, and a table wraps the ids inside it.
    for item in items:
        detail = item.get("detail") or (f"{item['member_name']}: {item.get('tool_name')}"
                                        if item.get("member_name") else None)
        out.out.print(f"[yellow]?[/yellow] [bold]{escape(str(item.get('title') or 'A question'))}[/bold]"
                      f"  [dim]{_when(item.get('asked_at'))}[/dim]", markup=True, highlight=False)
        if detail:
            out.out.print(f"  {escape(str(detail))}", markup=True, highlight=False)
        out.text(f"  {answer(item)}")


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


def _kind(s: dict) -> str:
    """What a schedule runs, in the product's words: an orchestration is a workflow."""
    return "workflow" if s.get("target_type") == "orchestration" else str(s.get("target_type") or "")


def _schedule_names(api: ApiClient) -> dict:
    return {**{a["id"]: a["name"] for a in api.get("/agents")},
            **{o["id"]: o.get("name") for o in api.get("/orchestrations")}}


@_handler
def schedules_list(args, api: ApiClient, out: Output):
    schedules = api.get("/schedules")
    if args.quiet:
        return out.ids(schedules)
    if out.json_mode:
        return out.document(schedules)
    names = _schedule_names(api) if schedules else {}
    out.table(schedules, [
        ("ID", lambda s: s["id"]), ("NAME", lambda s: s.get("name")),
        ("RUNS", lambda s: f"{_kind(s)} {names.get(s.get('target_id'), s.get('target_id'))}"),
        ("WHEN", _schedule_when),
        ("STATE", lambda s: "on" if s.get("enabled") else "paused"),
        ("NEXT RUN", lambda s: _when(s.get("next_run_at")) if s.get("enabled") else ""),
    ], empty="No schedules yet.")


def _show_schedule(out: Output, s: dict, names: dict | None = None) -> None:
    names = names or {}
    out.fields([("Name", s.get("name")), ("ID", s.get("id")),
                ("Runs", f"{_kind(s)} {names.get(s.get('target_id'), s.get('target_id'))}"),
                ("When", _schedule_when(s)), ("State", "on" if s.get("enabled") else "paused"),
                ("Next run", _when(s.get("next_run_at")) if s.get("enabled") else None),
                ("Last run", _when(s.get("last_run_at"))), ("If missed", s.get("missed_run_policy")),
                ("Prompt", s.get("prompt"))])


@_handler
def schedules_show(args, api: ApiClient, out: Output):
    s = api.get(f"/schedules/{args.schedule_id}")
    if out.json_mode:
        return out.document(s)
    _show_schedule(out, s, _schedule_names(api))


def _timing(args) -> dict:
    if args.cron:
        return {"schedule_type": "cron", "cron_expression": args.cron}
    if args.every:
        value, unit = _every(args.every)
        return {"schedule_type": "interval", "interval_value": value, "interval_unit": unit}
    return {}


@_handler
def schedules_create(args, api: ApiClient, out: Output):
    if args.workflow:
        target = _find(api, "workflow", args.workflow)
        target_type = "orchestration"
    else:
        target = _find(api, "agent", args.agent)
        target_type = "agent"
    body = {
        "name": args.name or f"{target.get('name') or target['id']} schedule",
        "target_type": target_type,
        "target_id": target["id"],
        "prompt": args.prompt or "",
        "enabled": not args.paused,
        **_timing(args),
    }
    if args.missed:
        body["missed_run_policy"] = args.missed
    made = api.post("/schedules", body)
    if out.json_mode:
        return out.document(made)
    _show_schedule(out, made, {target["id"]: target.get("name")})


@_handler
def schedules_update(args, api: ApiClient, out: Output):
    """Change a schedule: its timing, prompt, name or missed-run policy (F717)."""
    body = {**_timing(args)}
    for key, value in (("name", args.name), ("prompt", args.prompt), ("description", args.description),
                       ("missed_run_policy", args.missed)):
        if value is not None:
            body[key] = value
    if not body:
        raise CliError("Say what to change: --every, --cron, --prompt, --name, --description or --missed.",
                       errors.USAGE)
    s = api.patch(f"/schedules/{args.schedule_id}", body)
    if out.json_mode:
        return out.document(s)
    _show_schedule(out, s, _schedule_names(api))


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


def answer_tool_call(api: ApiClient, out: Output, args, where: dict) -> int:
    """Allow or refuse a tool call an agent stopped on, through MCP's `answer_tool_approval`."""
    if not (args.approve or args.reject):
        raise CliError("Say --approve or --reject.", errors.USAGE)
    result = Mcp(api).call("answer_tool_approval", {
        **where, "approve": bool(args.approve), "reason": args.reason,
        "question_id": getattr(args, "question", None), "agent": getattr(args, "agent", None),
        "wait_seconds": getattr(args, "wait", None),
    })
    if out.json_mode:
        out.document(result)
    elif isinstance(result, dict) and (result.get("response") or result.get("answer")):
        out.markdown(result.get("response") or result.get("answer"))
    else:
        out.text("Allowed." if args.approve else "Refused.")
    return errors.WAITING if isinstance(result, dict) and result.get("status") in ("awaiting_approval", "paused") \
        else errors.OK


@_handler
def schedules_approve(args, api: ApiClient, out: Output):
    """A scheduled run's held tool call, allowed or refused."""
    return answer_tool_call(api, out, args, {"schedule_id": args.schedule_id})


# ── login, chat, mcp, target ────────────────────────────────────────────────


@_handler
def login(args, api: ApiClient, out: Output):
    import httpx
    import os

    from synapse.client import target as _target

    t: Target = api.target
    if args.api_key_to_keep is not None:
        return _keep_key(api, out, args.api_key_to_keep)
    if not t.is_local or not t.managed:
        raise CliError("`synapse login` is for this machine's Synapse. For another, keep its key: "
                       "synapse login --url ORIGIN --api-key-stdin", errors.USAGE)
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
        raise CliError("Wrong username or password.", errors.REFUSED, 401)
    if resp.status_code >= 400:
        raise CliError(f"Login failed (HTTP {resp.status_code}).", errors.code_for(resp.status_code),
                       resp.status_code)
    t.save_session(resp.json().get("token"))
    out.text(f"Logged in as {username}. It lasts 7 days, as it does in the browser.")


def _keep_key(api: ApiClient, out: Output, key: str) -> int:
    """Keep a remote Synapse's key in cli.json (0600), checked first, so commands need no SYNAPSE_API_KEY."""
    key = (key or "").strip() or sys.stdin.readline().strip()
    if not key:
        raise CliError("Give the key on stdin: echo $KEY | synapse login --url ORIGIN --api-key-stdin",
                       errors.USAGE)
    api.target.api_key, api.target.managed = key, False
    api.get("/agents")  # refused keys are never kept
    api.target.keep_key(key)
    out.text(f"Kept the key for {api.target.origin}. `synapse logout --url {api.target.origin}` forgets it.")
    return errors.OK


@_handler
def logout(args, api: ApiClient, out: Output):
    api.target.save_session(None)
    if api.target.forget_kept_key():
        out.text(f"Forgot the key kept for {api.target.origin}.")
    else:
        out.text("Logged out.")


@_handler
def target_show(args, api: ApiClient, out: Output):
    """Which Synapse the CLI talks to, with which key, and whether it answers."""
    t = api.target
    reachable, why = True, None
    try:
        api.get("/agents")
    except CliError as exc:
        reachable, why = False, exc.message
    info = {"origin": t.origin, "api_base": t.api_base, "key_from": t.key_from,
            "key": (t.api_key[:11] + "…") if t.api_key else None, "reachable": reachable}
    if why:
        info["error"] = why
    if out.json_mode:
        out.document(info)
    else:
        out.fields([("Origin", t.origin), ("API", t.api_base), ("Key", f"{info['key'] or '—'} ({t.key_from})"),
                    ("Answers", "yes" if reachable else f"no: {why}")])
    return errors.OK if reachable else errors.UNAVAILABLE


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


class Tree:
    """How each module adds its commands: every command takes the target flags, every group a verb."""

    def __init__(self, sub):
        self.sub = sub
        self.flags = _client_flags()

    def command(self, parent, name, func, help, **kw):
        from synapse.client.help import EXAMPLES

        words = f"{parent._prog_prefix} {name}".split(" ", 1)[1]  # its words after `synapse`
        if words in EXAMPLES:
            kw.setdefault("epilog", EXAMPLES[words])
            kw.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        p = parent.add_parser(name, parents=[self.flags], help=help, description=help, **kw)
        p.set_defaults(func=func)
        return p

    def group(self, parent, name, help, **kw):
        from synapse.client.help import CommandsHelp

        p = parent.add_parser(name, help=help, description=help, formatter_class=CommandsHelp, **kw)
        p._positionals.title = "commands"
        verbs = p.add_subparsers(dest=f"verb_{name}", metavar="COMMAND")
        verbs.required = True
        return verbs


def _decision(p) -> None:
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="allow the tool call it is asking about")
    decision.add_argument("--reject", action="store_true", help="refuse the tool call it is asking about")
    p.add_argument("--reason", help="why, with --reject")


def register(sub) -> None:
    """Add the API commands to `synapse`'s subparsers."""
    from synapse.client import boards, build, knowledge, tools

    tree = Tree(sub)
    add, group = tree.command, tree.group

    p = add(sub, "login", login, "Log the CLI in to this machine's Synapse, or keep another's key")
    p.add_argument("--username")
    p.add_argument("--api-key-stdin", dest="api_key_to_keep", action="store_const", const="",
                   help="read a key on stdin and keep it for --url's Synapse (cli.json, 0600)")
    add(sub, "logout", logout, "Log the CLI out, or forget a kept key")
    add(sub, "target", target_show, "Which Synapse the CLI talks to, with which key, and whether it answers")

    agents = group(sub, "agents", "List, inspect, create, change and delete agents")
    _quiet_flag(add(agents, "list", agents_list, "List agents"))
    add(agents, "show", agents_show, "Show one agent").add_argument("agent", help="id or name")

    workflows = group(sub, "workflows", "List, inspect, build, check, deploy and delete workflows",
                      aliases=["orchestrations", "orch"])
    _quiet_flag(add(workflows, "list", workflows_list, "List workflows"))
    add(workflows, "show", workflows_show, "Show one workflow and its steps").add_argument(
        "workflow", help="id or name")

    build.register(tree, agents=agents, workflows=workflows)

    p = add(sub, "run", run_workflow, "Run a workflow and follow it to the end")
    p.add_argument("workflow", help="id or name")
    p.add_argument("-m", "--message", help="what to work on (the run's user_input)")
    p.add_argument("-i", "--input", action="append", metavar="KEY=VALUE",
                   help="an input for the run's state; VALUE is read as JSON when it is JSON")
    p.add_argument("--input-file", metavar="FILE", help="a JSON object of inputs; - reads stdin")
    p.add_argument("--detach", action="store_true", help="print the run id once it starts, and leave it running")
    p.add_argument("--webhook", metavar="URL", help="POST the outcome here when the run ends, signed (async API)")
    p.add_argument("--webhook-secret", metavar="SECRET", help="sign the webhook with this (X-Synapse-Signature)")

    runs = group(sub, "runs", "List, read, answer and stop runs, and see what waits on a person")
    p = add(runs, "list", runs_list, "Recent runs, newest first")
    p.add_argument("--status", choices=["running", "paused", "completed", "failed", "cancelled"])
    p.add_argument("--workflow", "--orchestration", dest="workflow", help="id or name")
    p.add_argument("--limit", type=int, default=20)
    _quiet_flag(p)
    add(runs, "show", runs_show, "A run's state, its question or its result").add_argument(
        "run_id", help="the run's id, from `runs list`")
    p = add(runs, "logs", runs_logs, "A run's events")
    p.add_argument("run_id")
    p.add_argument("-f", "--follow", action="store_true", help="keep following until the run ends")
    p = add(runs, "answer", runs_answer, "Answer the question a paused run is asking")
    p.add_argument("run_id", help="the run's id, from `runs list` or `runs waiting`")
    p.add_argument("text", nargs="?", help="the answer, for a question with no fields")
    p.add_argument("--field", action="append", metavar="NAME=VALUE", help="a field's answer")
    p.add_argument("--question", metavar="ID",
                   help="which question, when the run asks several at once (`runs show` names each id)")
    _decision(p)
    add(runs, "cancel", runs_cancel, "Stop a run").add_argument("run_id")
    p = add(runs, "waiting", runs_waiting, "What waits on a person: runs' questions and held tool calls")
    p.add_argument("--kind", choices=["run_question", "tool_approval"])
    p.add_argument("--limit", type=int, default=0, help="at most this many (default: all)")

    scheds = group(sub, "schedules", "List, create, change, pause and run schedules")
    _quiet_flag(add(scheds, "list", schedules_list, "List schedules"))
    add(scheds, "show", schedules_show, "Show one schedule").add_argument("schedule_id")
    p = add(scheds, "create", schedules_create, "Create a schedule")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--workflow", "--orchestration", dest="workflow", help="id or name")
    what.add_argument("--agent", help="id or name")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--every", help="an interval: 30m, 2h, 1d")
    when.add_argument("--cron", help='a cron expression, such as "0 9 * * 1"')
    p.add_argument("--prompt", help="what each run is asked")
    p.add_argument("--name")
    p.add_argument("--missed", choices=["skip", "run_once"], help="what to do about runs missed while stopped")
    p.add_argument("--paused", action="store_true", help="create it paused")
    p = add(scheds, "update", schedules_update, "Change a schedule's timing, prompt or name")
    p.add_argument("schedule_id")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--every", help="an interval: 30m, 2h, 1d")
    when.add_argument("--cron", help='a cron expression, such as "0 9 * * 1"')
    p.add_argument("--prompt")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--missed", choices=["skip", "run_once"])
    add(scheds, "pause", _set_enabled(False), "Pause a schedule").add_argument("schedule_id")
    add(scheds, "resume", _set_enabled(True), "Turn a paused schedule back on").add_argument("schedule_id")
    add(scheds, "trigger", schedules_trigger, "Run a schedule now, as a test").add_argument("schedule_id")
    p = add(scheds, "approve", schedules_approve, "Allow or refuse a tool call a scheduled run stopped on")
    p.add_argument("schedule_id")
    p.add_argument("--question", metavar="ID", help="which member's question, when several wait")
    p.add_argument("--agent", help="the agent whose question it is, when several wait")
    _decision(p)
    p = add(scheds, "delete", schedules_delete, "Delete a schedule")
    p.add_argument("schedule_id")
    p.add_argument("--yes", action="store_true", help="do not ask first")

    p = add(sub, "chat", chat, "Chat with an agent")
    p.add_argument("--agent", help="id or name (default: the first agent)")
    p.add_argument("--session", help="continue a conversation by its id")
    p.add_argument("-m", "--message", help="send one message, print the answer and leave")
    p.add_argument("--verbose", action="store_true", help="show the agent's tool calls and results")
    held = p.add_argument_group("a held tool call", "with --session: answer the call the conversation stopped "
                                                    "on, without a terminal")
    decision = held.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="allow it, and carry the turn on")
    decision.add_argument("--reject", action="store_true", help="refuse it, and carry the turn on")
    held.add_argument("--reason", help="why, with --reject (the agent is told)")
    held.add_argument("--question", metavar="ID", help="which team member's call, when several wait")

    knowledge.register(tree)
    boards.register(tree)
    tools.register(tree)

    add(sub, "mcp", mcp, "Serve this Synapse to an MCP client (Claude, Cursor, …) over stdio")

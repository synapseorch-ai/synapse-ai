"""`synapse chat`: a conversation with an agent, at the terminal (D126).

A REPL on a TTY; one message in and the answer out otherwise (`-m`, or a pipe).
The answer arrives whole — v1 sends it in one `response` event — so while a
turn runs the CLI shows what the agent is doing, then renders the answer as
Markdown. A question the turn stops on is answered here: a tool call to allow,
or the question of a run the agent started. Ctrl-C stops the turn, not the chat.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from rich.markup import escape

from synapse.client import commands, errors, look
from synapse.client.commands import _ask, _drive, _yes_no
from synapse.client.errors import CliError
from synapse.client.http import ApiClient
from synapse.client.output import Output

HELP = """\
/agents          list the agents
/agent NAME      talk to another agent (starts a new conversation)
/new             start a new conversation with this agent
/session         show this conversation's id, to continue it with --session
/exit            leave (Ctrl-D does too)
End a line with \\ to keep typing, or open and close a block with \"\"\"."""




class Chat:
    def __init__(self, api: ApiClient, out: Output, agent: dict, session: str | None, verbose: bool):
        self.api = api
        self.out = out
        self.agent = agent
        # None until the server names the conversation in its first `session`
        # event: cloud mints every session id and 404s one it did not (D124).
        self.session = session
        self.verbose = verbose

    # ── one turn ────────────────────────────────────────────────────────────

    def send(self, message: str) -> int:
        body = {"message": message, "agent": self.agent["id"]}
        if self.session:
            body["session_id"] = self.session
        return self._follow(self.api.stream("POST", "/chat/stream", body))

    def answer(self, approve: bool, reason: str = "", question_id: str | None = None,
               approval_id: str | None = None) -> int:
        """Allow or refuse the call this conversation stopped on, and show the turn it carries on with."""
        body = {"approve": approve, "reason": reason, "agent": self.agent["id"]}
        if approval_id:
            body["approval_id"] = approval_id
        if question_id:
            body["question_id"] = question_id
        return self._follow(self.api.stream("POST", f"/chat/{self.session}/approve", body))

    def _follow(self, frames) -> int:
        """Show a turn to its end; answer what it stops on when someone is here to."""
        try:
            pause = self._render(frames)
        except KeyboardInterrupt:
            if self.session:
                self.api.post(f"/chat/{self.session}/cancel")
            self.out.note("Stopped.")
            return errors.ERROR
        if pause is None:
            return self._last_code
        if self.out.json_mode or not commands._interactive():
            if pause["type"] == "awaiting_approval":
                asked = [q for q in pause.get("questions") or [] if q.get("question_id")]
                for q in asked:
                    self.out.note(f"  ? {_member_line(q)}", style="yellow")
                    self.out.note(f"    synapse chat --session {self.session} --question {q['question_id']} "
                                  "--approve|--reject", style="yellow")
                if not asked:
                    call = pause.get("pending_call") or {}
                    self.out.note(f"  ? {call.get('tool_name')} {call.get('tool_args') or ''}".rstrip(),
                                  style="yellow")
                    self.out.note(f"The agent is waiting for an answer. Give it with: "
                                  f"synapse chat --session {self.session} --approve|--reject", style="yellow")
                return errors.WAITING
            self.out.note(f"The agent is waiting for an answer. Continue with: synapse chat --session {self.session}",
                          style="yellow")
            return errors.WAITING
        if pause["type"] == "awaiting_approval":
            call = pause.get("pending_call") or {}
            asked = [q for q in pause.get("questions") or [] if q.get("question_id")]
            if asked:
                # A team's members each stopped on a call (TEAM1c): each is its own question.
                question = asked[0]
                who, tool, args = question.get("member_name") or "A member", question.get("tool_name"), \
                    question.get("tool_args")
            else:
                question = None
                who, tool, args = self.agent["name"], call.get("tool_name"), call.get("tool_args")
            look.question_box(self.out.err, "Allow this call?",
                              [f"{who} wants to run {tool}", *([f"  {args}"] if args else []),
                               *([str(pause.get("prompt"))] if not asked and pause.get("prompt")
                                 and tool and tool not in str(pause.get("prompt")) else [])])
            allowed = _yes_no("  Allow it?")
            reason = "" if allowed else input("Why not? (optional) ").strip()
            return self.answer(allowed, reason, question_id=(question or {}).get("question_id"),
                               approval_id=(question or call).get("approval_id"))
        # A run the agent started is asking a person.
        run_id = pause.get("run_id")
        run = self.api.get(f"/orchestrations/runs/{run_id}")
        question = run.get("human_input_required") or {"prompt": pause.get("prompt"), "fields": []}
        answer = _ask({**question, "run_id": run_id}, self.out)
        frames = self.api.stream("POST", f"/orchestrations/runs/{run_id}/resume/stream", {"response": answer})
        return _drive(self.api, self.out, frames)

    def _render(self, frames) -> dict | None:
        """Print a turn's events; return the pause it ended on, if any."""
        self._last_code = errors.OK
        spinner = None
        # At a terminal a turn is drawn as Claude Code and Codex draw one (D294): a working line while it thinks,
        # each tool call and what it answered, the answer under the agent's name, and how long it took. Piped,
        # only the answer is stdout's, as scripts read it.
        tty = not self.out.json_mode and self.out.err.is_terminal
        boxed = not self.out.json_mode and self.out.out.is_terminal
        started, tools = time.monotonic(), 0
        answered = False
        if tty:
            spinner = look.working(self.out.err)
        try:
            for frame in frames:
                event = frame["data"]
                if not isinstance(event, dict):
                    continue
                if self.out.json_mode:
                    self.out.event(event)
                kind = event.get("type")
                if kind is None and event.get("live"):
                    # A working team member took the answer at once; the turn carries on (TEAM1c).
                    if not self.out.json_mode:
                        self.out.note("Answered; the team carries on.")
                    break
                if kind == "session" and event.get("session_id"):
                    self.session = event["session_id"]
                elif kind == "tool_execution":
                    tools += 1
                    if spinner:
                        spinner.update(f"Running {event.get('tool_name')}…")
                    if tty:
                        self.out.err.print(look.call_line(str(event.get("tool_name")), event.get("args")))
                    elif self.verbose and not self.out.json_mode:
                        self.out.note(f"  ↳ {event.get('tool_name')} {event.get('args')}")
                elif kind == "tool_result" and not self.out.json_mode:
                    if tty:
                        self.out.err.print(look.result_line(event.get("preview") or ""))
                    elif self.verbose:
                        self.out.note(f"    {str(event.get('preview') or '')[:200]}")
                    if spinner:
                        spinner.update("Thinking…")
                elif kind in ("status", "thinking") and spinner and event.get("message"):
                    spinner.update(str(event["message"])[:80])
                elif kind in ("member_started", "helper_started") and not self.out.json_mode:
                    who = event.get("member_name") or event.get("helper_name")
                    task = str(event.get("task") or "").strip().splitlines()
                    self.out.note(f"  · {who}" + (f": {task[0][:80]}" if task else ""))
                elif kind in ("member_finished", "helper_finished") and not self.out.json_mode:
                    who = event.get("member_name") or event.get("helper_name")
                    status = event.get("status") or "done"
                    self.out.note(f"  {'✓' if status in ('completed', 'done') else '·'} {who} {status}",
                                  style="green" if status in ("completed", "done") else "dim")
                elif kind == "agent_step_result" and not self.out.json_mode:
                    self.out.note(f"  ✓ {event.get('step_name')}", style="green")
                elif kind == "response":
                    if spinner:
                        spinner.stop()
                        spinner = None
                    if boxed:
                        look.said(self.out.out, self.agent["name"], event.get("content") or "")
                    elif not self.out.json_mode:
                        self.out.markdown(event.get("content") or "")
                    answered = True
                elif kind in ("awaiting_approval", "paused"):
                    return event
                elif kind == "error":
                    self._last_code = errors.ERROR
                    if not self.out.json_mode:
                        self.out.error(event.get("message") or "The agent failed.")
                elif kind == "cancelled":
                    self._last_code = errors.ERROR
                    if not self.out.json_mode:
                        self.out.note("Stopped.")
                elif kind == "done":
                    break
        finally:
            if spinner:
                spinner.stop()
        if tty and answered:
            took = time.monotonic() - started
            self.out.note(f"  {took:.1f}s" + (f" · {tools} tool{'s' if tools != 1 else ''}" if tools else ""))
        return None

    # ── the REPL ────────────────────────────────────────────────────────────

    def command(self, line: str) -> bool:
        """Run a slash command; False means leave."""
        name, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if name in ("exit", "quit", "q"):
            return False
        if name == "help":
            self.out.note(HELP, style="")
        elif name == "agents":
            for a in self.api.get("/agents"):
                marker = "•" if a["id"] == self.agent["id"] else " "
                self.out.note(f"{marker} {a['name']}  ({a['id']})", style="")
        elif name == "agent":
            if not rest:
                self.out.note(f"Talking to {self.agent['name']}. /agent NAME to switch.", style="")
            else:
                try:
                    self.agent = commands._find(self.api, "agent", rest)
                except CliError as exc:
                    self.out.error(exc.message)
                    return True
                self.session = None
                self.out.note(f"Now talking to {self.agent['name']}.", style="bold")
        elif name == "new":
            self.session = None
            self.out.note("New conversation.", style="bold")
        elif name == "session":
            self.out.note(self.session or "No conversation yet: send a message first.", style="")
        else:
            self.out.error(f"There is no /{name}. /help lists them.")
        return True

    def repl(self) -> int:
        _history()
        target = self.api.target
        if not self.out.err.is_terminal:
            self.out.err.print(f"Chatting with {self.agent['name']}  /help for commands · Ctrl-D leaves",
                               markup=False, highlight=False)
        else:
            self._welcome(target)
        return self._loop()

    def _welcome(self, target) -> None:
        look.welcome(self.out.err, "Synapse", [
            ("agent", " · ".join(x for x in (self.agent["name"], self.agent.get("model")) if x)),
            ("talking to", target.origin),
            ("key", {"this machine": "this machine's own", "kept": "kept for this Synapse"}.get(
                target.key_from, f"from {target.key_from}")),
            ("conversation", self.session or "a new one"),
        ], "/help commands · /agent NAME switch · Ctrl-C stops a reply · Ctrl-D leaves")

    def _loop(self) -> int:
        while True:
            try:
                fancy = look.is_terminal(self.out.err)
                if fancy:
                    self.out.err.print()
                    look.composer_rule(self.out.err)
                message = _read_message("› " if fancy else "you › ")
                if fancy and message.strip() and not message.startswith("/"):
                    look.echo_message(self.out.err, message, typed_lines=1 + message.count("\n") + 1)
            except EOFError:
                self.out.note("")
                return errors.OK
            except KeyboardInterrupt:
                self.out.note("")
                continue
            if not message.strip():
                continue
            if message.startswith("/"):
                if not self.command(message.strip()):
                    return errors.OK
                continue
            try:
                self.send(message)
            except CliError as exc:
                self.out.error(exc.message)
            except KeyboardInterrupt:
                self.out.note("")


def _read_message(prompt: str = "you › ") -> str:
    first = input(prompt)
    if first.strip() == '"""':
        lines = []
        while (line := input("… ")).strip() != '"""':
            lines.append(line)
        return "\n".join(lines)
    lines = [first]
    while lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1]
        lines.append(input("… "))
    return "\n".join(lines)


def _history() -> None:
    """Line editing and a history kept between chats, where readline exists."""
    try:
        import atexit
        import readline
    except ImportError:  # Windows: plain input()
        return
    path = Path(os.getenv("SYNAPSE_CHAT_HISTORY") or Path.home() / ".synapse" / "chat_history")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(1000)
    atexit.register(lambda: _save_history(readline, path))


def _save_history(readline, path: Path) -> None:
    try:
        readline.write_history_file(path)
    except OSError:
        pass


def _member_line(q: dict) -> str:
    args = q.get("tool_args")
    return f"{q.get('member_name') or 'A member'} wants to run {q.get('tool_name')}" + (f" {args}" if args else "")


def run_chat(args, api: ApiClient, out: Output) -> int:
    deciding = getattr(args, "approve", False) or getattr(args, "reject", False)
    if deciding and not args.session:
        raise CliError("--approve and --reject answer a conversation's held call: say which with --session.",
                       errors.USAGE)
    if args.agent:
        agent = commands._find(api, "agent", args.agent)
    else:
        agents = api.get("/agents")
        if not agents:
            raise CliError("There are no agents to chat with yet.")
        agent = agents[0]
    chat = Chat(api, out, agent, args.session, args.verbose)
    if deciding:
        return chat.answer(bool(args.approve), args.reason or "", question_id=args.question)

    if args.message is not None or not sys.stdin.isatty():
        message = args.message if args.message is not None else sys.stdin.read()
        if not message.strip():
            raise CliError("There is no message to send.", errors.USAGE)
        return chat.send(message)
    return chat.repl()

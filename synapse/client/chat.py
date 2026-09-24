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
from pathlib import Path

from rich.markup import escape

from synapse.client import commands, errors
from synapse.client.commands import _ask, _drive, _pick, _yes_no
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
            self.out.note(f"The agent is waiting for an answer. Continue with: synapse chat --session {self.session}",
                          style="yellow")
            return errors.WAITING
        if pause["type"] == "awaiting_approval":
            call = pause.get("pending_call") or {}
            self.out.markdown(pause.get("prompt") or "")
            if call.get("tool_args"):
                self.out.note(f"  {call.get('tool_name')} {call.get('tool_args')}")
            allowed = _yes_no("Allow it?")
            reason = "" if allowed else input("Why not? (optional) ").strip()
            return self._follow(self.api.stream("POST", f"/chat/{self.session}/approve", {
                "approve": allowed, "reason": reason, "approval_id": call.get("approval_id"),
                "agent": self.agent["id"],
            }))
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
        if not self.out.json_mode and self.out.err.is_terminal:
            spinner = self.out.err.status("Thinking…", spinner="dots")
            spinner.start()
        try:
            for frame in frames:
                event = frame["data"]
                if not isinstance(event, dict):
                    continue
                if self.out.json_mode:
                    self.out.event(event)
                kind = event.get("type")
                if kind == "session" and event.get("session_id"):
                    self.session = event["session_id"]
                elif kind == "tool_execution":
                    if spinner:
                        spinner.update(f"Running {event.get('tool_name')}…")
                    if self.verbose and not self.out.json_mode:
                        self.out.note(f"  ↳ {event.get('tool_name')} {event.get('args')}")
                elif kind == "tool_result" and self.verbose and not self.out.json_mode:
                    self.out.note(f"    {str(event.get('preview') or '')[:200]}")
                elif kind in ("status", "thinking") and spinner and event.get("message"):
                    spinner.update(str(event["message"])[:80])
                elif kind == "agent_step_result" and not self.out.json_mode:
                    self.out.note(f"  ✓ {event.get('step_name')}", style="green")
                elif kind == "response":
                    if spinner:
                        spinner.stop()
                        spinner = None
                    if not self.out.json_mode:
                        self.out.markdown(event.get("content") or "")
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
                    self.agent = _pick(self.api.get("/agents"), rest, "agent")
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
        self.out.err.print(f"[bold]Chatting with {escape(self.agent['name'])}[/bold]  "
                           f"[dim]/help for commands · Ctrl-C stops a reply · Ctrl-D leaves[/dim]")
        while True:
            try:
                message = _read_message()
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


def _read_message() -> str:
    first = input("› ")
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


def run_chat(args, api: ApiClient, out: Output) -> int:
    agents = api.get("/agents")
    if not agents:
        raise CliError("There are no agents to chat with yet.")
    agent = _pick(agents, args.agent, "agent") if args.agent else agents[0]
    chat = Chat(api, out, agent, args.session, args.verbose)

    if args.message is not None or not sys.stdin.isatty():
        message = args.message if args.message is not None else sys.stdin.read()
        if not message.strip():
            raise CliError("There is no message to send.", errors.USAGE)
        return chat.send(message)
    return chat.repl()

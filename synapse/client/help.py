"""`synapse --help`, read as a person scans it: commands in sections, then where to start (D294).

argparse lists thirty commands in the order they were added, running this machine's install and driving any
Synapse with a key all in one column. This groups them, and ends with the four commands that get a newcomer
going, how to point at another Synapse, and what each exit code means. Standard library only: `synapse start`
imports it too, and must stay quick.
"""
from __future__ import annotations

import argparse

SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("This machine's Synapse", ("start", "stop", "restart", "status", "logs", "setup", "upgrade", "migrate",
                                "autostart", "access-link", "reset-password", "api-keys", "profile", "uninstall")),
    ("Which Synapse the CLI drives", ("target", "login", "logout")),
    ("Build", ("agents", "workflows")),
    ("Run, answer and schedule", ("run", "runs", "chat", "schedules")),
    ("Knowledge and Boards", ("knowledge", "boards")),
    ("The workspace", ("activity", "usage")),
    ("Every door a key has", ("tools", "api", "mcp")),
)

START_HERE = """\
start here:
  synapse start                      open Synapse on this machine
  synapse workflows list             what there is to run
  synapse run NAME -m "…"            run one, and read its answer
  synapse runs waiting               what waits on you

another Synapse:
  --url https://api.synapseorch.com with --api-key, SYNAPSE_API_KEY, or a key kept by
  `synapse login --url ORIGIN --api-key-stdin`. This machine's needs no key.

for scripts:
  --json prints JSON on stdout (one event a line for a run); -q lists ids only.
  exit codes: 0 done · 1 refused · 2 usage · 3 waiting on a person · 4 run failed
              5 not found · 6 key refused · 7 busy or unreachable"""


class GroupedHelp(argparse.RawDescriptionHelpFormatter):
    """Lists a parser's commands under `SECTIONS`; anything unlisted comes last, under "other"."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        rows = {a.dest: a for a in action._get_subactions()}
        parts, placed = [], set()
        sections = [*SECTIONS, ("Other", tuple(n for n in rows if n not in {m for _, ns in SECTIONS for m in ns}))]
        for title, names in sections:
            chosen = [rows[n] for n in names if n in rows]
            if not chosen:
                continue
            parts.append(f"\n{' ' * self._current_indent}{title}:\n")
            self._indent()
            for row in chosen:
                parts.append(super()._format_action(row))
                placed.add(row.dest)
            self._dedent()
        return "".join(parts)


class CommandsHelp(argparse.HelpFormatter):
    """A group's commands, listed under "commands:" without the bare COMMAND line argparse puts above them."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        return "".join(super(CommandsHelp, self)._format_action(row) for row in action._get_subactions())


def tidy(parser: argparse.ArgumentParser) -> None:
    """Give the top-level parser its sections and its last words."""
    parser.formatter_class = GroupedHelp
    parser.epilog = START_HERE
    parser._positionals.title = "commands"


#: Examples under a command's own --help, by its words after `synapse`.
EXAMPLES: dict[str, str] = {
    "run": """\
examples:
  synapse run "Weekly report" -m "this week's numbers"
  synapse run orch_ab12 -i pages=3 -i topic=cats --detach
  synapse run "Weekly report" --json | jq -c 'select(.type == "final")'""",
    "runs answer": """\
examples:
  synapse runs answer RUN_ID "yes, ship it"           a question with no fields
  synapse runs answer RUN_ID --field ok=true          a question's fields
  synapse runs answer RUN_ID --question q_1 --approve one of a team's questions""",
    "runs waiting": """\
examples:
  synapse runs waiting                 each item says the command that answers it
  synapse runs waiting --json | jq length""",
    "chat": """\
examples:
  synapse chat                                         talk, at the terminal
  synapse chat --agent Researcher -m "summarise today" one message, one answer
  synapse chat --session api_12ab --approve            allow the call it stopped on""",
    "workflows create": """\
examples:
  synapse workflows export "Weekly report" > report.json
  synapse workflows create -f report.json --name "Weekly report (copy)"
  synapse workflows validate "Weekly report (copy)" && synapse workflows deploy "Weekly report (copy)\"""",
    "agents create": """\
examples:
  synapse agents create --name Researcher --prompt "You research." --tools web_search
  synapse agents create --name Crew --type delegate --members Researcher,Writer --prompt-file lead.md""",
    "schedules create": """\
examples:
  synapse schedules create --workflow "Weekly report" --cron "0 9 * * 1" --prompt "last week"
  synapse schedules create --agent Digest --every 2h --paused""",
    "knowledge upload": """\
examples:
  synapse knowledge upload contracts/*.pdf --base Contracts --wait
  synapse knowledge upload notes.md --base Handbook --path policies""",
    "knowledge search": """\
examples:
  synapse knowledge search "termination notice period" --base Contracts --top-k 5
  synapse knowledge search "fees" --filter '{"region": "EU"}' --json""",
    "boards card add": """\
examples:
  synapse boards card add Launch "Write the release notes" --body "Short, for customers\"""",
    "tools call": """\
examples:
  synapse tools list skill
  synapse tools call list_skills
  synapse tools call create_skill --args-file skill.json
  synapse tools call delete_custom_tool --arg name=old_tool --yes""",
    "api": """\
examples:
  synapse api GET /agents
  synapse api GET /orchestrations/runs -F status=failed -F limit=5
  synapse api POST /knowledge/search -d '{"query": "fees", "kb_ids": ["kb_1"]}'
  synapse api GET /api/v2/queue/stats""",
    "login": """\
examples:
  synapse login                                          this machine, when its login is on
  echo "$KEY" | synapse login --url https://api.synapseorch.com --api-key-stdin""",
}

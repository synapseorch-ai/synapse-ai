"""Building agents and workflows from the terminal (D294).

Every write here is a product tool reached through the MCP door (`mcp.py`):
v1 lists and reads agents and workflows, and keys deliberately make no CRUD
route of their own (D124), so the CLI writes through the registry Claude and
the assistant write through — the same checks, the same audit, a delete
previewed and confirmed. A workflow file is what `workflows export` prints
(or `workflows show --json`): `create -f` makes a new workflow from it, and
`update ID -f` replaces that workflow's definition with it.
"""
from __future__ import annotations

import json
import sys

from synapse.client import errors
from synapse.client.commands import (
    Tree, _find, _handler, _json_file, _pairs, confirmed_call, show_value,
)
from synapse.client.errors import CliError
from synapse.client.http import ApiClient
from synapse.client.mcp import Mcp
from synapse.client.output import Output

#: The fields of a workflow file the builder takes, and what it calls each.
_WORKFLOW_FIELDS = ("name", "description", "entry_step_id", "max_total_turns", "timeout_minutes",
                    "max_run_credits", "max_run_usd")
#: An agent file's fields the builder takes on create.
_AGENT_FIELDS = ("name", "description", "type", "tools", "system_prompt", "model", "repos", "db_configs",
                 "delegate_agent_ids", "team", "tool_policy")


def _parsed(value):
    """A builder answer is JSON text; decoded where it is."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _workflow_args(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise CliError("A workflow file holds one JSON object: what `synapse workflows export` prints.",
                       errors.USAGE)
    args = {k: spec[k] for k in _WORKFLOW_FIELDS if spec.get(k) is not None}
    if spec.get("steps") is not None:
        args["steps_json"] = json.dumps(spec["steps"])
    if spec.get("state_schema"):
        args["state_schema_json"] = json.dumps(spec["state_schema"])
    return args


# ── agents ──────────────────────────────────────────────────────────────────


def _agent_spec(args) -> dict:
    spec: dict = {}
    if args.file:
        spec = _json_file(args.file, "-f")
        if not isinstance(spec, dict):
            raise CliError("-f holds one JSON object: an agent's fields.", errors.USAGE)
    prompt = args.prompt
    if args.prompt_file:
        prompt = sys.stdin.read() if args.prompt_file == "-" else open(args.prompt_file, encoding="utf-8").read()
    for key, value in (("name", args.name), ("description", args.description), ("type", args.type),
                       ("system_prompt", prompt), ("model", args.model)):
        if value is not None:
            spec[key] = value
    if args.tools is not None:
        spec["tools"] = [t.strip() for t in args.tools.split(",") if t.strip()]
    if args.members is not None:
        spec["delegate_agent_ids"] = [m.strip() for m in args.members.split(",") if m.strip()]
    return spec


@_handler
def agents_create(args, api: ApiClient, out: Output):
    spec = _agent_spec(args)
    if not spec.get("name"):
        raise CliError("An agent needs a name: --name, or \"name\" in -f.", errors.USAGE)
    if not spec.get("system_prompt"):
        raise CliError("An agent needs its instructions: --prompt, --prompt-file, or \"system_prompt\" in -f.",
                       errors.USAGE)
    body = {"description": "", "type": "conversational", "tools": [],
            **{k: v for k, v in spec.items() if k in _AGENT_FIELDS}}
    if body.get("delegate_agent_ids"):
        body["delegate_agent_ids"] = [_find(api, "agent", m)["id"] for m in body["delegate_agent_ids"]]
    made = _parsed(Mcp(api).call("create_agent", body))
    agent = (made.get("agent") if isinstance(made, dict) else None) or made
    if out.json_mode:
        return out.document(agent)
    if isinstance(agent, dict) and agent.get("id"):
        out.text(agent["id"])
        out.note(f"Made the agent {agent.get('name')}. Talk to it with: synapse chat --agent {agent['id']}")
    else:
        show_value(out, made)


@_handler
def agents_update(args, api: ApiClient, out: Output):
    agent = _find(api, "agent", args.agent)
    fields = {k: v for k, v in _agent_spec(args).items() if k in _AGENT_FIELDS}
    fields.update(_pairs(args.set, "--set"))
    if not fields:
        raise CliError("Say what to change: -f FILE, --set field=value, or a flag such as --prompt.",
                       errors.USAGE)
    if fields.get("delegate_agent_ids"):
        fields["delegate_agent_ids"] = [_find(api, "agent", m)["id"] for m in fields["delegate_agent_ids"]]
    changed = _parsed(Mcp(api).call("update_agent", {"agent_id": agent["id"], "fields": fields}))
    if out.json_mode:
        return out.document(changed)
    out.text(f"Updated {agent.get('name')}: " + ", ".join(sorted(fields)) + ".")


@_handler
def agents_delete(args, api: ApiClient, out: Output):
    agent = _find(api, "agent", args.agent)
    done = confirmed_call(Mcp(api), out, "delete_agent", {"agent_id": agent["id"]}, args.yes,
                          f"Delete the agent '{agent.get('name')}'")
    if done is None:
        return errors.OK
    if out.json_mode:
        return out.document(_parsed(done))
    out.text("Deleted.")


@_handler
def agents_activity(args, api: ApiClient, out: Output):
    """An agent's recent conversations and the workflows it runs in (MCP `agent_activity`)."""
    show_value(out, _parsed(Mcp(api).call("agent_activity", {"name": args.agent})))


# ── workflows ───────────────────────────────────────────────────────────────


@_handler
def workflows_export(args, api: ApiClient, out: Output):
    """A workflow's whole definition as JSON, for `workflows create -f` or `update -f`."""
    orch = _find(api, "workflow", args.workflow)
    keep = {k: v for k, v in orch.items() if k not in ("created_at", "updated_at")}
    sys.stdout.write(json.dumps(keep, indent=2, default=str) + "\n")


@_handler
def workflows_create(args, api: ApiClient, out: Output):
    spec = _json_file(args.file, "-f")
    body = _workflow_args(spec)
    if args.name:
        body["name"] = args.name
    if not body.get("name") or not body.get("steps_json") or not body.get("entry_step_id"):
        raise CliError("A workflow file needs \"name\", \"steps\" and \"entry_step_id\".", errors.USAGE)
    made = _parsed(Mcp(api).call("create_orchestration", body))
    orch = made.get("orchestration") if isinstance(made, dict) else None
    if out.json_mode:
        return out.document(orch or made)
    if not orch:
        return show_value(out, made)
    out.text(orch["id"])
    out.note(f"Made the workflow {orch.get('name')} ({len(orch.get('steps') or [])} steps). "
             f"Check it with: synapse workflows validate {orch['id']}")


@_handler
def workflows_update(args, api: ApiClient, out: Output):
    orch = _find(api, "workflow", args.workflow)
    body = _workflow_args(_json_file(args.file, "-f")) if args.file else {}
    for key, value in (("name", args.name), ("description", args.description)):
        if value is not None:
            body[key] = value
    if not body:
        raise CliError("Say what to change: -f FILE, --name or --description.", errors.USAGE)
    changed = _parsed(Mcp(api).call("update_orchestration", {"orch_id": orch["id"], **body}))
    if out.json_mode:
        return out.document(changed)
    out.text(f"Updated {orch.get('name')}.")


@_handler
def workflows_validate(args, api: ApiClient, out: Output):
    """Whether a workflow would run: its steps, the agents and workflows they name, its knowledge bases."""
    orch = _find(api, "workflow", args.workflow)
    result = _parsed(Mcp(api).call("validate_orchestration", {"orch_id": orch["id"]}))
    verdict = result if isinstance(result, dict) else {}
    valid = bool(verdict.get("ok"))
    if out.json_mode:
        out.document(result)
    else:
        out.text(f"{orch.get('name')} is ready to run." if valid else f"{orch.get('name')} would not run yet:")
        for issue in verdict.get("issues") or []:
            out.text(f"  ✗ {issue}")
        for warning in verdict.get("warnings") or []:
            out.note(f"  ! {warning}", style="yellow")
    return errors.OK if valid else errors.ERROR


@_handler
def workflows_deploy(args, api: ApiClient, out: Output):
    """Make a workflow an agent: chat to it, or give it a channel, a widget or a schedule."""
    orch = _find(api, "workflow", args.workflow)
    result = _parsed(Mcp(api).call("deploy_orchestration", {"orch_id": orch["id"]}))
    if out.json_mode:
        return out.document(result)
    agent_id = result.get("agent_id") if isinstance(result, dict) else None
    if agent_id:
        out.text(agent_id)
        out.note(f"{orch.get('name')} is an agent now. Talk to it with: synapse chat --agent {agent_id}")
    else:
        show_value(out, result)


@_handler
def workflows_delete(args, api: ApiClient, out: Output):
    orch = _find(api, "workflow", args.workflow)
    done = confirmed_call(Mcp(api), out, "delete_orchestration", {"orch_id": orch["id"]}, args.yes,
                          f"Delete the workflow '{orch.get('name')}'")
    if done is None:
        return errors.OK
    if out.json_mode:
        return out.document(_parsed(done))
    out.text("Deleted. Its run history is kept.")


# ── the tree ────────────────────────────────────────────────────────────────


def _agent_flags(p) -> None:
    p.add_argument("-f", "--file", metavar="FILE", help="a JSON object of the agent's fields; - reads stdin")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--type", choices=["conversational", "code", "delegate"],
                   help="delegate is a Team agent: its --members work together")
    p.add_argument("--prompt", help="its instructions")
    p.add_argument("--prompt-file", metavar="FILE", help="its instructions, from a file; - reads stdin")
    p.add_argument("--tools", metavar="A,B", help="tool names, comma-separated, or all")
    p.add_argument("--model", help="a model id, such as gpt-4.1-mini (default: the workspace's)")
    p.add_argument("--members", metavar="A,B", help="a Team agent's members: agent ids or names")


def register(tree: Tree, agents, workflows) -> None:
    add = tree.command

    p = add(agents, "create", agents_create, "Make an agent")
    _agent_flags(p)
    p = add(agents, "update", agents_update, "Change an agent")
    p.add_argument("agent", help="id or name")
    _agent_flags(p)
    p.add_argument("--set", action="append", metavar="FIELD=VALUE",
                   help="any other field; VALUE is read as JSON when it is JSON")
    p = add(agents, "delete", agents_delete, "Delete an agent (refused while anything uses it)")
    p.add_argument("agent", help="id or name")
    p.add_argument("--yes", action="store_true", help="do not ask first")
    add(agents, "activity", agents_activity, "An agent's recent conversations and workflows").add_argument(
        "agent", help="its name, or part of it")

    add(workflows, "export", workflows_export, "Print a workflow's definition, for create -f or update -f"
        ).add_argument("workflow", help="id or name")
    p = add(workflows, "create", workflows_create, "Make a workflow from a file")
    p.add_argument("-f", "--file", metavar="FILE", required=True,
                   help="a workflow's definition (what `workflows export` prints); - reads stdin")
    p.add_argument("--name", help="its name, instead of the file's")
    p = add(workflows, "update", workflows_update, "Change a workflow: its definition from a file, or its name")
    p.add_argument("workflow", help="id or name")
    p.add_argument("-f", "--file", metavar="FILE", help="the new definition; its steps replace the old ones")
    p.add_argument("--name")
    p.add_argument("--description")
    add(workflows, "validate", workflows_validate, "Check that a workflow would run").add_argument(
        "workflow", help="id or name")
    add(workflows, "deploy", workflows_deploy, "Make a workflow an agent you can chat to").add_argument(
        "workflow", help="id or name")
    p = add(workflows, "delete", workflows_delete, "Delete a workflow (refused while something runs it)")
    p.add_argument("workflow", help="id or name")
    p.add_argument("--yes", action="store_true", help="do not ask first")

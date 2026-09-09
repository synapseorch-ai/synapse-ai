"""
Whether a tool call may run, and who decides.

Until now the only governance was a name allowlist, decided before the model
was ever called: a tool was on the agent's list or it was not. Nothing asked
what the tool *did*. `run_sql_query` — one door for `SELECT` and `DROP` — was
admitted or refused as a unit, and the system prompt asked the model to
"confirm with the user before any write query", which is a sentence, not a
gate. The builder's one real guard told the *model* to call again with
`confirm=true`, which the model can simply do.

Tools now declare their behaviour (`core.tools.tool_hints`), so a policy can
be about behaviour rather than names.

    destructive   modifies data, not reversibly
    open_world    reaches systems outside this workspace
    undeclared    the tool did not say, so we do not know

Each maps to `allow`, `confirm` or `deny`.

**The default is `allow` everywhere**, so no existing agent changes behaviour
on upgrade. This is the same reasoning as the DB allowlist's empty default:
a closed default would silently break every agent already running. A
deployment opts in with the `tool_policy_defaults` setting; an agent overrides
with its own `tool_policy`.

Trust is asymmetric on purpose. An external MCP server's hints are a claim,
not a boundary — the MCP spec says so plainly. So a third party saying "I am
destructive" is believed, and one saying "I am safe" is not: its read-only
claim is treated as `undeclared`. Our own servers are declared in this repo
and are believed either way.
"""
from __future__ import annotations

from typing import Literal

Decision = Literal["allow", "confirm", "deny"]

#: The categories a policy can speak about, riskiest first — the first one a
#: tool matches decides.
CATEGORIES = ("destructive", "open_world", "undeclared")

#: No agent changes behaviour on upgrade.
DEFAULT_POLICY: dict[str, Decision] = {c: "allow" for c in CATEGORIES}

_VALID: frozenset[str] = frozenset(("allow", "confirm", "deny"))


def resolve_policy(agent: dict | None, settings: dict | None) -> dict[str, Decision]:
    """The effective policy: built-in defaults, then the deployment's, then
    the agent's. Unknown keys and values are ignored rather than obeyed."""
    policy = dict(DEFAULT_POLICY)
    for source in ((settings or {}).get("tool_policy_defaults"), (agent or {}).get("tool_policy")):
        if not isinstance(source, dict):
            continue
        for category, decision in source.items():
            if category in CATEGORIES and decision in _VALID:
                policy[category] = decision
    return policy


def decide(tool, policy: dict[str, Decision]) -> tuple[Decision, str]:
    """`(decision, category)` for one tool.

    `category` is why — it reaches the approval card and the refusal the model
    reads, so a person is never asked to approve something unexplained.
    """
    from core.tools import tool_hints

    hints = tool_hints(tool, trusted=getattr(tool, "trusted", True))

    if hints.declared and hints.destructive:
        return policy.get("destructive", "allow"), "destructive"
    if hints.declared and hints.open_world:
        return policy.get("open_world", "allow"), "open_world"
    # A trusted read-only declaration is the one thing that earns a pass. An
    # untrusted server's "I am safe" is not evidence, so it falls through.
    if hints.declared and hints.read_only and hints.trusted:
        return "allow", "read_only"
    return policy.get("undeclared", "allow"), "undeclared"


#: Shown to the model when a call is refused, and on the approval card.
REASONS = {
    "destructive": "it can modify data in a way that cannot be undone",
    "open_world": "it reaches systems outside this workspace",
    "undeclared": "it does not declare what it does",
}


def explain(category: str) -> str:
    return REASONS.get(category, "this agent's tool policy does not allow it")

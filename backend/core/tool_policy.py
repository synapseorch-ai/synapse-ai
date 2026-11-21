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

Decision = Literal["allow", "confirm", "deny", "check"]

#: The categories a policy can speak about, riskiest first — the first one a
#: tool matches decides.
CATEGORIES = ("destructive", "open_world", "undeclared")

#: No agent changes behaviour on upgrade.
DEFAULT_POLICY: dict[str, Decision] = {c: "allow" for c in CATEGORIES}

_VALID: frozenset[str] = frozenset(("allow", "confirm", "deny"))

#: A policy may also name one tool, `tool:<name>`, and that decision wins over the tool's category (BWA1, D274): an
#: agent that books can ask before `create_booking` while its searches run freely, and the AI builder can say so
#: when it builds the agent. A visitor's `confirm` is a `deny` for these keys too (the rewrites map every key).
TOOL_KEY = "tool:"

#: One tool's "ask unless it only reads" (F1679): decided each call from the tool's real declaration, as a build that
#: could read it decides (`builder_guard.acts`). Saved for a tool whose app wasn't connected when it was built, so
#: what it does couldn't be read then; a person may choose it too. A tool key's decision only, never a category's.
CHECK = "check"
#: A visitor's `check` (`as_visitor`): its ask is a refusal, as a visitor's `confirm` is. Never stored.
_CHECK_DENY = "check_deny"
_TOOL_VALID: frozenset[str] = frozenset((*_VALID, CHECK, _CHECK_DENY))


def _is_tool_key(key) -> bool:
    return isinstance(key, str) and key.startswith(TOOL_KEY) and len(key) > len(TOOL_KEY)


def as_visitor(policy: dict) -> dict:
    """A policy as a visitor's turn holds it: nobody is there to approve, so every ask is a refusal, a `check`'s too."""
    return {c: ("deny" if d == "confirm" else _CHECK_DENY if d == CHECK else d) for c, d in policy.items()}


def resolve_policy(agent: dict | None, settings: dict | None) -> dict[str, Decision]:
    """The effective policy: built-in defaults, then the deployment's, then
    the agent's. Unknown keys and values are ignored rather than obeyed."""
    policy = dict(DEFAULT_POLICY)
    for source in ((settings or {}).get("tool_policy_defaults"), (agent or {}).get("tool_policy")):
        if not isinstance(source, dict):
            continue
        for category, decision in source.items():
            if (category in CATEGORIES and decision in _VALID) or (_is_tool_key(category) and decision in _TOOL_VALID):
                policy[category] = decision
    return policy


def decide(tool, policy: dict[str, Decision]) -> tuple[Decision, str]:
    """`(decision, category)` for one tool.

    `category` is why — it reaches the approval card and the refusal the model
    reads, so a person is never asked to approve something unexplained.
    """
    from core.tools import tool_hints

    name = getattr(tool, "name", None)
    own = policy.get(f"{TOOL_KEY}{name}") if name else None
    if own in _VALID:
        return own, "tool"
    if own in (CHECK, _CHECK_DENY):
        # Decided now, from what the tool declares (F1679): it runs when it only reads, and asks (a visitor: is
        # refused) for anything else, saying why as its category does.
        from core.builder_guard import acts

        if not acts(name, tool):
            return "allow", "read_only"
        _, category = decide(tool, {k: v for k, v in policy.items() if k != f"{TOOL_KEY}{name}"})
        return ("confirm" if own == CHECK else "deny"), (category if category in REASONS else "undeclared")

    hints = tool_hints(tool, trusted=getattr(tool, "trusted", True))

    if hints.declared and hints.destructive:
        return policy.get("destructive", "allow"), "destructive"
    if hints.declared and hints.open_world:
        return policy.get("open_world", "allow"), "open_world"
    # A trusted read-only declaration is the one thing that earns a pass. An
    # untrusted server's "I am safe" is not evidence, so it falls through.
    if hints.declared and hints.read_only and hints.trusted:
        return "allow", "read_only"
    # A tool that says it changes something and stays inside (a custom tool whose person unticked "reaches
    # outside", TAG1) is decided by the `undeclared` policy, never looser than before, and says what it declared.
    if hints.declared and not hints.read_only:
        return policy.get("undeclared", "allow"), "changes"
    return policy.get("undeclared", "allow"), "undeclared"


#: Shown to the model when a call is refused, and on the approval card.
REASONS = {
    "destructive": "it can modify data in a way that cannot be undone",
    "open_world": "it reaches systems outside this workspace",
    "undeclared": "it does not declare what it does",
    # Not a policy key: decided by `undeclared` (TAG1).
    "changes": "it changes something in this workspace",
    "tool": "this agent's tool policy names this tool",
    # Not a policy category: a `remember` in a turn that read a file or a
    # tool's result is always held for a person (F279, `react_engine`).
    "memory_from_content": ("it would save something this turn read from a file or a tool "
                            "into the agent's permanent memory"),
}


def explain(category: str) -> str:
    return REASONS.get(category, "this agent's tool policy does not allow it")


def clean_policy(value) -> dict[str, Decision]:
    """What a write path should store: known categories, known decisions, in
    riskiest-first order.

    Not a safety check — `resolve_policy` already ignores anything it does not
    understand, and it has to, because a policy can arrive from an import or a
    hand-edited document. This is what stops a *write* from persisting junk that
    every later reader then has to ignore again, and what keeps two agents with
    the same policy storing the same JSON.

    An empty dict means inherit: for an agent, the deployment's defaults; for a
    deployment, `allow`.
    """
    if not isinstance(value, dict):
        return {}
    out = {c: value[c] for c in CATEGORIES if value.get(c) in _VALID}
    # A tool's `check` is stored too (F1679); a visitor's form of it never is.
    out.update({k: value[k] for k in sorted(value) if _is_tool_key(k) and value[k] in (*_VALID, CHECK)})
    return out

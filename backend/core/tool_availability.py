"""A tool a step or an agent names that isn't available now, said with its app's own reason (F1349; Naveen 11-04: "if
a workflow fails because a tool isn't available, it should fail gracefully").

A remote MCP app drops out when its sign-in or token expires, or is disconnected, turned off or deleted; a tool can be
deleted too. What the host knows of the app is read from its own record — OSS's manager (`MCPClientManager`: each
app's status, `reauth_needed` among them, and F1306's `last_error`, scrubbed when it was kept) or a cloud worker's
(`WorkerServerModule`: the apps it could not open, those that need signing in again, and why) — and said one way,
scrubbed again (`run_error.clean`):

* `why_unavailable(name, server_module)` — the tool, its app, why, and what to do;
* its `words()` are a Tool step's failure: the step stops there, and Resume is safe, since the tool never ran;
  `LEAD` begins them, which is how triage reads their class (`tool_unavailable`);
* its `note()` is an agent's: it runs with the tools it has and is told which one is unavailable, and why.

An app's tools are named `{app}__{tool}`, as an agent's list holds them (`tools.aggregate_all_tools`); the product's own
servers' tools and a tool turned off in settings are not this reading's (B, eb 11-04).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SEP = "__"
#: How a Tool step's failure begins: triage reads its class by it (`triage.classify`).
LEAD = "Not run: the tool "
SAFE = "Resuming the run is safe: the tool never ran."
#: A failure that says the app refused its sign-in, in any server's words.
_SIGN_IN = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid[_ ]?token|token (?:has )?expired|expired token|"
                      r"invalid[_ ]grant|re-?auth|sign(?:ed)? ?in again|auth(?:entication)?[_ ]required", re.I)
_REASON_CHARS = 240


@dataclass(frozen=True)
class Unavailable:
    name: str
    app: str | None
    why: str
    fix: str

    def words(self) -> str:
        """A Tool step's failure, whole."""
        return f"{LEAD}{self.name} isn't available — {self.why}. {self.fix} {SAFE}"

    def note(self) -> str:
        """An agent's: the tool and why, for the line that lists them."""
        return f"{self.name} ({self.why})"

    def answer(self) -> str:
        """What an agent that calls it anyway is answered."""
        return (f"The tool {self.name} isn't available — {self.why}. Do what you can without it, and say in your "
                "answer which tool was unavailable and why.")


def app_of(name: Any) -> str | None:
    """The app a tool's name says it is of (`{app}__{tool}`), or None."""
    if not isinstance(name, str) or SEP not in name:
        return None
    app = name.split(SEP, 1)[0]
    return app or None


def is_native(app: str | None) -> bool:
    from core.tools_registry import ALL_NATIVE_TOOLS

    return bool(app) and app in ALL_NATIVE_TOOLS


def app_state(server_module: Any, app: str | None) -> tuple[str | None, str | None]:
    """The app's status and why it last failed, from the host's own record; (None, None) when it has none."""
    if not app:
        return None, None
    manager = getattr(server_module, "mcp_manager", None)
    config = manager.get_server_config(app) if manager is not None and hasattr(manager, "get_server_config") else None
    if isinstance(config, dict):
        status = "disabled" if config.get("enabled") is False else config.get("status")
        return status, config.get("last_error")
    reasons = getattr(server_module, "mcp_errors", None) or {}
    if app in (getattr(server_module, "mcp_reauth_needed", None) or ()):
        return "reauth_needed", reasons.get(app)
    if app in (getattr(server_module, "mcp_disabled", None) or ()):
        return "disconnected", reasons.get(app)
    return None, reasons.get(app)


def signed_out(error: Any) -> bool:
    """Whether a call's failure says the app refused its sign-in."""
    return bool(_SIGN_IN.search(str(error or "")))


def _said(text: Any) -> str:
    from core.orchestration.run_error import clean

    said = " ".join(clean(str(text or "")).split())
    return said[:_REASON_CHARS].rstrip(" .")


def why_unavailable(name: str, server_module: Any, *, error: Any = None, where: str = "this step") -> Unavailable:
    """Why `name` isn't available, as its app's record says, and what to do: for a Tool step (`where` "this step")
    or an agent ("this agent"). `error` is a call's own failure, when one said it."""
    app = app_of(name)
    status, reason = app_state(server_module, app)
    said = _said(error or reason)
    because = f": {said}" if said else ""
    other = f"pick another tool for {where}"
    if app and (status == "reauth_needed" or signed_out(error) or (status != "connected" and signed_out(reason))):
        return Unavailable(name, app, f"its app {app} needs signing in again{because}",
                           f"Reconnect {app} under Apps, then Resume the run, or {other}.")
    if app and status == "disabled":
        return Unavailable(name, app, f"its app {app} is turned off",
                           f"Turn {app} on under Apps, then Resume the run, or {other}.")
    if app and (status or reason):
        return Unavailable(name, app, f"its app {app} isn't connected{because}",
                           f"Reconnect {app} under Apps, then Resume the run, or {other}.")
    if app:
        return Unavailable(name, app, f"no app called {app} is here any more",
                           f"{other[0].upper()}{other[1:]}, then Resume the run.")
    return Unavailable(name, None, "no app or tool here provides it any more",
                       f"{other[0].upper()}{other[1:]}, then Resume the run.")


def is_live(name: str, server_module: Any, custom_tools: list[dict] | None = None) -> bool:
    """Whether something here can answer `name` right now: an app's session that is open, or a tool of the
    workspace's own."""
    from core.tool_router import ToolRouter

    router = getattr(server_module, "tool_router", None)
    if not isinstance(router, ToolRouter):
        router = ToolRouter(router or {})
    key = router.resolve(name)
    if key is not None:
        session_key, _ = router[key]
        return session_key in (getattr(server_module, "agent_sessions", None) or {})
    return any(isinstance(t, dict) and t.get("name") == name for t in custom_tools or [])


def unavailable_for(agent: dict, offered: Any, server_module: Any) -> list[Unavailable]:
    """The tools an agent names that this turn does not offer and an app should: each with why (F1349). Never a
    product server's tool, `all`, a group, or a name that is not an app's (those are not this reading's)."""
    offered = set(offered or ())
    out: list[Unavailable] = []
    for name in agent.get("tools") or []:
        app = app_of(name)
        if app and not is_native(app) and name not in offered and not name.startswith("group:"):
            out.append(why_unavailable(name, server_module, where="this agent"))
    return out


def agent_note(missing: list[Unavailable]) -> str | None:
    """The line an agent reads while one of its tools is unavailable."""
    if not missing:
        return None
    return ("Unavailable now: " + "; ".join(m.note() for m in missing) + ". Do what you can without "
            + ("it" if len(missing) == 1 else "them") + ", and say in your answer which tool was unavailable and why.")

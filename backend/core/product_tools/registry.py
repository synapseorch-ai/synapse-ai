"""The product tools, and the one door every call goes through (MCP2, D292).

An entry says what it is; `invoke` enforces it. Nothing an adapter or a prompt says can widen it:

* **Surface.** An `app_only` entry is offered only to the assistant, which is in the app. Acts above builder must
  be `app_only` (Naveen, D292): members, billing, keys and settings never leave the app, whatever the caller's role.
* **Scope.** A connection's granted scopes (read / run / build, D127) narrow what its role allows.
* **Role.** The caller's live role must reach the entry's `min_role`. It is checked on every call, a confirmed
  one included, so a demotion takes effect on the next call.
* **Confirmation.** An act that needs it (`confirm.py`) answers first with a preview and a token, and is done only
  when the same call comes back with that token. Under `approved_review` (Synapse AI's apply and save, SAI2-P1),
  the person's approved review stands in for the token of each call it covers (`approved.py`), and only those.
* **Audit.** Every act that succeeded is told to the host once (`hooks.emit`); a read never is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from core.product_tools import confirm, hooks
from core.product_tools import caller as _caller_mod
from core.product_tools import page as paging
from core.product_tools.caller import Caller
from core.product_tools.errors import AlreadySpent, ToolRefusal
from core.product_tools.roles import Role

__all__ = ["ProductTool", "ToolRefusal", "register", "unregister", "get", "all_tools", "for_caller", "invoke"]

Handler = Callable[[Caller, dict[str, Any]], Awaitable[Any]]
Preview = Callable[[Caller, dict[str, Any]], Awaitable[Any]]
Target = Callable[[dict[str, Any], Any], "tuple[str, str] | None"]
ConfirmPolicy = Literal["irreversible", "every_act", "approved_review"]

KINDS = frozenset({"read", "act"})
SCOPES = frozenset({"read", "run", "build"})
#: MCP clients refuse other names (Claude: ^[a-zA-Z0-9_-]{1,64}$); ours are lower snake case.
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: A name's words a title spells as their owners do, or says whole (F1546).
_TITLE_WORDS = {"http": "HTTP", "python": "Python", "openapi": "OpenAPI", "db": "database"}


def title_of(name: str) -> str:
    """A tool's name said as words, for its title (F1546): `create_http_tool` is "Create HTTP tool"."""
    words = [_TITLE_WORDS.get(w, w) for w in name.split("_") if w]
    if not words:
        return name
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


@dataclass(frozen=True)
class ProductTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    kind: str
    #: What the entry is about (`runs`, `workflows`, `approvals`, …); `<area>.<name>` is its default audit action.
    area: str
    min_role: Role = Role.VIEWER
    scope: str = "read"
    #: It cannot be undone (a delete). Marked destructive; MCP confirms it with a token.
    irreversible: bool = False
    #: It replaces or stops what is there (an overwrite, a cancel) without being a delete: marked destructive, so a
    #: client asks its person, but no token.
    destructive: bool = False
    #: Its person sees it before it is done, though it can be undone: previewed, then done with a token, as an
    #: irreversible act is (F1198: a Python tool, code that will run with the internet).
    confirmed: bool = False
    #: Calling it twice with the same arguments does no more than once.
    idempotent: bool = False
    #: It reaches outside the workspace (the web, a third party's API).
    open_world: bool = False
    #: Its answer is a list: it takes `cursor` and `limit`, and answers through `page.page`.
    paginated: bool = False
    #: Offered only in the app (the assistant), never over MCP or the API.
    app_only: bool = False
    #: What the act would do, shown before it is confirmed. None shows the call's own arguments.
    preview: Preview | None = None
    #: The host's audit action, e.g. `run.started`. None is `<area>.<name>`.
    audit_action: str | None = None
    #: (target_type, target_id) for the audit, from the arguments and the result.
    target: Target | None = None
    #: The handler's own code already writes the host's audit row (cloud's run doors do): the registry does not
    #: write a second.
    self_audited: bool = False
    #: Whether it is listed right now (a feature switched off is not). None is always. A listing hint, not a gate:
    #: a call by name still reaches the handler, which refuses with the feature's own words ("Knowledge is
    #: switched off in this workspace…") rather than "Unknown tool".
    offered: Callable[[], bool] | None = field(default=None, compare=False)
    #: What a person reads for it: Claude's Connectors Directory, a client's permission prompt (F1546). Empty is its
    #: name said as words (`title_of`), so no entry is ever without one.
    title: str = ""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} must be lower snake case, at most 64 characters")
        object.__setattr__(self, "title", self.title.strip() or title_of(self.name))
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind must be one of {sorted(KINDS)}")
        if self.scope not in SCOPES:
            raise ValueError(f"{self.name}: scope must be one of {sorted(SCOPES)}")
        object.__setattr__(self, "min_role", Role.parse(self.min_role))
        if self.kind == "read":
            if self.scope != "read":
                raise ValueError(f"{self.name}: a read needs the read scope, nothing more")
            if self.irreversible or self.destructive or self.confirmed:
                raise ValueError(f"{self.name}: a read cannot be irreversible, destructive or confirmed")
        if self.kind == "act" and self.min_role > Role.BUILDER and not self.app_only:
            raise ValueError(
                f"{self.name}: an act needing more than builder must be app_only (acts off the app are capped at "
                "builder, D292)"
            )

    @property
    def action(self) -> str:
        return self.audit_action or f"{self.area}.{self.name}"

    def annotations(self) -> dict[str, bool]:
        """MCP's tool annotations, derived, never set by hand, in the one spelling of tool tags
        (`core.tool_tags`, TAG1). `explicit`: a read states `destructiveHint: False` too, since a client
        treats a tool as destructive unless told (D125)."""
        from core.tool_tags import hints_of

        read = self.kind == "read"
        return hints_of({
            "read_only": read,
            "destructive": self.irreversible or self.destructive,
            "idempotent": read or self.idempotent,
            "open_world": self.open_world,
        }, explicit=True)

    def schema(self, *, confirm_token: bool = False) -> dict[str, Any]:
        """The input schema an adapter offers: the entry's own, plus paging and, where asked, the token."""
        schema = dict(self.input_schema)
        props = dict(schema.get("properties") or {})
        if self.paginated:
            props.update(paging.PROPERTIES)
        if confirm_token:
            props[confirm.TOKEN_ARG] = {
                "type": "string",
                "description": "Leave out at first: the answer previews what this would do and gives a token. "
                               "Call again with the same arguments and that token to do it.",
            }
        schema["type"] = schema.get("type", "object")
        schema["properties"] = props
        return schema

    def offered_to(self, caller: Caller) -> bool:
        if self.app_only and caller.via != "assistant":
            return False
        if self.offered is not None and not self.offered():
            return False
        return caller.granted(self.scope) and caller.role.can_act_as(self.min_role)

    def needs_token(self, policy: ConfirmPolicy) -> bool:
        # Under `approved_review` an act needs one unless the review covers the call, which `invoke` decides.
        return self.kind == "act" and (policy in ("every_act", "approved_review") or self.irreversible
                                       or self.confirmed)


_TOOLS: dict[str, ProductTool] = {}


def register(tool: ProductTool, *, replace: bool = False) -> ProductTool:
    if tool.name in _TOOLS and not replace:
        raise ValueError(f"a product tool named {tool.name!r} is already registered")
    _TOOLS[tool.name] = tool
    return tool


def unregister(name: str) -> None:
    _TOOLS.pop(name, None)


def get(name: str) -> ProductTool | None:
    return _TOOLS.get(name)


def all_tools() -> list[ProductTool]:
    return list(_TOOLS.values())


def for_caller(caller: Caller) -> list[ProductTool]:
    """The entries this caller may call, in registration order."""
    return [t for t in _TOOLS.values() if t.offered_to(caller)]


_SECRET_KEY = re.compile(r"secret|token|password|passwd|api_?key|authori[sz]ation|credential|cookie", re.I)
_TEXT_CHARS = 200


def _summary(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if depth >= 2:
            return f"{{{len(value)} keys}}"
        return {k: "[withheld]" if _SECRET_KEY.search(str(k)) else _summary(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if depth >= 2:
            return f"[{len(value)} items]"
        return [_summary(v, depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > _TEXT_CHARS:
        return value[:_TEXT_CHARS] + "…"
    return value


async def invoke(name: str, caller: Caller, args: dict[str, Any] | None = None, *,
                 confirm_policy: ConfirmPolicy = "irreversible", review: Any = None, reviewed: Any = None,
                 made: dict[str, str] | None = None) -> Any:
    """Call `name` as `caller`: every rule above, then the handler. Raises `ToolRefusal` for anything refused.

    `review` and `reviewed`, with `confirm_policy="approved_review"`: the person's approved review and the object it
    approved as state holds it now, both read by the host's code, never from a model's arguments; `made`, the ids a
    build's save has made, each to the placeholder its approval covered it with (`approved.as_approved`)."""
    args = dict(args or {})
    tool = _TOOLS.get(name)
    if tool is None or (tool.app_only and caller.via != "assistant"):
        raise ToolRefusal(f"Unknown tool: {name}")
    if not caller.granted(tool.scope):
        raise ToolRefusal(
            f"This connection was not granted '{tool.scope}', which {name} needs. "
            f"Reconnect and allow '{tool.scope}' to use it."
        )
    if not caller.role.can_act_as(tool.min_role):
        raise ToolRefusal(
            f"{name} needs the {tool.min_role.label} role; you are a {caller.role.label} in this workspace."
        )

    token = args.pop(confirm.TOKEN_ARG, None)
    policy, by_review, confirmed_by = confirm_policy, False, None
    if confirm_policy == "approved_review":
        # The person's approved review is the confirmation of each call it covers; anything else asks as the
        # assistant always does, with a token per call.
        from core.product_tools import approved

        by_review = tool.kind == "act" and approved.uncovered(review, caller, name, args, reviewed, made=made) is None
        policy = "every_act"
    if not by_review and tool.needs_token(policy):
        if not token:
            preview = await tool.preview(caller, args) if tool.preview is not None else {"tool": name, "arguments": args}
            return {
                "status": "needs_confirmation",
                "preview": preview,
                "confirm_token": confirm.mint(caller, name, args),
                "expires_in_seconds": confirm.TTL_SECONDS,
                "next": f"Nothing has been done yet. Call {name} again with the same arguments and this "
                        "confirm_token to do it.",
            }
        await confirm.redeem(token, caller, name, args)
        confirmed_by = "token"
    if by_review:
        confirmed_by = f"review:{review.digest[:16]}"
        # Spent once, as a token is (MCP2's review): an apply run twice for one approval makes nothing twice.
        if not await confirm.claim_once(f"review:{review.id or review.digest}:{name}:{confirm.args_hash(args)}",
                                        confirm.REVIEW_TTL_SECONDS):
            raise AlreadySpent(f"{name}: the approved change already made this call.")

    # Bound for the handler and for the audit after it: a host's audit reads who acted from
    # `caller.current()` (cloud's principal rides on the caller).
    token_var = _caller_mod._CURRENT.set(caller)
    try:
        result = await tool.handler(caller, args)

        if tool.kind == "act" and not tool.self_audited:
            target = tool.target(args, result) if tool.target is not None else None
            await hooks.emit(hooks.AuditEvent(
                action=tool.action, tool=name, area=tool.area, via=caller.via, user_id=caller.user_id,
                credential_id=caller.credential_id, role=caller.role.label,
                target_type=target[0] if target else None, target_id=target[1] if target else None,
                args=_summary(args), confirmed_by=confirmed_by,
            ))
    finally:
        _caller_mod._CURRENT.reset(token_var)
    return result

"""Who is calling a product tool, and through what (MCP2, D292).

Built by the host, never by a model: the MCP server from the connection's credential (an OAuth token is the person
and their live role; an API key is the workspace's, at builder), the assistant from the run its person started.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

from core.product_tools.roles import Role

#: The surfaces a call comes through. `assistant` is in the app; the others are not. `workflow` is a person's own
#: workflow or agent run, acting for the person the host says it is for (F1319).
VIAS = frozenset({"mcp", "assistant", "api", "workflow"})


@dataclass(frozen=True)
class Caller:
    role: Role
    via: str
    #: The person, when there is one (an OAuth connection, the assistant). None for an API key.
    user_id: str | None = None
    #: What the host counts quotas against: the key or the grant.
    credential_id: str | None = None
    #: The scopes this connection was granted; None grants every scope (a key, the assistant).
    scopes: frozenset[str] | None = None
    #: The host's own record of the credential (the key's row, cloud's principal), for a handler that must name
    #: it to the host (Knowledge's trace records the key). Never shown to a model.
    principal: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.via not in VIAS:
            raise ValueError(f"unknown surface {self.via!r}")
        object.__setattr__(self, "role", Role.parse(self.role))

    def granted(self, scope: str) -> bool:
        return self.scopes is None or scope in self.scopes

    @property
    def who(self) -> str:
        """Whom a confirmation token is bound to: the person, else the credential, else this install's one person."""
        if self.user_id:
            return f"user:{self.user_id}"
        if self.credential_id:
            return f"credential:{self.credential_id}"
        return "local"


_CURRENT: contextvars.ContextVar["Caller | None"] = contextvars.ContextVar("product_tools_caller", default=None)


def current() -> "Caller | None":
    """The caller of the product tool whose handler is running here, or None outside one. For a host's code deep
    under a handler that must name who acted and through what (cloud's delete observer writes its audit row with
    the person and the surface rather than a second row)."""
    return _CURRENT.get()

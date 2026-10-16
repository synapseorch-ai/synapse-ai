"""What a host plugs into the product tools (MCP2, D292), the way `builder_readers.set_*` does.

* `set_audit(fn)`: told about every act a product tool does, once, after it succeeded. Cloud writes its audit
  log with `metadata.via`; the self-hosted product has none, so the default does nothing. An audit that fails is
  logged and never undoes or fails the act: the act has happened.
* `set_role_source(fn)`: a person's current role in the bound workspace, for a host that builds a caller from a
  user id (the assistant's run). None means the host has no roles: the self-hosted product's one person is the
  owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.log import get_logger
from core.product_tools.roles import Role

_log = get_logger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    #: The host's action name, e.g. `run.started`, or `<area>.<tool>` when the entry names none.
    action: str
    tool: str
    area: str
    via: str
    user_id: str | None
    credential_id: str | None
    role: str
    target_type: str | None = None
    target_id: str | None = None
    #: The call's arguments, summarised: secrets withheld, long text cut.
    args: dict[str, Any] = field(default_factory=dict)


Audit = Callable[[AuditEvent], Awaitable[None]]
RoleSource = Callable[[str], Awaitable["Role | None"]]

_audit: Audit | None = None
_role_source: RoleSource | None = None


def set_audit(fn: Audit | None) -> None:
    global _audit
    _audit = fn


async def emit(event: AuditEvent) -> None:
    if _audit is None:
        return
    try:
        await _audit(event)
    except Exception as exc:  # the act is done; a lost audit row is logged, not raised
        _log.warning(f"product tool audit failed for {event.tool}: {exc!r}")


def set_role_source(fn: RoleSource | None) -> None:
    global _role_source
    _role_source = fn


async def role_of(user_id: str) -> Role:
    """`user_id`'s role in the bound workspace. Without a host source, the owner (the self-hosted product)."""
    if _role_source is None:
        return Role.OWNER
    role = await _role_source(user_id)
    if role is None:
        raise LookupError("not a member of this workspace")
    return Role.parse(role)

"""An act done only on a second, confirmed call (MCP2, D292; ASSIST1's card, D291).

The first call answers with what the act would do and a token; the act happens when the same call comes back
with that token. A code guard, never prompt text. The adapter decides which acts need one: MCP asks only for
irreversible ones (the second call is marked destructive, so the client asks its person); the assistant asks for
every act, and the person's click in the card redeems it from their own session.

A token is an HMAC over (tool, who, the arguments' hash, expiry, nonce), good for `TTL_SECONDS`, and used once:
its nonce is claimed in a store (in memory here; cloud's workers share Redis, `set_claim_store`). It carries no
role: `registry.invoke` checks the redeeming caller's role again, so a person demoted between the preview and the
click cannot finish the act.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Protocol

from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal

TTL_SECONDS = 600
TOKEN_ARG = "confirm_token"


class ClaimStore(Protocol):
    async def claim(self, key: str, ttl: int) -> bool:
        """True the first time `key` is claimed within `ttl` seconds, False after."""


class MemoryClaims:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    async def claim(self, key: str, ttl: int) -> bool:
        now = _now()
        for k in [k for k, until in self._seen.items() if until < now]:
            del self._seen[k]
        if key in self._seen:
            return False
        self._seen[key] = now + ttl
        return True


_store: ClaimStore = MemoryClaims()
_secret: bytes | None = None


def set_claim_store(store: ClaimStore | None) -> None:
    global _store
    _store = store if store is not None else MemoryClaims()


def set_secret(secret: bytes | None) -> None:
    """The signing key. Hosts with several processes set a shared one; otherwise one per process."""
    global _secret
    _secret = secret


def _key() -> bytes:
    global _secret
    if _secret is None:
        env = os.environ.get("SYNAPSE_CONFIRM_SECRET")
        _secret = env.encode() if env else secrets.token_bytes(32)
    return _secret


def _now() -> float:
    return time.time()


def args_hash(args: dict[str, Any]) -> str:
    body = {k: v for k, v in args.items() if k != TOKEN_ARG}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def mint(caller: Caller, tool: str, args: dict[str, Any]) -> str:
    payload = {"t": tool, "w": caller.who, "h": args_hash(args), "e": int(_now()) + TTL_SECONDS,
               "n": secrets.token_urlsafe(12)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


async def redeem(token: Any, caller: Caller, tool: str, args: dict[str, Any]) -> None:
    """Raise `ToolRefusal` unless `token` confirms exactly this call by this caller, for the first time."""
    try:
        body, sig = str(token).rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            raise ValueError
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise ToolRefusal("That confirm_token is not valid. Call the tool again without it to get a new one.") from None
    if payload.get("t") != tool or payload.get("h") != args_hash(args):
        raise ToolRefusal(
            "That confirm_token was given for a different call. Confirm with the same tool and the same arguments "
            "the preview was for, or call again without a token for a new preview."
        )
    if payload.get("w") != caller.who:
        raise ToolRefusal("That confirm_token was given to someone else.")
    if int(payload.get("e", 0)) < _now():
        raise ToolRefusal("That confirm_token has expired. Call the tool again without it for a new preview.")
    if not await _store.claim(f"confirm:{payload['n']}", TTL_SECONDS):
        raise ToolRefusal("That confirm_token was already used: the act was done once already.")

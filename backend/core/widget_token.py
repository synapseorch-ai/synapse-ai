"""
The visitor token an embedded widget talks with.

A widget is an agent answering on somebody else's website, to somebody who is
not a member of anything. The chat routes the workspace uses all authenticate a
*person* — a session, or an API key that stands in for one — and there is no
person here. So the widget gets its own credential, and the shape of it is the
whole security argument:

* **The public key in the page is an identifier, not a credential.** It says
  which widget; it grants nothing. It is printed in HTML on a public site, so
  treating it as a secret would be a lie we then had to maintain. What
  authorises the conversation is *where the page is* — the parent origin,
  checked against the widget's allow-list.
* **This token is minted server-side, once, when the embed page renders**, and
  only after that origin check has passed. The browser never mints one and
  never sees the signing key.
* **The session id is minted here too, and travels inside the signature.** It
  is not a parameter. A conversation id the client could choose is a conversation
  id the client could *guess*, and every transcript is one store lookup away.

Structured rather than opaque, modelled on `api/gateway/token.py` in the cloud
repo: there is no server-side row to look up, so the claims *are* the state and
the HMAC is what makes them unforgeable. That also means there is **no
revocation** — the TTL is the only bound, so keep it short. Disabling a widget
stops the next mint, not the current conversation.

Format, mirroring the gateway's `mgt1.`:

    wgt1.<base64url(claims json)>.<hex hmac-sha256>

Malformed, tampered and expired all return `None`. A caller that distinguished
them would be an oracle for someone probing with a scraped key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass

#: Long enough that an ordinary conversation never trips it, short enough that a
#: leaked token is worth little. A widget that outlives it re-mints through
#: `refresh`, which re-checks nothing but the signature — the origin was checked
#: when the page rendered and cannot change under the same iframe.
DEFAULT_TTL_SECONDS = 3600

#: How long past `exp` a token may still be traded for a fresh one. A person who
#: leaves a tab open over lunch comes back to a working widget; one who leaves it
#: open for a week does not.
REFRESH_GRACE_SECONDS = 86_400

_PREFIX = "wgt1"


@dataclass(frozen=True)
class VisitorClaims:
    """Who is talking, to which agent, in which conversation, from where."""
    widget_id: str
    agent_id: str
    session_id: str
    origin: str
    issued_at: int
    expires_at: int


def new_session_id() -> str:
    """A conversation id the visitor cannot choose.

    Minted here rather than accepted from the page, because the store keys
    transcripts on it: a client-supplied id is a client-supplied *lookup*.
    """
    return f"wgtc_{uuid.uuid4().hex}"


def new_public_key() -> str:
    """The `pk_` identifier that goes in the customer's HTML.

    Random despite not being a secret. It is world-readable by design, but it
    must not be *enumerable* — a sequential id would let anyone walk the list of
    widgets and learn which sites run one.
    """
    return f"pk_{secrets.token_urlsafe(24)}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint(
    widget_id: str,
    agent_id: str,
    *,
    origin: str,
    secret: str,
    session_id: str | None = None,
    ttl: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, str]:
    """`(token, session_id)`. Pass `session_id` to continue a conversation."""
    issued = int(time.time()) if now is None else now
    session = session_id or new_session_id()
    claims = {
        "w": widget_id,
        "a": agent_id,
        "s": session,
        "o": origin,
        "iat": issued,
        "exp": issued + ttl,
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    payload = f"{_PREFIX}.{body}"
    return f"{payload}.{_sign(payload, secret)}", session


def verify(
    raw: str,
    *,
    secret: str,
    now: int | None = None,
    grace: int = 0,
) -> VisitorClaims | None:
    """The claims, or `None`. `grace` allows a just-expired token through — used
    only by `refresh`, never by a route that acts on the token."""
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    prefix, body, signature = parts
    if not hmac.compare_digest(_sign(f"{prefix}.{body}", secret), signature):
        return None
    try:
        claims = json.loads(_unb64(body))
        expires = int(claims["exp"])
        result = VisitorClaims(
            widget_id=str(claims["w"]),
            agent_id=str(claims["a"]),
            session_id=str(claims["s"]),
            origin=str(claims["o"]),
            issued_at=int(claims["iat"]),
            expires_at=expires,
        )
    except (ValueError, KeyError, TypeError):
        return None

    current = int(time.time()) if now is None else now
    if current > expires + grace:
        return None
    return result


def refresh(raw: str, *, secret: str, ttl: int = DEFAULT_TTL_SECONDS) -> str | None:
    """A fresh token for the same conversation, or `None`.

    Deliberately re-checks nothing but the signature and the refresh window. The
    origin was verified when the embed page rendered, and the iframe it rendered
    into cannot change origin without a fresh page load — which mints a new token
    through the checked path anyway.
    """
    claims = verify(raw, secret=secret, grace=REFRESH_GRACE_SECONDS)
    if claims is None:
        return None
    token, _ = mint(
        claims.widget_id,
        claims.agent_id,
        origin=claims.origin,
        secret=secret,
        session_id=claims.session_id,
        ttl=ttl,
    )
    return token

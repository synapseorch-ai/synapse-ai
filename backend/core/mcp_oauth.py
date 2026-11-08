"""Refreshing a remote MCP server's OAuth access token.

An access token is short-lived by design — an hour is typical — and until this
existed nothing renewed one. A server authorised in the morning stopped working
by lunchtime, and the failure was invisible in the worst way: the server answers
`401`, the MCP client's task group cancels its host, and what surfaced was a
cancelled job rather than "your Vercel token expired".

`routes/mcp.py` in the cloud has documented a `reauth_needed` status since it was
written. Nothing could ever set it, because nothing could try a refresh.

## Why it lives in the engine

Both products need it. The self-hosted server holds the same tokens in its own
store, and a refresh implemented in the cloud router only would leave every
self-hosted install with the same silent expiry.

## What it needs, and why that was the hard part

A refresh takes a refresh token, the token endpoint and the client credentials.
The cloud held all three **only in the single-use Redis state** that carries an
authorization through its callback, which expires ten minutes after the person
clicks Approve — so a refresh was not merely unimplemented, it was impossible.
They are persisted onto the server's definition now, the secret ones as
`synsec://` references that resolve on the way to the worker.

## Persisting the new token

The engine cannot write cloud's `org_secrets` and must not know its naming, so
the caller registers a persister. Nothing registered is a supported state: the
refresh still works for the life of the process, and is simply asked for again
next time. That keeps this module useful in a test and in a CLI.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

#: Set by the embedder. `(server_name, tokens) -> awaitable`, where `tokens` has
#: `access_token` and optionally `refresh_token`.
_persister: Optional[Callable[..., Any]] = None


def set_token_persister(fn: Optional[Callable[..., Any]]) -> None:
    """Install the callback that stores a freshly minted token. None to clear."""
    global _persister
    _persister = fn


def get_token_persister() -> Optional[Callable[..., Any]]:
    return _persister


def can_refresh(cfg: dict) -> bool:
    """True when this server's config carries everything a refresh needs.

    Checked before the attempt rather than discovered inside it, so a server
    that simply has no refresh token — a bearer PAT, or one authorised before
    these fields were persisted — is reported as needing re-authorisation
    instead of as a refresh that failed.
    """
    return bool(
        cfg.get("refresh_token") and cfg.get("token_endpoint") and cfg.get("client_id")
    )


async def refresh_access_token(cfg: dict) -> dict | None:
    """Exchange the refresh token for a new access token, or None.

    Returns the token response, so a server that rotates its refresh token — the
    OAuth 2.1 recommendation, and what several MCP providers do — has the new one
    persisted alongside the access token. Dropping it would make the *next*
    refresh fail with a token that had already been spent.

    The endpoint is the one recorded when the person authorised this server, so
    it has already been through the egress guard on a path that could reject it.
    Refreshing does not re-derive it from the server's metadata, deliberately: a
    server that later advertises a different token endpoint would otherwise be
    handed this org's refresh token on its say-so.
    """
    import httpx

    if not can_refresh(cfg):
        return None

    form = {
        "grant_type": "refresh_token",
        "refresh_token": str(cfg["refresh_token"]),
        "client_id": str(cfg["client_id"]),
    }
    if cfg.get("client_secret"):
        form["client_secret"] = str(cfg["client_secret"])

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                str(cfg["token_endpoint"]),
                data=form,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        print(f"[mcp_oauth] refresh failed for '{cfg.get('name')}': {exc}", flush=True)
        return None

    if response.status_code != 200:
        # A refresh token can be revoked, expired or already spent. All three
        # mean the same thing to the person: authorise the server again.
        #
        # The body is logged because the status code alone cannot tell those
        # apart from the case that is *not* the person's fault. OAuth answers
        # both with `400`, and the two want opposite responses: `invalid_grant`
        # is "authorise it again", while `invalid_client` means this
        # deployment's client registration is wrong and re-authorising will
        # produce the same 400 forever. Measured against a real provider, where
        # `(400)` on its own sent the diagnosis down the wrong path.
        detail = " ".join(response.text.split())[:200]
        print(
            f"[mcp_oauth] refresh rejected for '{cfg.get('name')}' "
            f"({response.status_code}) — re-authorisation needed: {detail}",
            flush=True,
        )
        return None

    try:
        tokens = response.json()
    except ValueError:
        return None

    if not tokens.get("access_token"):
        return None
    return dict(tokens)


async def refresh_and_persist(cfg: dict) -> str | None:
    """Refresh, hand the result to the persister, and return the access token."""
    tokens = await refresh_access_token(cfg)
    if tokens is None:
        return None

    persist = get_token_persister()
    if persist is not None:
        try:
            await persist(cfg.get("name", ""), tokens)
        except Exception as exc:
            # A token that works but was not written down is still worth using
            # for this job. The next one asks again, which is wasteful and not
            # wrong — and far better than failing a turn that could have run.
            print(
                f"[mcp_oauth] could not persist refreshed token for "
                f"'{cfg.get('name')}': {exc}",
                flush=True,
            )

    return str(tokens["access_token"])

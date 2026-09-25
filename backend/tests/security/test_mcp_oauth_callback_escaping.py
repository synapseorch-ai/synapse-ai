"""The MCP OAuth callback page must not run what its query string says (S7).

`GET /api/mcp/oauth/callback` is unauthenticated by design — an OAuth
provider redirects a browser to it — and it answered with an HTML page that
put `?error=` into the markup raw, and into an inline script through
`json.dumps`, which does not escape `</script>`.

Reproduced on a scratch install (2026-09-25, D128), in a real browser, on both
origins:

  * the UI origin, through Next's `/api/:path*` rewrite: with login off the
    injected script read `/api/settings`; with login on, a signed-in victim
    who opened the link had an API key minted in their name, and the raw key
    was in the script's hands. A victim who is not signed in is sent to
    `/login?redirect=<the link>` and lands on it after signing in;
  * the backend origin: `</script><script>…` broke out of the inline script.

The success page put the server's name in the same way; it is fixed with it.
"""
from __future__ import annotations

import pytest

import core.mcp_oauth_state as oauth_state
from core.routes.tools import mcp_oauth_callback

PAYLOADS = [
    "<img src=x onerror=alert(document.domain)>",
    "</script><script>alert(2)</script>",
    "\"><svg onload=alert(3)>",
]


def _body(response) -> str:
    return response.body.decode()


@pytest.mark.parametrize("payload", PAYLOADS)
async def test_an_error_parameter_is_text_not_markup(payload):
    response = await mcp_oauth_callback(code=None, state=None, error=payload)
    body = _body(response)

    assert response.status_code == 400
    assert "<img" not in body and "<svg" not in body
    # Exactly one script element: the page's own.
    assert body.count("<script") == 1
    assert body.count("</script>") == 1
    # Still shown to the person, as text.
    assert "OAuth failed: &lt;" in body or "OAuth failed: &quot;&gt;" in body


async def test_the_error_still_reaches_the_opener_intact():
    """The opener gets the provider's text back — escaped for the script, not changed."""
    response = await mcp_oauth_callback(code=None, state=None, error="access_denied")
    assert 'error:"access_denied"' in _body(response)


@pytest.mark.parametrize("payload", PAYLOADS)
async def test_a_server_name_is_text_not_markup(payload):
    state = "s7-test-state"
    oauth_state.register(state, payload)
    try:
        response = await mcp_oauth_callback(code="c", state=state, error=None)
    finally:
        oauth_state.pop(state)
    body = _body(response)

    assert response.status_code == 200
    assert "<img" not in body and "<svg" not in body
    assert body.count("<script") == 1
    assert body.count("</script>") == 1
    assert "Connected to &lt;" in body or "Connected to &quot;&gt;" in body

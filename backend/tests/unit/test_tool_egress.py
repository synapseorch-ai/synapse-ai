"""A custom tool's URL, and the scraper's, are checked when they are fetched.

Both reach the network with a destination someone else chose: a tool document
written through the Tools screen, or a URL an agent was persuaded to scrape.
Neither was checked at all before 1.9.2 — `core/egress_guard.py` did not exist —
so `http://169.254.169.254/latest/meta-data/` was a reachable address from
inside a workflow, and on a hosted worker that is a credential.

Checking when the tool is *saved* cannot cover it: DNS can be re-pointed
afterwards, and the Tools screen writes a raw document with no check at all. The
call is the only moment that holds.
"""

import pytest

from core import egress_guard


class TestTheToolCallPosture:
    """`check_tool_url` refuses a destination and tolerates a non-destination."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8000/admin",
        "http://localhost/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[fd00::1]/internal",
    ])
    async def test_an_inward_url_is_refused(self, url: str) -> None:
        with pytest.raises(egress_guard.BlockedEgress):
            await egress_guard.check_tool_url(url)

    async def test_a_non_http_scheme_is_refused(self) -> None:
        with pytest.raises(egress_guard.BlockedEgress):
            await egress_guard.check_tool_url("file:///etc/passwd")

    async def test_a_public_url_is_allowed(self) -> None:
        await egress_guard.check_tool_url("https://example.com/api")

    async def test_a_host_that_does_not_resolve_is_allowed_through(self) -> None:
        # An internal API, a service not yet deployed, a split-horizon DNS.
        # Refusing it would be a guess about someone else's network, and httpx
        # fails the identical lookup a moment later if it really is absent.
        await egress_guard.check_tool_url("https://nothing-here.invalid/api")

    async def test_an_unrendered_template_is_allowed_through(self) -> None:
        # Almost always a placeholder the tool never filled in. httpx cannot
        # send it anywhere, and its own error names the missing value.
        await egress_guard.check_tool_url("{base_url}/things")

    async def test_the_scraper_still_fails_closed_on_both(self) -> None:
        # The tolerance above is the *tool* posture. The scraper is handed URLs
        # a model invented, so "cannot evaluate" must stay a refusal for it.
        with pytest.raises(egress_guard.UnresolvedHost):
            await egress_guard.check_url("https://nothing-here.invalid/api")
        with pytest.raises(egress_guard.MalformedUrl):
            await egress_guard.check_url("{base_url}/things")


class TestTheScrapersPreflight:
    async def test_every_handler_refuses_an_inward_url(self) -> None:
        from tools import web_scraper

        blocked = await web_scraper._preflight("http://169.254.169.254/latest/meta-data/")
        assert blocked is not None and "blocked" in blocked.lower()
        assert await web_scraper._preflight("https://example.com") is None

    async def test_the_browser_layer_aborts_what_the_preflight_cannot_see(self) -> None:
        """A redirect hop, a `js_code` navigation and an in-page fetch all happen
        inside Chromium, where the pre-flight never sees them. `_guard_route` is
        the deny that does, and it must fail closed on anything it cannot judge."""
        from tools import web_scraper

        aborted: list[str] = []

        class _Route:
            def __init__(self, url: str) -> None:
                self.request = type("R", (), {"url": url})()

            async def continue_(self) -> None:
                aborted.append("CONTINUED")

            async def abort(self, reason: str) -> None:
                aborted.append(reason)

        await web_scraper._guard_route(_Route("http://169.254.169.254/latest/meta-data/"))
        assert aborted == ["blockedbyclient"]

        aborted.clear()
        await web_scraper._guard_route(_Route("https://example.com/"))
        assert aborted == ["CONTINUED"]

        # Not a URL it can judge: abort, never continue.
        aborted.clear()
        await web_scraper._guard_route(_Route("data:text/html,hi"))
        assert aborted == ["blockedbyclient"]

"""
An API that needs the person's own sign-in says why no tool is made, and offers a browser agent where there are
browser agents (F1427, BLD1; Naveen's scratch, 11-06, run builder_77a81e4bb602).

For posting on X the research said OAuth user context; `_signs_in_only` rightly made no tool from its spec (F1121), but
the card said only "Twitter (X) has an API, but I can't make a tool for it yet. Add an MCP server…", and the browser
came up only when Naveen asked for it ("use browser i can login a session"). Now the line says why: the API needs you
signed in to your own account, which a tool can't hold. The MCP server comes first, as the way that works, with "build
without it". With browser agents on (`browser_automation_enabled`, the switch the server registers the browser on) a
browser agent with the person's signed-in session is offered after it, honestly: only if the site allows automated
browsers, since some turn them away (REACH1, 11-06: x.com answers 403 to any, and the browser never hides that it is
one). Any other API with no tool keeps its line as it was.
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_env import use_saved_settings
from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from test_an_api_with_no_tool_is_said_once import LINE, TELEGRAM, TELEGRAM_LEAD, WRITE, _reviewed
from test_the_build_route_decides_in_code import workspace  # noqa: F401

POST = {"id": "post", "type": "tool", "name": "Post Tweet", "does": "Posts the tweet on X",
        "tool": "twitter_x_post_tweet_x"}
# The research's own words in Naveen's run (event 110), spec and all.
X = {"provider": "Twitter (X)", "how": "api",
     "detail": "Posts a tweet via Twitter API v2 — requires user OAuth so the tweet appears on the authenticated "
               "user's account.",
     "needs": "A Twitter Developer account, an app with OAuth 2.0 (Authorization Code with PKCE) or OAuth 1.0a User "
              "Context enabled, and a bearer/access token with write permissions.",
     "sources": ["https://developer.twitter.com/en/docs/twitter-api/tweets/manage-tweets/api-reference/post-tweets"],
     "own_domains": ["twitter.com"],
     "api": {"base_url": "https://api.twitter.com/2",
             "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
             "endpoints": [{"method": "POST", "path": "/tweets", "does": "Posts a new tweet with the given text",
                            "tool": "twitter_x_post_tweet_x"}],
             "docs_url": "https://developer.twitter.com/en/docs/twitter-api/tweets/manage-tweets/api-reference/"
                         "post-tweets"}}
WHY = ("I can't build `twitter_x_post_tweet_x` here yet: Twitter (X) has an API, but it needs you signed in to your "
       "own account, which a tool can't hold.")
STILL = "Add an MCP server for it on the MCP servers screen and ask again, or say \"build without it\"."
BROWSER = ("If Twitter (X) allows automated browsers, a browser agent can do it with your signed-in session instead "
           "(sign in under Agents › Browser › Sessions); some sites turn automated browsers away.")


@pytest.fixture
async def browser(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings

    async def switch(on: bool):
        await save_settings({"browser_automation_enabled": on})

    return switch


async def test_a_sign_in_api_says_why_and_offers_a_browser_agent(client, browser):
    await browser(True)
    _, _, out = await _reviewed(WRITE, POST, options=[X])
    assert out["outcome"] == "review"
    assert f"{WHY} {STILL} {BROWSER}" in out["say"], "the MCP server first, the browser only as what may work"
    assert "but I can't make a tool for it yet" not in out["say"]
    assert out["no_tool"] == ["twitter_x_post_tweet_x"] and "without:twitter_x_post_tweet_x" in out["offers"]


async def test_with_browser_agents_off_there_is_no_browser_offer(client, browser):
    await browser(False)
    _, _, out = await _reviewed(WRITE, POST, options=[X])
    assert f"{WHY} {STILL}" in out["say"]
    assert "browser agent" not in out["say"] and "Agents › Browser" not in out["say"]


async def test_an_api_with_no_tool_for_another_reason_keeps_its_line(client, browser):
    """Telegram's token in its URL's path is no sign-in: its line is F1121's, word for word, with no browser offer."""
    await browser(True)
    _, _, out = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    assert LINE in out["say"] and "signed in" not in out["say"] and "browser agent" not in out["say"]

"""A host the browser's guard won't let through is said by the guard's own reason (F1505, eb's QA B6).

On prod a page's tracker read "Kept from leaving the browser: 298279967.log.optimizely.com is not a public address":
a public name that didn't resolve. `gate.verdict` and `gate.socket_verdict` said every failure of `check_host` that
way: a name that doesn't resolve, a private address and a fault in the guard alike. Each is now said as what it is.
Every one is still refused: the guard never fails open.
"""
from __future__ import annotations

import pytest

from core import egress_guard
from core.browser import gate

#: What check_host raised → what the person and the agent read.
CASES = [
    (egress_guard.UnresolvedHost("could not resolve 298279967.log.optimizely.com"),
     "298279967.log.optimizely.com could not be found"),
    (egress_guard.BlockedAddress("298279967.log.optimizely.com resolves to a blocked range: private",
                                 "298279967.log.optimizely.com"),
     "298279967.log.optimizely.com is not a public address"),
    (RuntimeError("the resolver broke"), "298279967.log.optimizely.com could not be checked"),
]


@pytest.mark.parametrize("raised, said", CASES)
async def test_a_request_the_guard_refuses_says_why(monkeypatch, raised, said):
    async def check_host(_host):
        raise raised

    monkeypatch.setattr(gate, "check_host", check_host)
    assert await gate.verdict("https://298279967.log.optimizely.com/event", "GET", gate.CO_DRIVE) == said


@pytest.mark.parametrize("raised, said", CASES)
async def test_a_socket_the_guard_refuses_says_why(monkeypatch, raised, said):
    async def check_host(_host):
        raise raised

    monkeypatch.setattr(gate, "check_host", check_host)
    assert await gate.socket_verdict("wss://298279967.log.optimizely.com/live", gate.CO_DRIVE) == said


async def test_a_private_address_is_still_said_as_one():
    # An IP literal needs no DNS: the real guard, unpatched.
    assert await gate.verdict("http://10.0.0.5/", "GET", gate.CO_DRIVE) == "10.0.0.5 is not a public address"

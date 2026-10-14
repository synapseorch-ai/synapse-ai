"""A builder tool registered from another module is opt-in: offered to an agent that names it, never through "all"
(F495, BWA1, D274).

Found on cloud dev after pin 5d37b82: `register_builder_tool` added `read_workspace`, `search_help`,
`product_capabilities` and `prepare_connection` the way the original builder tools are added, so every agent whose
tools are `["all"]` gained four tools — a longer prompt, a changed prompt cache, and a Connect card an ordinary agent
could put up. Knowledge's builder tool was made opt-in for exactly this reason.
"""
from __future__ import annotations

import pytest

from core.builder_tools import BUILDER_TOOL_NAMES, OPT_IN_BUILDER_TOOLS

REGISTERED = {"read_workspace", "search_help", "product_capabilities", "prepare_connection"}


class _Server:
    agent_sessions: dict = {}


def test_every_registered_tool_is_opt_in():
    assert REGISTERED <= BUILDER_TOOL_NAMES
    assert REGISTERED <= OPT_IN_BUILDER_TOOLS


async def _offered(tools: list[str]) -> set[str]:
    from core.tools import aggregate_all_tools

    offered = (await aggregate_all_tools(_Server(), {"tools": tools}, []))[0]
    return {getattr(t, "name", "") for t in offered}


async def test_an_all_agent_does_not_get_them():
    names = await _offered(["all"])
    assert not (REGISTERED & names), REGISTERED & names
    assert "list_agents" in names, "the original builder tools reach it as before"


@pytest.mark.parametrize("tool", sorted(REGISTERED))
async def test_an_agent_that_names_one_gets_it(tool):
    from core.tools import resolve_one_tool

    assert tool in await _offered([tool])
    assert await resolve_one_tool(_Server(), tool, {"tools": [tool]}, []) is not None, "a forced tool step finds it"

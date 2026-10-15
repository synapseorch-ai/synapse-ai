"""A team's members share their turn's MCP sessions (TEAM1a, D283; 20 §13 risk 5).

Members run at once inside the lead's turn and call tools on the same `ClientSession`
per server, with no lock — as a Parallel's branches and a reply's helpers already do.
The SDK routes each response by its request id, so concurrent calls on one session each
get their own answer; this holds it. And a fleet turn opens the servers a team's lead and
members can use: the lead's own tools and its members', as a delegate's (VOX-MCP).
"""
import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from core.scale import mcp_needs


async def test_concurrent_calls_on_one_session_each_get_their_own_answer():
    server = FastMCP("echo")

    @server.tool()
    async def slow_echo(text: str, delay: float) -> str:
        await asyncio.sleep(delay)
        return f"echo:{text}"

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        # The slowest is sent first: answers come back out of order, and each still finds its caller.
        calls = [session.call_tool("slow_echo", {"text": str(i), "delay": 0.05 * (5 - i)}) for i in range(5)]
        results = await asyncio.gather(*calls)
    assert [r.content[0].text for r in results] == [f"echo:{i}" for i in range(5)]


async def test_a_teams_turn_opens_the_leads_and_its_members_servers():
    agents = {
        "ana": {"id": "ana", "type": "conversational", "tools": ["crm__lookup"]},
        "ben": {"id": "ben", "type": "conversational", "tools": ["mail__send"]},
    }

    async def resolve(agent_id):
        return agents.get(agent_id)

    lead = {"id": "lead", "type": "delegate", "tools": ["docs__search"], "delegate_agent_ids": ["ana", "ben"],
            "team": {"mode": "team"}}
    needs = await mcp_needs.of_agent(lead, resolve=resolve)
    assert {"docs__search", "crm__lookup", "mail__send"} <= set(needs)

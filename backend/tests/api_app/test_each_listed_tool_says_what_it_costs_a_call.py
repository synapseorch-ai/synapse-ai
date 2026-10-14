"""The tool list says what each tool adds to every call (F483).

The agent editor tells a person how many tokens the tools they choose add to every model call, and warns past the
budget the engine holds "every tool" to (`core/tools.py` `ALL_TOOLS_TOKEN_BUDGET`). It reads each tool's `tokens` from
`/api/tools/available`, counted the engine's way (`declaration_tokens`), so the editor and a run agree.
"""
from core.tools import declaration_tokens


class _Tool:
    def __init__(self, name, description, schema):
        self.name, self.description, self.inputSchema = name, description, schema


class _Session:
    def __init__(self, *tools):
        self._tools = list(tools)

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


SCHEMA = {"type": "object", "properties": {"project": {"type": "string", "description": "The project to deploy."}}}


async def test_each_tool_says_what_it_adds_to_a_call(client, monkeypatch):
    import core.routes.tools as routes_tools
    import core.server as server

    monkeypatch.setitem(server.agent_sessions, "ext_mcp_vercel", _Session(_Tool("deploy", "Deploy a project.", SCHEMA)))

    async def custom():
        return [{"name": "weather_today", "description": "Today's weather.", "inputSchema": SCHEMA}]

    monkeypatch.setattr(routes_tools, "load_custom_tools", custom)
    tools = {t["name"]: t for t in (await client.get("/api/tools/available")).json()["tools"]}
    assert tools["vercel__deploy"]["tokens"] == declaration_tokens("vercel__deploy", "Deploy a project.", SCHEMA)
    assert tools["weather_today"]["tokens"] == declaration_tokens("weather_today", "Today's weather.", SCHEMA)
    assert tools["vercel__deploy"]["tokens"] > 0

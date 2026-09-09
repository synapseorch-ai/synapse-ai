import asyncio
import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from core.personal_details import load_personal_details


app = Server("personal-details-mcp-server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_personal_details",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "Get saved personal details (name, email, phone, address) from the system. "
                "Use when you need the user's details to complete a workflow."
            ),
            inputSchema={"type": "object", "properties": {}},
        )
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    try:
        if name != "get_personal_details":
            raise ValueError(f"Unknown tool: {name}")

        details = load_personal_details()
        return [types.TextContent(type="text", text=json.dumps(details, ensure_ascii=False))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())

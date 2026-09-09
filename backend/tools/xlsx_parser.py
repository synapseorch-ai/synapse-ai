
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
import asyncio
import json
import requests
import pandas as pd
import io

# Initialize MCP Server
app = Server("xlsx-parser-mcp-server")

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="parse_xlsx",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description="Parse an Excel file (XLSX) from a URL. Extracts sheets and converts them to Markdown tables.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_url": {
                        "type": "string",
                        "description": "URL of the Excel file to parse."
                    }
                },
                "required": ["file_url"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if name == "parse_xlsx":
        file_url = arguments.get("file_url")
        if not file_url:
            raise ValueError("file_url is required")

        try:
            # Download XLSX
            response = requests.get(file_url)
            response.raise_for_status()
            xlsx_bytes = io.BytesIO(response.content)

            output_text = []

            # Read all sheets
            xlsx = pd.read_excel(xlsx_bytes, sheet_name=None)  # None reads all sheets as dict

            for sheet_name, df in xlsx.items():
                output_text.append(f"\n--- Sheet: {sheet_name} ---\n")
                
                if df.empty:
                    output_text.append("[Empty Sheet]")
                    continue

                # Convert to Markdown
                markdown = df.to_markdown(index=False)
                output_text.append(markdown)
                output_text.append("\n")

            return [types.TextContent(type="text", text="".join(output_text))]

        except Exception as e:
            return [types.TextContent(type="text", text=f"Error parsing XLSX: {str(e)}")]

    raise ValueError(f"Tool {name} not found")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())

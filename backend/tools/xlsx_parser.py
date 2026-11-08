
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
import asyncio
import json
import pandas as pd
import io

from core.safe_fetch import fetch_bytes

# Initialize MCP Server
app = Server("xlsx-parser-mcp-server")


def _cell(value) -> str:
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return ""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _markdown_table(df: pd.DataFrame) -> str:
    """A sheet as a Markdown table.

    Written out rather than `DataFrame.to_markdown`, which needs `tabulate` —
    a package nothing here installs, so every parse used to end in an import
    error (F172). Same shape `pdf_parser` gives its tables.
    """
    headers = [_cell(c) for c in df.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="parse_xlsx",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
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
            # The URL is model-supplied: checked at every hop, capped, and
            # given a deadline (S6).
            xlsx_bytes = io.BytesIO(await fetch_bytes(file_url))

            output_text = []

            # Read all sheets
            xlsx = pd.read_excel(xlsx_bytes, sheet_name=None)  # None reads all sheets as dict

            for sheet_name, df in xlsx.items():
                output_text.append(f"\n--- Sheet: {sheet_name} ---\n")
                
                if df.empty:
                    output_text.append("[Empty Sheet]")
                    continue

                output_text.append(_markdown_table(df))
                output_text.append("\n")

            return [types.TextContent(type="text", text="".join(output_text))]

        except Exception as e:
            # Raised, so the call is an MCP error (`isError`): returned as text,
            # a Tool step ended `completed` with this as its output (F174).
            raise RuntimeError(f"Error parsing XLSX: {e}") from e

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

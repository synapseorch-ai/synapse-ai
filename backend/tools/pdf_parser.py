
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
import asyncio
import json
import pdfplumber
import io
from typing import List

from core.safe_fetch import fetch_bytes

# Initialize MCP Server
app = Server("pdf-parser-mcp-server")

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="parse_pdf",
            description="Parse a PDF file from a URL. Extracts text and tables, formatting tables as Markdown.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_url": {
                        "type": "string",
                        "description": "URL of the PDF file to parse."
                    }
                },
                "required": ["file_url"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if name == "parse_pdf":
        file_url = arguments.get("file_url")
        if not file_url:
            raise ValueError("file_url is required")

        try:
            # The URL is model-supplied: checked at every hop, capped, and
            # given a deadline (S6).
            pdf_bytes = io.BytesIO(await fetch_bytes(file_url))

            output_text = []
            
            with pdfplumber.open(pdf_bytes) as pdf:
                for i, page in enumerate(pdf.pages):
                    output_text.append(f"\n--- Page {i+1} ---\n")
                    
                    # Extract tables first to avoid duplicating text if possible, 
                    # but pdfplumber treats them separately.
                    # Strategy: Extract text, then extract tables and append them formatted.
                    
                    text = page.extract_text()
                    if text:
                        output_text.append(text)
                    
                    tables = page.extract_tables()
                    if tables:
                        output_text.append("\n[Tables Found on Page]\n")
                        for table in tables:
                            # Convert table to markdown
                            # Filter out None values
                            clean_table = [[str(cell) if cell is not None else "" for cell in row] for row in table]
                            
                            if not clean_table:
                                continue
                                
                            # Create markdown table
                            # Header
                            headers = clean_table[0]
                            header_row = "| " + " | ".join(headers) + " |"
                            separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"
                            
                            md_table = [header_row, separator_row]
                            
                            # Body
                            for row in clean_table[1:]:
                                md_table.append("| " + " | ".join(row) + " |")
                            
                            output_text.append("\n".join(md_table))
                            output_text.append("\n")

            return [types.TextContent(type="text", text="".join(output_text))]

        except Exception as e:
            return [types.TextContent(type="text", text=f"Error parsing PDF: {str(e)}")]

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

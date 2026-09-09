from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel


app = Server("collect-data-server")


class FieldDefinition(BaseModel):
    label: str
    type: str  # text, number, email, date, phone, options
    options: list[str] | None = None
    multiple: bool = False


class CollectDataArgs(BaseModel):
    fields: list[FieldDefinition]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="collect_data",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description=(
                "**USE THIS TOOL TO REQUEST DATA FROM THE USER VIA A FORM.** "
                "This tool displays a form in the chat interface where the user can enter their information. "
                "\n\n"
                "**CRITICAL INSTRUCTIONS:** "
                "\n"
                "1. This tool does NOT collect or provide actual data — it only returns a form definition. "
                "\n"
                "2. **DO NOT PROCESS OR UNDERSTAND THE TOOL'S RESPONSE.** When this tool returns data, you MUST return it DIRECTLY to the user/client as-is. "
                "\n"
                "3. The frontend will automatically detect this response and build a form based on it. "
                "\n"
                "4. After the user fills out and submits the form, their response will appear as a new user message in the conversation. "
                "\n"
                "5. DO NOT hallucinate or make up data like 'John Smith' or 'john@example.com'. Instead, use this tool to ASK the user, then WAIT for their response. "
                "\n\n"
                "**WORKFLOW:** "
                "\n"
                "- You call this tool → Tool returns form definition → You pass it DIRECTLY to client (no processing!) → Frontend builds form → User submits → You receive actual data in next message. "
                "\n\n"
                "**EXAMPLES:** "
                "\n"
                "- Need email? Call: `collect_data(fields=[{label: 'Your email address', type: 'email'}])` → Return tool response directly → User sees form → User submits → You get real email. "
                "\n"
                "- Need multiple fields? Call: `collect_data(fields=[{label: 'Full name', type: 'text'}, {label: 'Email', type: 'email'}])` → Return tool response directly → User fills form → You get real data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "description": "List of form fields to collect from the user",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "The question/label to show the user (e.g., 'What is your email address?')"
                                },
                                "type": {
                                    "type": "string",
                                    "enum": ["text", "number", "email", "date", "phone", "options"],
                                    "description": "Type of input field"
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "List of options if type is 'options'"
                                },
                                "multiple": {
                                    "type": "boolean",
                                    "description": "Allow multiple selections (only for type='options')"
                                }
                            },
                            "required": ["label", "type"]
                        }
                    }
                },
                "required": ["fields"]
            },
        )
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    try:
        sys.stderr.write(f"DEBUG: collect_data called with {arguments}\n")
        if name != "collect_data":
            raise ValueError(f"Unknown tool: {name}")

        args = CollectDataArgs(**(arguments or {}))
        
        # Convert to dict for JSON serialization
        result = {
            "fields": [field.dict() for field in args.fields]
        }
        
        return [
            types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False),
            )
        ]
    except Exception as e:
        sys.stderr.write(f"ERROR: collect_data error: {e}\n")
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

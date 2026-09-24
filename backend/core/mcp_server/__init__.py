"""Synapse as an MCP server (D125): builder tools, run tools and every workflow as a tool.

`server.py` is the tool list and dispatch, `endpoint.py` serves it over
streamable HTTP inside the backend, and `synapse mcp` relays stdio clients to it.
"""

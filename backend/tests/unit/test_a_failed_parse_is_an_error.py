"""
A parse that fails is a failed tool call, not the parse's answer (F174).

`parse_pdf` and `parse_xlsx` caught every exception and returned "Error parsing
PDF: …" as ordinary text content. So a Tool step whose download was refused,
or whose file was not a PDF, ended `completed` with the error text as its
output: four fleet runs on 2026-09-25, all `completed` (found proving S6,
D128). F90's rule is that a tool's error is a failed call; over MCP that is
`isError`. Read through a real MCP session, where a client reads it.
"""
from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.security.test_parser_egress import MARKER, _public_is_127, server  # noqa: F401 — fixtures
from tools import pdf_parser, xlsx_parser

PARSERS = [
    pytest.param(pdf_parser, "parse_pdf", "doc.pdf", "sheet.xlsx", "Error parsing PDF:", id="pdf"),
    pytest.param(xlsx_parser, "parse_xlsx", "sheet.xlsx", "doc.pdf", "Error parsing XLSX:", id="xlsx"),
]


async def _call(module, tool: str, url: str):
    async with create_connected_server_and_client_session(module.app) as client:
        return await client.call_tool(tool, {"file_url": url})


@pytest.mark.parametrize("module,tool,good,wrong,prefix", PARSERS)
async def test_a_parse_that_works_is_the_answer(server, module, tool, good, wrong, prefix):
    result = await _call(module, tool, f"http://127.0.0.1:{server}/{good}")
    assert not result.isError
    assert MARKER in result.content[0].text


@pytest.mark.parametrize("module,tool,good,wrong,prefix", PARSERS)
async def test_the_wrong_kind_of_file_is_an_error(server, module, tool, good, wrong, prefix):
    result = await _call(module, tool, f"http://127.0.0.1:{server}/{wrong}")
    assert result.isError, result.content[0].text
    assert result.content[0].text.startswith(prefix)


@pytest.mark.parametrize("module,tool,good,wrong,prefix", PARSERS)
async def test_a_refused_download_is_an_error(server, module, tool, good, wrong, prefix):
    result = await _call(module, tool, "http://10.0.0.1/file")
    assert result.isError, result.content[0].text
    assert result.content[0].text.startswith(prefix) and "private" in result.content[0].text


@pytest.mark.parametrize("module,tool,good,wrong,prefix", PARSERS)
async def test_a_missing_file_is_an_error(server, module, tool, good, wrong, prefix):
    result = await _call(module, tool, f"http://127.0.0.1:{server}/nope")
    assert result.isError, result.content[0].text

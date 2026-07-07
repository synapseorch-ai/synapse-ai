"""
Fake httpx client for provider-call tests.

Providers that talk over HTTP (OpenAI/Grok/DeepSeek/OpenAI-compatible and the
local Ollama path) all use ``httpx.AsyncClient`` as an async context manager and
call ``.post(...)``. ``install_httpx`` swaps that out so tests can drive the
provider bodies (success, retry, error) without any network.
"""
from __future__ import annotations

from typing import Any, Callable

import httpx


class FakeResponse:
    def __init__(self, json_data: Any = None, status_code: int = 200, text: str = ""):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://test")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=req, response=resp)


def install_httpx(monkeypatch, handler: Callable[[str, dict], FakeResponse] | FakeResponse):
    """Patch httpx.AsyncClient so post()/get() return the given response.

    ``handler`` may be a single FakeResponse, or a callable(url, kwargs) ->
    FakeResponse (useful for retry sequences / URL-dependent responses).
    """
    def _resolve(url, kwargs):
        return handler(url, kwargs) if callable(handler) else handler

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return _resolve(url, kwargs)

        async def get(self, url, **kwargs):
            return _resolve(url, kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def sequence_handler(responses: list[FakeResponse]):
    """A handler that returns each response in turn (for retry tests)."""
    box = {"i": 0}

    def _h(url, kwargs):
        i = min(box["i"], len(responses) - 1)
        box["i"] += 1
        return responses[i]

    return _h

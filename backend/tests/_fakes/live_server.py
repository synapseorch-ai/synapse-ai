"""A real HTTP server on the test's own event loop.

For the one thing httpx's ``ASGITransport`` cannot do: hang up. It buffers a
response body whole and never tells the app the client went away, so a route
whose work dies with its reader passes every test written against it (F49).
Here the client is a real TCP connection, and leaving the ``async with`` closes
it mid-body, which is what a closed tab or a reload does.

The server runs as a task on the test's loop, in the test's process: the store
the autouse fixtures point at ``tmp_path``, the monkeypatched fake LLM and the
aiosqlite connections are all bound to that loop, and a thread or a subprocess
would see none of them.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import AsyncIterator

import httpx
import uvicorn


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves pytest's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class LiveServer:
    def __init__(self, url: str, server: uvicorn.Server) -> None:
        self.url = url
        self._server = server

    def client(self, **kwargs) -> httpx.AsyncClient:
        """A fresh client — its own connection, so closing it closes the socket."""
        headers = dict(kwargs.pop("headers", {}) or {})
        token = os.getenv("SYNAPSE_INTERNAL_TOKEN")
        if token:
            headers.setdefault("X-Synapse-Internal", token)
        return httpx.AsyncClient(
            base_url=self.url, headers=headers,
            timeout=kwargs.pop("timeout", 30.0), **kwargs,
        )

    async def wait_disconnected(self, timeout: float = 5.0) -> None:
        """Return once the server has seen every connection close."""
        deadline = time.monotonic() + timeout
        while self._server.server_state.connections:
            if time.monotonic() > deadline:
                raise AssertionError("the server never saw the client hang up")
            await asyncio.sleep(0.01)
        # Starlette hears the disconnect on its own receive() — give it a turn.
        await asyncio.sleep(0.2)


@contextlib.asynccontextmanager
async def serve(app) -> AsyncIterator[LiveServer]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off",
        log_config=None, log_level="warning", access_log=False, ws="none",
        # Without it, shutdown waits for ever on a stream still being tailed.
        timeout_graceful_shutdown=2,
    )
    server = _QuietServer(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()  # a startup failure surfaces here
                raise RuntimeError("uvicorn stopped before it started")
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield LiveServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 15)

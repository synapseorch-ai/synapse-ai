"""A secret variable only the test's resolver can read, and a scan for it in everything a test left behind (F854).

`API_TOKEN` is stored as a reference, as cloud stores a secret (`synsec://…`); the resolver swaps it for `CANARY`
on the way out of the store, as cloud's does. Nothing else ever sees the plaintext, so finding it anywhere — an
event, a frame, a file, the database — is a leak.
"""
from __future__ import annotations

from pathlib import Path

import pytest

CANARY = "SYNSEC-CANARY-7f3a9c1e5b"
REFERENCE = "synsec://vault/API_TOKEN"
MARK = "‹secret: API_TOKEN›"


@pytest.fixture
async def canary_secret():
    from core import variables
    from core.store import collections

    async def resolve(document: dict) -> dict:
        values = document.get("values")
        if not isinstance(values, dict):
            return document
        return {**document, "values": {k: (CANARY if v == REFERENCE else v) for k, v in values.items()}}

    await variables.save({"API_TOKEN": REFERENCE, "REGION": "eu-west-1"})
    collections.set_document_resolver(resolve)
    yield
    collections.set_document_resolver(None)


def left_behind(root: Path) -> list[str]:
    """Every file under `root` (a test's own directories: database, blobs, scratch, state) holding the plaintext."""
    return [str(path.relative_to(root)) for path in root.rglob("*")
            if path.is_file() and CANARY.encode() in path.read_bytes()]

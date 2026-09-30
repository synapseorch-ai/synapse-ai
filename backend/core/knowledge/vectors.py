# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""How an embedding is stored as bytes: float32, little-endian, no header.

The engine's `knowledge_chunks.embedding` column (SQLite BLOB, Postgres BYTEA)
and a trace's query vector both hold this. The width is the byte length / 4;
the snapshot records which model made it.

Standard library only (`array`), so the cloud API and the contract tests do
not need numpy. OSS's vector leg reads the same bytes with
``numpy.frombuffer(b, dtype="<f4")``.
"""
from __future__ import annotations

import sys
from array import array
from collections.abc import Sequence

BYTES_PER_DIM = 4


def pack(vector: Sequence[float]) -> bytes:
    """A vector as float32 little-endian bytes."""
    values = array("f", vector)
    if sys.byteorder == "big":  # pragma: no cover - no supported platform is big-endian
        values.byteswap()
    return values.tobytes()


def unpack(blob: bytes) -> list[float]:
    """Bytes from `pack` back into floats (float32 precision)."""
    if len(blob) % BYTES_PER_DIM:
        raise ValueError(f"an embedding is a whole number of float32s, not {len(blob)} bytes")
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder == "big":  # pragma: no cover
        values.byteswap()
    return values.tolist()


def dims_of(blob: bytes) -> int:
    return len(blob) // BYTES_PER_DIM

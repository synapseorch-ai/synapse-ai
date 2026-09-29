"""
Free ports, found rather than asked for.

The old installer asked "Change the default ports?" of everyone to serve the few
whose 3000 was taken. Now a taken port is simply skipped for the next free one.
A port counts as taken if anything listens on it on any interface — the
frontend binds 0.0.0.0, so a loopback-only probe would miss a clash.
"""
from __future__ import annotations

import socket


def is_free(port: int) -> bool:
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                s.bind((host, port))
            except OSError:
                return False
    return True


def free_port(preferred: int, avoid: set[int] | None = None, tries: int = 50) -> int:
    """`preferred` if free, else the next free port above it."""
    avoid = avoid or set()
    for port in range(preferred, preferred + tries):
        if port not in avoid and is_free(port):
            return port
    return preferred


def pick(backend: int = 8765, frontend: int = 3000) -> tuple[int, int]:
    b = free_port(backend)
    f = free_port(frontend, avoid={b})
    return b, f

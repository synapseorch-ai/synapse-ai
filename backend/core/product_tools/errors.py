"""A call a product tool refuses: the caller sees the message as the tool's error, never a crash."""
from __future__ import annotations


class ToolRefusal(Exception):
    pass

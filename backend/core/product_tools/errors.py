"""A call a product tool refuses: the caller sees the message as the tool's error, never a crash."""
from __future__ import annotations


class ToolRefusal(Exception):
    pass


class AlreadySpent(ToolRefusal):
    """The approved review that covers this call has already made it (F778, MCP2's review): a review's coverage is
    spent once per call, as a token is, so a redelivered or resumed apply makes nothing twice."""

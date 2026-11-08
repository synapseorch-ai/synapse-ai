"""Exit codes, and the one error type a command raises to leave with one."""
from __future__ import annotations

#: The command did what it was asked.
OK = 0
#: Anything else that went wrong: the server said no to what was asked.
ERROR = 1
#: argparse's own code for a command line it cannot read.
USAGE = 2
#: A run stopped to ask a person, and nobody here could answer.
WAITING = 3
#: A run ended, but not by completing: it failed or was cancelled.
RUN_FAILED = 4
#: The thing named does not exist: HTTP 404, or nothing has that name or id (D294).
NOT_FOUND = 5
#: The key was refused, or it may not do this: HTTP 401 or 403 (D294).
REFUSED = 6
#: Synapse is busy or cannot be reached: HTTP 429 or 503 after waiting once, or no answer at all (D294).
UNAVAILABLE = 7


def code_for(status: int) -> int:
    """The exit code an HTTP refusal leaves with."""
    if status == 404:
        return NOT_FOUND
    if status in (401, 403):
        return REFUSED
    if status in (429, 503):
        return UNAVAILABLE
    return ERROR


class CliError(Exception):
    """A message for stderr and the code to exit with."""

    def __init__(self, message: str, code: int = ERROR, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        #: The HTTP status the server answered with, when it answered.
        self.status = status


class ApiError(CliError):
    """The server answered, and not with a success."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{detail} (HTTP {status})", code_for(status), status)
        self.detail = detail


class StreamBroken(CliError):
    """A stream that had started was cut before its end."""

    def __init__(self, message: str):
        super().__init__(message, UNAVAILABLE)

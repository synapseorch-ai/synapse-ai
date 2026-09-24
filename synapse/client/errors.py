"""Exit codes, and the one error type a command raises to leave with one."""
from __future__ import annotations

#: The command did what it was asked.
OK = 0
#: Anything that went wrong: the server said no, could not be reached, or the
#: thing named does not exist.
ERROR = 1
#: argparse's own code for a command line it cannot read.
USAGE = 2
#: A run stopped to ask a person, and nobody here could answer.
WAITING = 3
#: A run ended, but not by completing: it failed or was cancelled.
RUN_FAILED = 4


class CliError(Exception):
    """A message for stderr and the code to exit with."""

    def __init__(self, message: str, code: int = ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


class ApiError(CliError):
    """The server answered, and not with a success."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{detail} (HTTP {status})", ERROR)
        self.status = status
        self.detail = detail

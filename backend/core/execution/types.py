"""What a code runner is asked to do, and what it answers.

`ExecRequest` is deliberately a description of *work*, not of Docker. Nothing on
it names a container, an image or a mount flag, because the same request has to
mean something to a runner that has no host filesystem at all.

## `network` has no default, and that is the point

The engine used to compute an `allow_network` flag, thread it through
`_build_docker_cmd(…, allow_network=…)` — and then never use it. Every sandbox
in the product ran on the default bridge network, with the code that *looked*
like it was choosing otherwise sitting right there. A keyword argument can be
ignored; a required constructor field cannot, and the one test that asserts
`Network.NONE` actually blocks a URL is what would have caught it.

`RESTRICTED` is deliberately **not** a member yet. A deployment that wants "the
internet, but only these hosts" needs an egress proxy to enforce it, and adding
the name before the mechanism would let a caller ask for a guarantee nothing
delivers. `allowed_domains` rides on `INTERNET` and is documented for what it
is: a guardrail a runner applies inside the sandbox, not a boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Network(str, Enum):
    """What the code may reach."""

    #: No outbound network at all. A runner that cannot guarantee this must
    #: raise rather than run the code with less isolation than was asked for.
    NONE = "none"

    #: The public internet. `allowed_domains`, if set, is a best-effort narrowing
    #: applied inside the sandbox — see `ExecRequest.allowed_domains`.
    INTERNET = "internet"


@dataclass(frozen=True)
class ExecRequest:
    """One script to run, and the box to run it in.

    `code` is the **complete** script, already carrying whatever preamble the
    caller needs — `wrap_args` and `wrap_transform` in this package build the
    two the engine uses. Keeping the wrapping at the caller is what makes
    `DockerRunner` a move of the existing code rather than a rewrite of it.
    """

    code: str

    #: Required. See the module docstring.
    network: Network

    #: The script's own budget. A runner waits `timeout_s + grace_s` for the
    #: process before killing it, so the script gets the time it was promised
    #: and the runner still bounds its own wait.
    timeout_s: int = 30
    grace_s: int = 5

    memory_mb: int = 512
    cpus: str = "1.0"
    tmpfs_mb: int = 256

    #: Hostnames the code should be able to reach when `network` is `INTERNET`.
    #: Empty means "no narrowing". **This is a guardrail, not a boundary** — a
    #: runner enforces it from inside the sandbox, where the code being
    #: constrained is also the code that could go around it. A deployment that
    #: needs a boundary puts a proxy outside the sandbox and does not rely on
    #: this field.
    allowed_domains: tuple[str, ...] = ()

    #: Packages to install before the script runs. Only a runner that has both a
    #: writable site-packages and a network can honour this; the others raise.
    packages: tuple[str, ...] = ()

    #: A directory to expose read-only at `/data`. Ignored by runners with no
    #: host filesystem to share — see `LambdaRunner` in the cloud tree.
    vault_path: Path | None = None

    max_stdout: int = 20_000
    max_stderr: int = 5_000

    #: Free-form, for a runner that needs a hint the others do not. Nothing in
    #: the engine reads it.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecResult:
    """What came back. Already truncated to the request's caps."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ExecUnsupported(RuntimeError):
    """The runner cannot honour this request, and will not run it anyway.

    Raised rather than silently degrading: a request for `Network.NONE` served
    with a network, or a `packages` install quietly skipped, is the class of bug
    this package exists to end.
    """

"""Running code that somebody else wrote.

Five places in this engine shelled out to `docker run` with slightly different
flags, and one of the differences was a `--network` argument that existed in
every signature and in none of the commands. This package is the one place, and
`ExecRequest.network` is a field a runner must answer for rather than a keyword
argument it can drop.

    from core.execution import ExecRequest, Network, get_runner, wrap_args

    result = await get_runner().run(
        ExecRequest(code=wrap_args(tool["code"], tool_args), network=Network.INTERNET)
    )

A deployment chooses its runner with `SYNAPSE_CODE_RUNNER=package.module:factory`
(see `runner.py`); unset, it is `DockerRunner` — what the product has always
done.
"""
from .docker_runner import DOCKER_IMAGE, DockerRunner
from .host_runner import HostRunner
from .runner import RUNNER_ENV, CodeRunner, describe_runner, get_runner, register_runner
from .scripts import wrap_args, wrap_transform
from .types import ExecRequest, ExecResult, ExecUnsupported, Network

__all__ = [
    "DOCKER_IMAGE",
    "RUNNER_ENV",
    "CodeRunner",
    "DockerRunner",
    "ExecRequest",
    "ExecResult",
    "ExecUnsupported",
    "HostRunner",
    "Network",
    "describe_runner",
    "get_runner",
    "register_runner",
    "wrap_args",
    "wrap_transform",
]

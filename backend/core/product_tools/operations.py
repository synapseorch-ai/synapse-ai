"""What only a hosted workspace has: its dead jobs, and filing a support request (F1260, REACH1; D340 R1, Naveen 10-30).

Naveen: Synapse AI triages what failed, so on Synapse Cloud it also retries or discards a job the fleet gave up on, and
files a support request for the person. These are cloud's screens (Dead jobs, Support); a self-hosted install has
neither a fleet's dead-letter queue nor a support desk. So the engine holds each act's entry and its card, and says
plainly that it is not here; the host that has them (`set_available`) does each through its own route, its roles and
its audit row (cloud's `assistant_hooks.ACTS`).

- `retry_dead_job`: run the job on again from where it stopped (a builder's, as cloud's route is).
- `discard_dead_job`: take it off the list, the run left as it ended (a builder's; Naveen's Q1, 10-30).
- `file_support_request`: a subject, what happened, its category and severity, and the run it is about (any member's,
  in the app only: a signed-in person files it).

All three are the app's only (`app_only`): never served over MCP or the API.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

#: What each is, on an install that does not have it.
NO_DEAD_JOBS = ("This install has no dead-job queue: a run that failed is in Runs, and resumes from its page or with "
                "a resume card once what failed is fixed.")
NO_SUPPORT = ("Support requests are filed on Synapse Cloud. Here, the project's issues are linked from Help & docs "
              "(/settings/support).")

#: Support's own lists, as cloud's desk takes them.
CATEGORIES = ("bug", "question", "billing", "account", "feature", "security")
SEVERITIES = ("blocking", "degraded", "minor")

_available = False


def set_available(on: bool) -> None:
    """A host that has dead jobs and a support desk says so; its own handlers do each (cloud's ACTS)."""
    global _available
    _available = bool(on)


def available() -> bool:
    return _available


async def _not_here_jobs(caller: Any, args: dict[str, Any]) -> dict:
    raise ToolRefusal(NO_DEAD_JOBS)


async def _not_here_support(caller: Any, args: dict[str, Any]) -> dict:
    raise ToolRefusal(NO_SUPPORT)


async def _preview_job(caller: Any, args: dict[str, Any]) -> dict:
    if not _available:
        raise ToolRefusal(NO_DEAD_JOBS)
    return {"job_id": str(args.get("job_id") or "")}


async def _preview_support(caller: Any, args: dict[str, Any]) -> dict:
    if not _available:
        raise ToolRefusal(NO_SUPPORT)
    return {k: args.get(k) for k in ("subject", "body", "category", "severity", "run_id") if args.get(k)}


_JOB = {"type": "object", "properties": {"job_id": {"type": "string", "description": "From topic `dead_jobs`."}},
        "required": ["job_id"]}


def entries() -> list[ProductTool]:
    return [
        ProductTool(name="retry_dead_job", handler=_not_here_jobs, kind="act", area="operations", app_only=True,
                    min_role=Role.BUILDER, scope="run", open_world=True, confirmed=True, preview=_preview_job,
                    self_audited=True, offered=available, input_schema=dict(_JOB),
                    description="Retry a job the fleet gave up on: its run goes on from where it stopped."),
        ProductTool(name="discard_dead_job", handler=_not_here_jobs, kind="act", area="operations", app_only=True,
                    min_role=Role.BUILDER, scope="run", destructive=True, confirmed=True, preview=_preview_job,
                    self_audited=True, offered=available, input_schema=dict(_JOB),
                    description="Take a job the fleet gave up on off the list; its run stays as it ended."),
        ProductTool(name="file_support_request", handler=_not_here_support, kind="act", area="operations",
                    app_only=True, min_role=Role.VIEWER, scope="run", open_world=True, confirmed=True,
                    preview=_preview_support, self_audited=True, offered=available,
                    input_schema={"type": "object", "properties": {
                        "subject": {"type": "string"}, "body": {"type": "string"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "run_id": {"type": "string"}}, "required": ["subject", "body", "category", "severity"]},
                    description="File a support request for the person: what happened, its category and severity, "
                                "and the run it is about."),
    ]

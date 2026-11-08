"""
Logs API: endpoints for agent run logs and orchestration run logs.
Agent logs: /api/logs/agents
Orchestration logs: /api/logs/orchestrations (mirrors /api/orchestrations/logs)
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter()


# -- Agent Logs -----------------------------------------------------

@router.get("/api/logs/agents")
async def list_agent_logs(limit: int = 100, offset: int = 0, agent_id: str | None = None):
    """List recent agent run logs (summary only), optionally for one agent."""
    from core.agent_logger import AgentLogger
    return AgentLogger.list_logs(limit=limit, offset=offset, agent_id=agent_id)


@router.get("/api/logs/agents/{run_id}")
async def get_agent_log(run_id: str):
    """Get full detailed log for a specific agent run (plain text)."""
    from core.agent_logger import AgentLogger
    log = AgentLogger.get_log(run_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return PlainTextResponse(log)


@router.delete("/api/logs/agents/{run_id}")
async def delete_agent_log(run_id: str):
    """Delete a specific agent log."""
    from core.agent_logger import AgentLogger
    if AgentLogger.delete_log(run_id):
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(status_code=404, detail="Log not found")


# -- Orchestration Logs ---------------------------------------------

@router.get("/api/logs/orchestrations")
async def list_orchestration_logs(limit: int = 100, offset: int = 0):
    """List recent orchestration run logs (summary only)."""
    from core.orchestration.logger import OrchestrationLogger
    return OrchestrationLogger.list_logs(limit=limit, offset=offset)


@router.get("/api/logs/orchestrations/{run_id}")
async def get_orchestration_log(run_id: str):
    """Get full detailed log for a specific orchestration run (plain text)."""
    from core.orchestration.logger import OrchestrationLogger
    log = OrchestrationLogger.get_log(run_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return PlainTextResponse(log)


@router.delete("/api/logs/orchestrations/{run_id}")
async def delete_orchestration_log(run_id: str):
    """Delete a specific orchestration log."""
    from core.orchestration.logger import OrchestrationLogger
    if OrchestrationLogger.delete_log(run_id):
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(status_code=404, detail="Log not found")


# -- Schedule Logs ------------------------------------------------------

@router.get("/api/logs/schedules")
async def list_schedule_logs(limit: int = 100, offset: int = 0):
    """List recent schedule run logs (summary only)."""
    from core.schedule_logger import ScheduleLogger
    return ScheduleLogger.list_logs(limit=limit, offset=offset)


@router.get("/api/logs/schedules/{run_id}")
async def get_schedule_log(run_id: str):
    """Get full detailed log for a specific schedule run (plain text)."""
    from core.schedule_logger import ScheduleLogger
    log = ScheduleLogger.get_log(run_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return PlainTextResponse(log)


@router.delete("/api/logs/schedules/{run_id}")
async def delete_schedule_log(run_id: str):
    """Delete a specific schedule log."""
    from core.schedule_logger import ScheduleLogger
    if ScheduleLogger.delete_log(run_id):
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(status_code=404, detail="Log not found")

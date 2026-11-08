"""An agent's memory, for the person who owns it (F270).

The engine keeps a memory folder per agent (`core/agent_memory.py`); until now
nothing in the product read it, so a person could not see, correct or delete
what an agent remembered except by asking the agent to forget. These are the
agent editor's routes — cloud's two reads (`api/control/routes/agents.py`), and
the edit and delete cloud's viewer does not have yet.

Owner routes, behind the member's session like the rest of `/api/agents`. A
widget visitor's token opens none of them, and a visitor's turn has no memory
at all (F276).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import agent_memory

router = APIRouter()

_KINDS = ("items", "sessions")


async def _agent_or_404(agent_id: str) -> dict:
    from core.store.resources import get_agent
    agent = await get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="No such agent.")
    return agent


def _kind_or_404(kind: str) -> str:
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail="Memory has items and sessions.")
    return kind


@router.get("/api/agents/{agent_id}/memory")
async def get_agent_memory(agent_id: str):
    """The index, the saved memories and the past sessions, newest first."""
    agent = await _agent_or_404(agent_id)
    return {"enabled": bool(agent.get("memory")), **agent_memory.memory_view(agent_id)}


@router.get("/api/agents/{agent_id}/memory/{kind}/{name}")
async def get_agent_memory_file(agent_id: str, kind: str, name: str):
    """One saved memory or session trace, whole."""
    await _agent_or_404(agent_id)
    content = agent_memory.read_file(agent_id, _kind_or_404(kind), name)
    if content is None:
        raise HTTPException(status_code=404, detail="That is not in this agent's memory.")
    return {"name": name, "kind": kind, "content": content}


class MemoryEdit(BaseModel):
    content: str


@router.put("/api/agents/{agent_id}/memory/items/{name}")
async def edit_agent_memory(agent_id: str, name: str, body: MemoryEdit):
    """Correct a saved memory. It is re-indexed, as the newest."""
    await _agent_or_404(agent_id)
    saved, error = await agent_memory.write_memory(agent_id, name, body.content)
    if error:
        raise HTTPException(status_code=422, detail=f"Not saved: {error}.")
    return {"name": saved}


@router.delete("/api/agents/{agent_id}/memory/{kind}/{name}")
async def delete_agent_memory_file(agent_id: str, kind: str, name: str):
    """Delete one saved memory (and its index line) or one session trace."""
    await _agent_or_404(agent_id)
    if _kind_or_404(kind) == "items":
        existed = await agent_memory.delete_memory(agent_id, name)
    else:
        existed = await agent_memory.delete_session(agent_id, name)
    if not existed:
        raise HTTPException(status_code=404, detail="That is not in this agent's memory.")
    return {"deleted": name}


@router.delete("/api/agents/{agent_id}/memory")
async def forget_everything(agent_id: str):
    """Everything this agent remembered: index, archive, memories and sessions."""
    await _agent_or_404(agent_id)
    return {"deleted": await agent_memory.wipe(agent_id)}

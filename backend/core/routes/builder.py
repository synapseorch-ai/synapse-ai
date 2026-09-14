"""The AI builder's endpoints.

The builder is an ordinary orchestration (`orch_native_builder`) with ordinary
sub-agents, so these routes do very little: start a run, resume one, and stream
what the engine says. The stream is the engine's own events, unchanged.

That last part used to be false. This module translated engine events into a
private vocabulary and, because a tool result reaches it truncated to 500
characters, recovered "an orchestration was saved" by running a regex over the
truncation — with three fallbacks behind it, all three dead code, the last of
which announced *the most recently updated orchestration in the workspace*
whether or not this run had touched it. The dispatcher now emits
`resource_saved` with the whole resource, so there is nothing left to infer,
and one reducer in the frontend maps engine events to panel messages for both
this product and the commercial one.
"""
import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.images import checked_images
from core.models_orchestration import Orchestration
from core.native_builder import NATIVE_BUILDER_ORCH_ID, bundled_definitions
from core.orchestration.engine import OrchestrationEngine
from core.react_engine import iter_with_heartbeat

router = APIRouter()

#: Engine events that are internal bookkeeping — mid-step progress markers and
#: the tool-step's own LLM log. Underscore-prefixed by convention.
def _is_internal(event_type: str) -> bool:
    return event_type.startswith("_")


class BuilderChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    selected_agent_ids: list[str] = []
    can_create_agents: bool = False
    model: str | None = None
    current_orchestration_id: str | None = None
    #: Identifies one conversation. It reaches the engine as part of the
    #: session id, which scopes the tool cache and the agents' own history —
    #: this was the literal string "builder" for every conversation, and in the
    #: commercial edition that meant every conversation of every user.
    client_session_id: str = ""
    #: Base64 data-URI images, same transport and same limits as chat. Reaches
    #: the run out of band rather than in `initial_state` — see
    #: `run_builder_stream` for why.
    images: list[str] = []

    _check_images = checked_images()


class BuilderResumeRequest(BaseModel):
    run_id: str
    response: dict = {}
    #: Attachments sent *with the answer*. The images a run started with are
    #: not persisted anywhere — see `OrchestrationEngine.resume` — so a step
    #: re-entered after a pause sees only what was attached to the reply.
    images: list[str] = []

    _check_images = checked_images()


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history)


async def _resolve_current_orch_id(raw: str | None) -> str:
    """Only pass through IDs that actually exist — the UI may send temp draft IDs."""
    if not raw:
        return ""
    # An indexed point lookup, rather than loading every orchestration to test
    # one id for membership.
    from core.store.resources import get_orchestration
    return raw if await get_orchestration(raw) else ""


async def _workspace_profile() -> str:
    """What this workspace is for, in one line, for the analyst's first read.

    Open source has nowhere to record it: the `personal_details` collection is
    contact information — a name, a phone number, a postal address — not a
    description of the work, and piping that into a model prompt costs
    something and buys nothing. The commercial edition fills this key from the
    onboarding questionnaire, which does ask.
    """
    return "(not recorded — ask the user if the answer would change what you build)"


async def _selected_model(request_model: str | None) -> str | None:
    """The model this run uses: the caller's pick, else the workspace default."""
    if request_model:
        return request_model
    from core.config import load_settings
    return load_settings().get("builder_model") or None


def _session_id(client_session_id: str) -> str:
    from core.tenancy import get_tenant
    return f"builder:{get_tenant()}:{client_session_id or 'default'}"


async def reject_unseeable_images(request: BuilderChatRequest) -> None:
    """Refuse attachments a model will throw away, rather than throwing them away.

    Four providers discard images — two with a log line nobody reads, two in
    silence — so the run answers as though nothing were attached, which reads as
    the agent ignoring you. The composer already disables its attach button from
    `vision_capable`; this is for every caller that is not the composer, and for
    the chat page's builder-typed agent, which reaches the same run by a
    different door.
    """
    if not request.images:
        return
    from core.config import load_settings
    from core.images import model_can_see_images

    resolved = await _selected_model(request.model) or load_settings().get("model", "")
    if not model_can_see_images(resolved):
        raise HTTPException(
            status_code=422,
            detail=f"{resolved} cannot see images. Pick a different builder model, "
                   f"or send the message without them.",
        )


async def run_builder_stream(request: BuilderChatRequest, server_module):
    """Drive the builder orchestration, yielding the engine's own events."""
    from core.store.resources import get_orchestration

    orch_data = await get_orchestration(NATIVE_BUILDER_ORCH_ID)
    if not orch_data:
        yield {
            "type": "orchestration_error",
            "error": f"The builder orchestration '{NATIVE_BUILDER_ORCH_ID}' is not installed.",
        }
        return

    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, server_module)
    # Deliberately NOT in `initial_state`. Shared state is echoed to the browser
    # twice at the end of a run, checkpointed at every step boundary, returned by
    # the run-detail endpoint and fed to the memory summariser — all fine for a
    # model name, none of it fine for up to 20 MB of image. `global_variables`
    # travels the same way for the same reason.
    engine.images = list(request.images or [])

    initial_state = {
        "user_message": request.message,
        "chat_history": _format_history(request.history),
        "selected_agent_ids": list(request.selected_agent_ids or []),
        "can_create_agents": bool(request.can_create_agents),
        "current_orchestration_id": await _resolve_current_orch_id(request.current_orchestration_id),
        "org_profile": await _workspace_profile(),
        "requirements": "",
        "human_response": "",
        "final_orch": {},
        # Applied by the engine on run and re-applied on resume, so the second
        # half of a conversation runs on the model the first half did.
        OrchestrationEngine.MODEL_OVERRIDE_KEY: await _selected_model(request.model),
    }

    async for event in engine.run(
        initial_input=request.message,
        run_id=f"builder_{uuid.uuid4().hex[:12]}",
        session_id=_session_id(request.client_session_id),
        initial_state=initial_state,
    ):
        if not _is_internal(event.get("type", "")):
            yield event


async def run_builder_resume_stream(run_id: str, human_response: dict, server_module,
                                    images: list[str] | None = None):
    """Resume a paused builder run, yielding the engine's own events.

    This used to be a hand-copied near-duplicate of `OrchestrationEngine.resume`
    that had drifted: it never called `_complete_human_step`, so the approval
    stayed open in the run's history, and it re-applied the model itself
    because there was nowhere to carry it. Both are the engine's job now.
    """
    async for event in OrchestrationEngine.resume(run_id, human_response, server_module, images):
        if not _is_internal(event.get("type", "")):
            yield event


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _stream(source) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in iter_with_heartbeat(source):
                if isinstance(event, str):  # heartbeat comment — pass through
                    yield event
                    continue
                yield _sse(event)
                await asyncio.sleep(0)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            yield _sse({"type": "orchestration_error", "error": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/builder/chat")
async def builder_chat(request: BuilderChatRequest, http_request: Request):
    await reject_unseeable_images(request)
    return _stream(run_builder_stream(request, http_request.app.state.server_module))


@router.post("/api/builder/resume")
async def builder_resume(request: BuilderResumeRequest, http_request: Request):
    """Resume a paused builder run after the user answers."""
    return _stream(
        run_builder_resume_stream(
            request.run_id, request.response, http_request.app.state.server_module, request.images
        )
    )


async def run_builder_stream_compat(request, server_module):
    """SSE for the main chat page, which talks to a builder-typed agent.

    The chat page has no builder panel: no inline approval box, no canvas to
    open. So the run's events are flattened into the chat vocabulary, and a
    human step ends the turn with the question as an assistant message — the
    user answers by replying, which starts a fresh turn.
    """
    builder_req = BuilderChatRequest(
        message=request.message,
        history=getattr(request, "history_messages", []) or [],
        can_create_agents=True,
        model=getattr(request, "model", None),
        client_session_id=str(getattr(request, "session_id", "") or "chat"),
        # Chat has an attach button and the builder-typed agent is reached
        # through it, so dropping these here would lose the one attachment path
        # that already exists. Already validated on the way in as ChatRequest.
        images=list(getattr(request, "images", None) or []),
    )

    try:
        await reject_unseeable_images(builder_req)
    except HTTPException as exc:
        # This generator is already streaming SSE to the chat page, so the
        # refusal has to arrive as an event rather than as a status code.
        yield _sse({"type": "error", "message": str(exc.detail)})
        yield _sse({"type": "done"})
        return

    async for event in iter_with_heartbeat(run_builder_stream(builder_req, server_module)):
        if isinstance(event, str):  # heartbeat comment — pass through
            yield event
            continue
        etype = event.get("type")

        if etype == "step_start":
            yield _sse({"type": "status", "message": f"Running: {event.get('step_name', '')}"})

        elif etype == "thinking":
            yield _sse({"type": "status", "message": event.get("message", "Thinking…")})

        elif etype == "tool_execution":
            yield _sse({"type": "tool_execution", "tool_name": event.get("tool_name"), "args": event.get("args", {})})

        elif etype == "tool_result":
            yield _sse({"type": "tool_result", "tool_name": event.get("tool_name"), "preview": event.get("preview", "")[:200]})

        elif etype == "resource_saved":
            preview = f"✓ {event.get('kind', 'resource')} '{event.get('name') or event.get('id')}' {event.get('action', 'saved')}"
            yield _sse({"type": "tool_result", "tool_name": "resource_saved", "preview": preview})

        elif etype == "human_input_required":
            yield _sse({"type": "response", "content": event.get("prompt", ""), "intent": "chat", "data": None, "tool_name": None})
            yield _sse({"type": "done"})
            return

        elif etype == "orchestration_complete":
            summary = (event.get("final_state") or {}).get("summary")
            yield _sse({"type": "response", "content": summary or "Done.", "intent": "chat", "data": None, "tool_name": None})
            yield _sse({"type": "done"})

        elif etype in ("orchestration_error", "step_error"):
            yield _sse({"type": "error", "message": event.get("error", "Unknown error")})

        await asyncio.sleep(0)


# ── "See how I'm built" ──────────────────────────────────────────────────────

@router.get("/api/builder/legend")
async def builder_legend():
    """The builder's own shape, read from the definitions it ships with.

    Rendered rather than written down, so it cannot drift from the thing it
    describes the way a hand-typed diagram would.
    """
    bundle = bundled_definitions()
    orch = bundle["orchestration"]
    agents = {a["id"]: a for a in bundle["agents"]}
    by_id = {s["id"]: s for s in orch["steps"]}

    entry = by_id.get(orch["entry_step_id"], {})
    return {
        "name": orch["name"],
        "description": orch["description"],
        "step_count": len(orch["steps"]),
        "entry_step": {"id": entry.get("id"), "name": entry.get("name"), "type": entry.get("type")},
        "routes": [
            {"label": label, "to": by_id.get(target, {}).get("name", target),
             "description": (entry.get("route_descriptions") or {}).get(label, "")}
            for label, target in (entry.get("route_map") or {}).items()
        ],
        "human_gates": [
            {"id": s["id"], "name": s["name"], "asks": (s.get("human_fields") or [{}])[0].get("label", "")}
            for s in orch["steps"] if s.get("type") == "human"
        ],
        "agents": [
            {
                "role": agents[s["agent_id"]].get("system_role"),
                "name": agents[s["agent_id"]]["name"],
                "description": agents[s["agent_id"]].get("description", ""),
                "step": s["name"],
                "tools": agents[s["agent_id"]].get("tools", []),
            }
            for s in orch["steps"]
            if s.get("agent_id") and s["agent_id"] in agents
        ],
        "step_types": sorted({s["type"] for s in orch["steps"]}),
        # What the modal's read-only canvas draws: the steps as shipped, and
        # the names their `agent_id`s resolve to.
        "canvas": {
            "orchestration": {
                "id": orch.get("id") or "native_builder",
                "name": orch["name"],
                "entry_step_id": orch["entry_step_id"],
                "steps": orch["steps"],
            },
            "agents": [{"id": a["id"], "name": a["name"]} for a in bundle["agents"]],
        },
    }


@router.post("/api/builder/duplicate")
async def builder_duplicate():
    """Copy the builder into the workspace as an ordinary, editable orchestration.

    Fresh ids, agent references remapped to them, and the `system` marker
    stripped — so the copy is the customer's, not another instance of ours.
    """
    from core.builder_tools import _random_id
    from core.routes.agents import load_user_agents
    from core.routes.orchestrations import load_orchestrations
    from core.store.resources import save_agent, save_orchestration

    bundle = bundled_definitions()
    taken_agents = {a.get("id") for a in await load_user_agents()}
    taken_orchs = {o.get("id") for o in await load_orchestrations()}

    def _mint(prefix: str, taken: set[str]) -> str:
        new_id = _random_id(prefix, length=10)
        while new_id in taken:
            new_id = _random_id(prefix, length=10)
        taken.add(new_id)
        return new_id

    mapping: dict[str, str] = {}
    for agent in bundle["agents"]:
        copy = {k: v for k, v in agent.items() if k not in ("system", "system_role")}
        copy["id"] = _mint("agent_", taken_agents)
        copy["name"] = f"{agent['name']} (Copy)"
        mapping[agent["id"]] = copy["id"]
        # The orchestrator shell points at the orchestration; fixed up below.
        if copy.get("orchestration_id"):
            copy["orchestration_id"] = None
        await save_agent(copy)

    orch = {k: v for k, v in bundle["orchestration"].items() if k != "system"}
    orch["id"] = _mint("orch_", taken_orchs)
    orch["name"] = f"{bundle['orchestration']['name']} (Copy)"
    orch["steps"] = [
        {**s, "agent_id": mapping.get(s["agent_id"], s["agent_id"])} if s.get("agent_id") else s
        for s in orch["steps"]
    ]
    await save_orchestration(orch)

    return {
        "status": "created",
        "orchestration_id": orch["id"],
        "name": orch["name"],
        "agent_ids": list(mapping.values()),
    }

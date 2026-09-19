"""
REST API for messaging channel management.

Includes the webhook endpoints: the platform-specific Teams/WhatsApp pair that
predate webhook mode, and the generic verified endpoint every webhook-mode
channel (Telegram, Slack, Discord) receives through.
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from core.messaging import store as channel_store

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_manager(request: Request):
    """Get the MessagingManager from app state, or raise 503 if not available."""
    manager = getattr(request.app.state, "messaging_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Messaging is not enabled. Set messaging_enabled=true in setup.py and restart."
        )
    return manager


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/api/messaging/channels")
async def list_channels(request: Request, agent_id: str | None = None):
    """List all channels with live status, optionally filtered by agent.

    The agent filter goes through the assignment join (with its legacy-scalar
    fallback), and every channel carries its `agent_ids` so the UI can show
    who answers where without a request per channel.
    """
    manager = _get_manager(request)
    channels = await manager.get_status()
    if agent_id:
        assigned = {c["id"] for c in await channel_store.get_channels_for_agent(agent_id)}
        channels = [c for c in channels if c["id"] in assigned]
    for ch in channels:
        ch["agent_ids"] = await channel_store.get_agent_ids_for_channel(ch["id"])
    return channels


@router.post("/api/messaging/channels")
async def create_or_update_channel(request: Request):
    """Create or update a channel config. Does NOT auto-start the adapter."""
    body = await request.json()
    # Strip runtime-only fields from client payload
    body.pop("status", None)
    body.pop("last_error", None)
    saved = await channel_store.save_channel(body)
    return saved


@router.delete("/api/messaging/channels/{channel_id}")
async def delete_channel(channel_id: str, request: Request):
    """Stop and delete a channel."""
    manager = _get_manager(request)
    await manager.stop_channel(channel_id)
    deleted = await channel_store.delete_channel(channel_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"status": "deleted", "channel_id": channel_id}


# ── Assignments — channels are standalone; agents are assigned ────────────────

@router.get("/api/messaging/channels/{channel_id}/assignments")
async def list_assignments(channel_id: str, request: Request):
    _get_manager(request)
    if not await channel_store.get_channel(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    return await channel_store.list_assignments(channel_id)


@router.post("/api/messaging/channels/{channel_id}/assignments")
async def assign_agent(channel_id: str, request: Request):
    """Bind an agent to a channel. Body: `{agent_id, is_default?}`."""
    _get_manager(request)
    if not await channel_store.get_channel(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    body = await request.json()
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=422, detail="agent_id is required")
    return await channel_store.assign_channel(
        channel_id, agent_id, is_default=body.get("is_default")
    )


@router.put("/api/messaging/channels/{channel_id}/assignments")
async def set_assignments(channel_id: str, request: Request):
    """Replace a channel's agents in one call. Body: `{agent_ids, default_agent_id?}`.

    The whole-list write the settings screen uses: its chips describe a set, so
    the request describes a set. Every id is checked against the agent store
    first — an assignment naming an agent that does not exist is the bug D68
    already paid for once, where a dangling `agent_id` silently ran whichever
    agent happened to be first.
    """
    _get_manager(request)
    if not await channel_store.get_channel(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")

    body = await request.json()
    agent_ids = body.get("agent_ids")
    if not isinstance(agent_ids, list) or not all(isinstance(a, str) for a in agent_ids):
        raise HTTPException(status_code=422, detail="agent_ids must be a list of agent ids")

    from core.routes.agents import load_user_agents

    known = {a["id"] for a in await load_user_agents()}
    unknown = [a for a in agent_ids if a not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown agent(s): {', '.join(unknown)}",
        )

    default_agent_id = body.get("default_agent_id")
    if default_agent_id and default_agent_id not in agent_ids:
        raise HTTPException(
            status_code=422,
            detail="default_agent_id must be one of agent_ids",
        )

    return await channel_store.set_assignments(
        channel_id, agent_ids, default_agent_id=default_agent_id
    )


@router.delete("/api/messaging/channels/{channel_id}/assignments/{agent_id}")
async def unassign_agent(channel_id: str, agent_id: str, request: Request):
    _get_manager(request)
    removed = await channel_store.unassign_channel(channel_id, agent_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"status": "removed", "channel_id": channel_id, "agent_id": agent_id}


# ── Enable / Disable ──────────────────────────────────────────────────────────

@router.post("/api/messaging/channels/{channel_id}/enable")
async def enable_channel(channel_id: str, request: Request):
    """Start the adapter for a channel."""
    manager = _get_manager(request)
    ch = await channel_store.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Mark as enabled in store before starting
    ch["enabled"] = True
    await channel_store.save_channel(ch)
    await manager.start_channel(channel_id)

    updated = await channel_store.get_channel(channel_id)
    return updated


@router.post("/api/messaging/channels/{channel_id}/disable")
async def disable_channel(channel_id: str, request: Request):
    """Stop the adapter for a channel."""
    manager = _get_manager(request)
    ch = await channel_store.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    await manager.stop_channel(channel_id)

    ch["enabled"] = False
    await channel_store.save_channel(ch)
    return {"status": "stopped", "channel_id": channel_id}


# ── Test ──────────────────────────────────────────────────────────────────────

@router.post("/api/messaging/channels/{channel_id}/test")
async def test_channel(channel_id: str, request: Request):
    """
    Send a test greeting to verify credentials.
    For polling-based adapters (Telegram, Discord, Slack) this is a noop
    because we can't pick an arbitrary chat_id. We validate the bot token
    instead by checking the adapter is running.
    For webhook adapters (Teams, WhatsApp) we validate credential fields.
    """
    manager = _get_manager(request)
    ch = await channel_store.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    platform = ch.get("platform", "")
    adapter = manager._adapters.get(channel_id)

    if adapter is None:
        return {
            "status": "not_running",
            "message": "Adapter is not running. Click Enable first."
        }

    # For Telegram, verify the bot token by calling getMe
    if platform == "telegram":
        try:
            if adapter._app:
                me = await adapter._app.bot.get_me()
                return {
                    "status": "ok",
                    "message": f"Bot @{me.username} is alive and listening.",
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # For Discord
    if platform == "discord":
        if adapter._client and not adapter._client.is_closed():
            user = adapter._client.user
            return {
                "status": "ok",
                "message": f"Bot {user} is connected." if user else "Discord client connected.",
            }
        return {"status": "error", "message": "Discord client is not connected."}

    # Generic: if adapter is in _adapters, it started successfully
    return {
        "status": "ok",
        "message": f"{platform.title()} adapter is running.",
    }


# ── Generic webhook (webhook-mode Telegram / Slack / Discord) ─────────────────

@router.post("/api/messaging/{platform}/webhook/{channel_id}")
async def platform_webhook(platform: str, channel_id: str, request: Request):
    """One provider delivery for a webhook-mode channel.

    Signature first, on the exact bytes received, before anything is parsed —
    the channel's own credentials say what to check
    (`core/messaging/verify.py`). Then the handshakes platforms probe with
    (Slack's `url_verification`, Discord's PING) are answered from the payload
    alone, and a real message is dispatched **in the background**: Slack and
    Discord retry any delivery not answered within seconds, and an agent turn
    takes longer than that. The 200 means "received", not "answered" — the
    answer arrives in the conversation when the agent is done.
    """
    from core.messaging import inbound, verify

    if platform not in ("telegram", "slack", "discord"):
        raise HTTPException(status_code=404, detail="Unknown platform")

    manager = _get_manager(request)
    ch = await channel_store.get_channel(channel_id)
    if not ch or ch.get("platform") != platform:
        raise HTTPException(status_code=404, detail="Channel not found")

    body = await request.body()
    creds = ch.get("credentials") or {}
    if platform == "telegram":
        ok = verify.verify_telegram(
            creds.get("webhook_secret", ""),
            request.headers.get("x-telegram-bot-api-secret-token", ""),
        )
    elif platform == "slack":
        ok = verify.verify_slack(
            creds.get("signing_secret", ""),
            request.headers.get("x-slack-request-timestamp", ""),
            request.headers.get("x-slack-signature", ""),
            body,
        )
    else:
        ok = verify.verify_discord(
            creds.get("public_key", ""),
            request.headers.get("x-signature-ed25519", ""),
            request.headers.get("x-signature-timestamp", ""),
            body,
        )
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body is not JSON")

    # Platform handshakes — answered whether or not the channel is running,
    # because they arrive while someone is still setting the channel up.
    if platform == "slack":
        challenge = inbound.slack_url_verification(payload)
        if challenge is not None:
            return {"challenge": challenge}
    if platform == "discord" and inbound.discord_is_ping(payload):
        return inbound.DISCORD_PONG

    adapter = manager.get_adapter(channel_id, platform=platform)
    if adapter is None:
        # 200, not an error: a disabled channel must not put the platform into
        # a retry loop. Discord needs a real interaction response to say so.
        if platform == "discord":
            return {"type": 4, "data": {"content": "This channel is disabled."}}
        return {"status": "ignored", "reason": "channel not running"}

    from core.background import spawn_background
    spawn_background(adapter.handle_inbound(payload), name=f"messaging-inbound-{platform}")

    if platform == "discord":
        # "Thinking…" — buys the 15-minute follow-up window the adapter
        # answers through.
        return inbound.DISCORD_DEFERRED_REPLY
    return {"status": "ok"}


# ── Teams Webhook ─────────────────────────────────────────────────────────────

@router.post("/api/messaging/teams/webhook/{channel_id}")
async def teams_webhook(channel_id: str, request: Request):
    """Inbound webhook for Microsoft Teams Bot Framework."""
    manager = _get_manager(request)
    adapter = manager.get_teams_adapter(channel_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Teams channel not found or not running")

    # Delegate to the adapter for full Bot Framework processing
    from aiohttp.web import Request as AiohttpRequest, Response as AiohttpResponse
    # FastAPI/Starlette compatible shim is needed here.
    # For simplicity, we pass the raw request JSON to the adapter.
    body = await request.json()
    try:
        await adapter.process_request(body, None)
        return {"status": "ok"}
    except Exception as e:
        logger.error("[Teams Webhook] Error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

@router.get("/api/messaging/whatsapp/webhook/{channel_id}")
async def whatsapp_verify(channel_id: str, request: Request):
    """Meta webhook verification (GET with hub.challenge)."""
    manager = _get_manager(request)
    adapter = manager.get_whatsapp_adapter(channel_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="WhatsApp channel not found or not running")

    params = dict(request.query_params)
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    result = adapter.verify_webhook(mode, token, challenge)
    if result is None:
        raise HTTPException(status_code=403, detail="Verification failed")
    return PlainTextResponse(result)


@router.post("/api/messaging/whatsapp/webhook/{channel_id}")
async def whatsapp_webhook(channel_id: str, request: Request):
    """Inbound webhook for WhatsApp Cloud API (Meta)."""
    manager = _get_manager(request)
    adapter = manager.get_whatsapp_adapter(channel_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="WhatsApp channel not found or not running")

    body = await request.json()
    await adapter.process_webhook(body)
    return {"status": "ok"}

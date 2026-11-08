"""
The embeddable chat widget: one script tag drops an agent into another site.

The visitor route names — ``session``, ``refresh``, ``chat``, ``stream``,
``cancel``, ``history`` — are a contract shared with the cloud product, because the widget
client that calls them is the same file in both. It reaches a Next route handler
here and a control-plane route there, and the paths are what let one file do
both. Renaming one without the other is a 404 that only shows up in a browser.

Two route groups with very different callers, deliberately in one file so the
asymmetry is visible:

* ``/api/widgets*`` — the owner's CRUD, reached from the settings screen like
  every other management route.
* ``/api/widget/*`` — the *visitor* surface, reached from the embed page. Not a
  person, no session, no role.

**Nothing here is a new public hole.** Both groups sit under ``/api/`` and are
therefore gated by ``core.internal_auth.InternalTokenMiddleware`` exactly like
``/chat`` is: the only caller is our own Next app, which adds
``X-Synapse-Internal``. That is the whole reason the widget is an *iframe on our
own origin* rather than a cross-origin bundle — the browser inside it is
same-origin with the app, so nothing had to be opened up, and
GHSA-3j67-x3j8-r32x does not recur.

Two credentials therefore layer up, and both are required:

    X-Synapse-Internal   this request came through our frontend
    Bearer wgt1.…        which widget, which agent, which conversation, from where

The visitor token is minted by ``POST /api/widget/session`` — which the embed
*page* calls server-side, after checking the parent origin — and never by the
browser. See ``core.widget_token`` for why the claims are the state, and
``core.chat_widgets.origins`` for why the origin is the credential and the public
key is not.

**What a widget deliberately does not have**, each for the same reason — the
person on the other end is a stranger, not a member:

* *Only the tools the owner ticked.* A visitor's turn runs in a visitor scope
  (``core.visitor``): it is offered, and may run, only the widget's
  ``visitor_tools`` — nothing by default — in the agent itself and in every agent
  or step the turn reaches. Every tool an agent held used to be a stranger's
  too, ``run_bash`` and the file readers included (F280).
* *No memory.* The agent's memory is the owner's; a visitor neither reads it nor
  writes it (F276).
* *No tool-approval card.* Nobody here can answer one, so for a visitor a
  ``confirm`` is a ``deny`` in the engine; a pause that somehow happens anyway
  ends the turn with a sentence instead of showing a stranger a dialog with raw
  SQL in it.
* *No attachments.* Uploads need their own door with their own limits; anonymous
  storage is not something to add as a side effect of a chat box.
* *Stop and retry, yes.* A spinner a visitor cannot cancel is the exact complaint
  that started this work.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.chat_widgets import branding as _branding
from core.chat_widgets import limits as _limits
from core.chat_widgets import origins as _origins
from core.chat_widgets import service as _service
from core.chat_widgets import store as _store
from core.models import ChatRequest
from core import chat_turns
from core import visitor as _visitor
from core.routes import chat as _chat
from core.routes.chat import TURN_RUNNING, _read, _sse
from core.widget_token import REFRESH_GRACE_SECONDS, VisitorClaims, mint, refresh, verify

router = APIRouter()


def _secret() -> str:
    """The widget signing key, derived from the deployment's own secret.

    Derived rather than stored, and labelled rather than shared: the same trick
    the cloud's Telegram webhook uses. Nothing new to configure, nothing new to
    leak, and a widget token can never be replayed as anything else because no
    other consumer holds this exact key.
    """
    from core.config import get_or_create_jwt_secret

    root = get_or_create_jwt_secret()
    return hmac.new(root.encode(), b"chat-widget-visitor:v1", hashlib.sha256).hexdigest()


def _redis(request: Request):
    return getattr(request.app.state, "redis", None)


def _client_ip(request: Request) -> str:
    """The visitor's address, for the per-IP limit.

    The last `X-Forwarded-For` entry, matching what the Next proxy in front of
    this appends — the earlier entries are whatever the client sent and are not
    evidence of anything.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    client = request.client
    return client.host if client else "unknown"


async def _claims(authorization: str | None) -> VisitorClaims:
    """The visitor token, or 401. Never says *why* it failed."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    claims = verify(token, secret=_secret())
    if claims is None:
        raise HTTPException(status_code=401, detail="This chat session has expired. Reload the page.")
    return claims


#: The refusal a returning visitor gets once the owner points the widget at
#: another agent. The frame acts on it by itself (F278): it starts a new chat
#: through `/api/widget/restart`, which is why it carries a `code` — the same
#: status is also "a reply is still being written" (`TURN_RUNNING`).
UPDATED = "This chat has been updated. Reload the page."
UPDATED_CODE = "widget_updated"


class WidgetUpdated(HTTPException):
    """This conversation's agent is no longer the widget's (F278)."""

    def __init__(self) -> None:
        super().__init__(status_code=409, detail=UPDATED)


async def widget_updated_response(_request: Request, _exc: WidgetUpdated) -> JSONResponse:
    """`WidgetUpdated` as the frame reads it: the sentence, and which 409 it is."""
    return JSONResponse(status_code=409, content={"detail": UPDATED, "code": UPDATED_CODE})


async def _live_widget(claims: VisitorClaims) -> dict:
    """The widget the token names, still enabled.

    Re-read per request rather than trusted from the token: disabling a widget
    has to stop the conversations already running on it, and the token has no
    revocation of its own.
    """
    widget = await _store.get_widget(claims.widget_id)
    if widget is None or not widget.get("enabled", True):
        raise HTTPException(status_code=403, detail="This chat is not available.")
    if widget.get("agent_id") != claims.agent_id:
        # The owner re-pointed the widget at a different agent mid-conversation.
        # Refuse rather than silently switch: the transcript above belongs to the
        # agent that wrote it. The frame starts a new chat on this (F278).
        raise WidgetUpdated()
    return widget


# ── The visitor surface ─────────────────────────────────────────────────────

class SessionRequest(BaseModel):
    """What the embed page asks for once, server-side, per page load."""
    public_key: str = Field(min_length=1, max_length=200)
    #: The *parent* page's origin. Supplied by the embed route from the `Referer`
    #: it received — never from a query parameter the host page controls.
    origin: str = Field(min_length=1, max_length=500)
    #: Continue an existing conversation across a reload.
    session_id: str | None = Field(default=None, max_length=255)


@router.post("/api/widget/session")
async def open_session(body: SessionRequest, request: Request):
    """Mint a visitor token for one page load, if that page may have one.

    This is where the origin is checked. Everything downstream trusts the
    signature instead, because the signature is only ever produced here.
    """
    if not await _limits.check_ip(_redis(request), _client_ip(request), _limits.SESSION_PER_IP):
        raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.")

    widget = await _store.get_by_public_key(body.public_key)
    if widget is None:
        raise HTTPException(status_code=404, detail="Unknown widget key.")
    if not widget.get("enabled", True):
        raise HTTPException(status_code=403, detail="This chat is switched off.")

    if not _origins.matches(body.origin, widget.get("allowed_origins")):
        # Named plainly, and on purpose. The person who sees this is the customer
        # who just pasted the snippet onto a site they forgot to list — telling
        # them which origin was refused is the difference between a two-second
        # fix and a support conversation. It reveals nothing: they hold the key.
        raise HTTPException(
            status_code=403,
            detail=(
                f"{_origins.normalise(body.origin) or body.origin} is not an allowed origin "
                "for this widget. Add it in Settings → Widgets."
            ),
        )

    agent_id = widget.get("agent_id") or ""
    if not agent_id:
        raise HTTPException(status_code=409, detail="This widget has no agent yet.")

    token, session_id = mint(
        widget["id"], agent_id, origin=body.origin, secret=_secret(), session_id=body.session_id
    )
    return {
        "token": token,
        "session_id": session_id,
        "widget": _store.public_view(widget),
    }


@router.get("/api/widget/{public_key}/csp")
async def widget_csp(public_key: str):
    """The `frame-ancestors` directive for this widget's embed page.

    Its own endpoint because a CSP header has to be set before the page renders,
    and in Next that means middleware — which cannot see what the page later
    learns. One collection read, and the answer is the same for every visitor, so
    it caches at the edge of the request rather than per person.

    An unknown key gets `'none'`, identical to a widget with an empty allow-list.
    The browser's behaviour is then the same in both cases — nothing renders —
    and the page itself says which it was, to the one person who can act on it.
    """
    widget = await _store.get_by_public_key(public_key)
    allowed = (widget or {}).get("allowed_origins") if (widget or {}).get("enabled", True) else []
    return {"frame_ancestors": _origins.frame_ancestors(allowed)}


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


@router.post("/api/widget/refresh")
async def refresh_session(authorization: str | None = Header(default=None)):
    """A fresh token for a conversation that outlived the first one.

    Not for a conversation whose agent the widget no longer answers with
    (F278): a reload used to refresh that token and throw away the fresh one
    the page had just minted, so reloading never got the visitor out.
    """
    token = _bearer(authorization)
    claims = verify(token, secret=_secret(), grace=REFRESH_GRACE_SECONDS)
    if claims is not None:
        widget = await _store.get_widget(claims.widget_id)
        if widget is not None and widget.get("agent_id") and widget.get("agent_id") != claims.agent_id:
            raise WidgetUpdated()
    fresh = refresh(token, secret=_secret())
    if fresh is None:
        raise HTTPException(status_code=401, detail="This chat session has expired. Reload the page.")
    return {"token": fresh}


@router.post("/api/widget/restart")
async def restart_session(request: Request, authorization: str | None = Header(default=None)):
    """A new conversation on this widget, with the agent it answers with now (F278).

    What the frame calls when its conversation's agent is no longer the
    widget's. The old token is the credential, as it is for `/refresh`: its
    signature proves the widget and the origin `/session` checked. The origin is
    checked again here, against the widget's list as it stands, and the call
    counts against the same per-address limit as a new session.
    """
    if not await _limits.check_ip(_redis(request), _client_ip(request), _limits.SESSION_PER_IP):
        raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.")
    claims = verify(_bearer(authorization), secret=_secret(), grace=REFRESH_GRACE_SECONDS)
    if claims is None:
        raise HTTPException(status_code=401, detail="This chat session has expired. Reload the page.")
    widget = await _store.get_widget(claims.widget_id)
    if (widget is None or not widget.get("enabled", True)
            or not _origins.matches(claims.origin, widget.get("allowed_origins"))):
        raise HTTPException(status_code=403, detail="This chat is not available.")
    agent_id = widget.get("agent_id") or ""
    if not agent_id:
        raise HTTPException(status_code=409, detail="This widget has no agent yet.")
    token, session_id = mint(widget["id"], agent_id, origin=claims.origin, secret=_secret())
    return {"token": token, "session_id": session_id, "widget": _store.public_view(widget)}


class WidgetMessage(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@router.post("/api/widget/chat")
async def widget_chat(
    body: WidgetMessage,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """One turn, streamed back on the connection that asked for it — which can
    go away without taking the turn with it; `/api/widget/stream` reads it again.

    The agent comes from the *token*, never from the body — which is the one
    thing `api_v1`'s `_resolve_agent_for_api` gets wrong for this purpose: it
    falls back to the first configured agent when the identifier does not match,
    so a typo reaches an agent nobody meant to publish.
    """
    import core.server as _server

    claims = await _claims(authorization)
    widget = await _live_widget(claims)
    redis = _redis(request)

    # Claimed before the limits are counted, so a message refused because a
    # reply is still being written costs the visitor nothing.
    try:
        with chat_turns.claim(claims.session_id, message=body.message,
                              agent_key=claims.agent_id or "default") as turn:
            if not await _limits.check_ip(redis, _client_ip(request)):
                raise HTTPException(status_code=429, detail="Too many messages. Try again in a few minutes.")
            if not await _limits.check_daily(redis, widget["id"], widget.get("daily_message_cap")):
                raise HTTPException(
                    status_code=429,
                    detail="This chat has reached its limit for today. Please try again tomorrow.",
                )

            await _done(_service.refuse_an_unpublishable_agent(claims.agent_id))

            visitor_turn = ChatRequest(
                message=body.message,
                session_id=claims.session_id,
                agent_id=claims.agent_id,
            )
            # Started here, not inside the response: a visitor clicking through
            # the host site reloads this frame, and that must not end the turn
            # (F49). A visitor is not a member: their `@[path]` must not read
            # the vault, and their turn holds only the tools the owner ticked —
            # the task copies this context, so the scope outlives the block.
            scope = _visitor.VisitorScope("widget", frozenset(_visitor.clean_tools(widget.get("visitor_tools"))))
            with _visitor.scope(scope):
                turn.start(_chat._event_stream(visitor_turn, _server, None, expand_mentions=False))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return _sse(_for_a_visitor(_read(turn)))


@router.get("/api/widget/stream")
async def widget_stream(cursor: str | None = None, authorization: str | None = Header(default=None)):
    """Read this conversation's turn again, from `cursor` — through the same filter.

    What a reloaded frame opens when its history says a turn is still running,
    and what a broken stream reconnects to. The conversation comes from the
    signed token, never the URL, so a visitor can only ever read their own.
    """
    claims = await _claims(authorization)
    await _live_widget(claims)
    turn = chat_turns.find(claims.session_id)
    if turn is None:
        return JSONResponse(status_code=404, content={"detail": "No reply is being written."})
    try:
        after = chat_turns.parse_cursor(turn, cursor)
    except chat_turns.TurnGone:
        return JSONResponse(status_code=410, content={"detail": "That reply has finished."})
    return _sse(_for_a_visitor(_read(turn, after)))


#: The only events that may leave this building.
#:
#: **An allowlist, not a denylist, and that distinction is the whole point.** The
#: workspace timeline shows an agent's reasoning, its tool calls with their
#: arguments, its task list and — in `_log_llm_call` — the *full assembled
#: prompt and the agent's system prompt*. All of that is fine for a member
#: entitled to see how their own agent works. Streamed to an anonymous visitor on
#: somebody else's website it is the customer's intellectual property, their
#: retrieved documents, and whatever a tool result happened to contain.
#:
#: A denylist would have to be updated every time the engine learns a new event,
#: and would be wrong by default until somebody noticed. This way a new event is
#: invisible to a widget until someone decides it should not be.
#:
#: Everything here is something the widget actually renders: whether it is
#: working, what it finally said, and how it ended.
_VISITOR_EVENTS = frozenset((
    "status", "thinking", "final", "response",
    "cancelled", "error", "orchestration_error", "done",
))

#: `human_input_required` is the tool-approval card and the workflow pause. Both
#: ask a *member* to decide something; neither has an answer a stranger on a
#: marketing page could give. A visitor's `confirm` is a `deny` in the engine
#: (`core.visitor`) — this is the backstop for a pause the turn reaches some other
#: way, a workflow's human step among them.
_PAUSE_EVENTS = frozenset(("human_input_required",))

_PAUSED_MESSAGE = (
    "I need someone from the team to confirm the next step, so I can't finish this here. "
    "Please get in touch and someone will pick it up."
)


async def _for_a_visitor(stream):
    """The turn's events, narrowed to what a stranger may see.

    Two jobs, and they are separate on purpose. The allowlist decides what leaves
    the building; the pause branch decides how a turn that wants a human decision
    ends, since the visitor cannot give one.

    Anything not in `_VISITOR_EVENTS` is dropped silently rather than replaced
    with a placeholder — the widget is not a debugging surface, and a gap in the
    timeline is not something a visitor can act on.
    """
    async for chunk in stream:
        if not isinstance(chunk, str):
            continue
        if chunk.lstrip().startswith(":"):
            # Heartbeat comments keep the connection alive and carry nothing.
            yield chunk
            continue

        # A frame may carry its cursor (`id: …`) before its `data:` line. Only
        # those two are read; anything else in a frame is dropped, so nothing
        # rides past the allowlist on a line of its own.
        cursor, payload = None, []
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                cursor = line[4:]
            elif line.startswith("data: "):
                payload.append(line[6:])
        if not payload:
            continue
        try:
            event = json.loads("\n".join(payload))
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue

        shown, ends = _visitor_view(event)
        prefix = f"id: {cursor}\n" if cursor else ""
        for item in shown:
            yield f"{prefix}data: {json.dumps(item)}\n\n"
        if ends:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return


def _visitor_view(event: dict) -> tuple[list[dict], bool]:
    """One event as a stranger may see it, and whether their turn ends there.

    Closed, not parked, on a pause: the pause is already written to the session
    and a member can see it in the workspace. The visitor gets a sentence and a
    finished conversation instead of a spinner waiting on a dialog that will
    never be shown to them.
    """
    kind = event.get("type")
    if kind in _PAUSE_EVENTS:
        return [{"type": "response", "content": _PAUSED_MESSAGE}], True
    if kind not in _VISITOR_EVENTS:
        return [], False
    # Re-serialised from a narrowed dict rather than passed through, so a
    # field added to an allowed event cannot ride along unread.
    return [_visitor_fields(event)], False


def _visitor_fields(event: dict) -> dict:
    """One allowed event, reduced to the keys the widget reads.

    `status` and `thinking` keep no message: the engine's own words describe the
    machinery ("calling scrape_url", "turn 3 of 8"), and the widget renders a
    single "Thinking…" from the fact of the event rather than its content.
    """
    kind = event.get("type")
    if kind in ("final", "response"):
        # Without its [K#] labels: the widget shows no Sources to read them by (D161).
        from core.knowledge.citations import strip_labels

        return {"type": kind, "content": strip_labels(event.get("response") or event.get("content") or "")}
    return {"type": kind}


@router.post("/api/widget/cancel")
async def widget_cancel(request: Request, authorization: str | None = Header(default=None)):
    """Stop the turn this conversation is running.

    The session id comes from the signed token rather than the path, so a visitor
    can only ever stop their own turn — there is no id to guess.
    """
    claims = await _claims(authorization)
    from core.routes.chat import cancel_chat

    return await cancel_chat(claims.session_id)


@router.get("/api/widget/history")
async def widget_history(authorization: str | None = Header(default=None)):
    """The transcript, so a reload does not lose the conversation.

    `pending_approval` is deliberately **not** returned. A widget never shows an
    approval card, so a parked turn must not come back as one — see the module
    docstring.
    """
    claims = await _claims(authorization)
    await _live_widget(claims)
    from core.store import sessions as _sessions

    row = await _sessions.get(claims.session_id, claims.agent_id)
    # What the visitor wrote and what the agent answered — nothing else (F325).
    # A saved turn also holds its tools' calls and results and the model's
    # reasoning, which the visitor's stream never shows (`_VISITOR_EVENTS`);
    # cloud's widget history answers the same two keys.
    # An answer without its [K#] labels, as the stream showed it (D161).
    from core.knowledge.citations import strip_labels

    messages = [
        {"role": m.get("role"),
         "content": strip_labels(m.get("content") or "") if m.get("role") == "assistant" else m.get("content") or ""}
        for m in (row or {}).get("messages") or []
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]
    # A turn still being written is not in `messages` yet — a turn is saved when
    # it ends. `running` is what lets a reloaded frame show the question and
    # follow the reply (`/api/widget/stream`).
    turn = chat_turns.live(claims.session_id)
    running = {"message": turn.message, "cursor": turn.cursor(0)} if turn else None
    return {"session_id": claims.session_id, "messages": messages, "running": running}


# ── The owner's surface ─────────────────────────────────────────────────────

class WidgetBody(BaseModel):
    name: str = Field(default="", max_length=120)
    agent_id: str = Field(default="", max_length=255)
    allowed_origins: list[str] = Field(default_factory=list, max_length=50)
    enabled: bool = True
    greeting: str = Field(default="", max_length=500)
    accent: str = Field(default="", max_length=32)
    position: str = Field(default="right", max_length=10)
    launcher_label: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=80)
    daily_message_cap: int = Field(default=500, ge=0, le=1_000_000)
    #: The tools a visitor's turn may hold (F280). Nothing unless ticked; cleaned
    #: by the store (`core.visitor.clean_tools`), never shown to a visitor.
    visitor_tools: list[str] = Field(default_factory=list, max_length=_visitor.MAX_TOOLS)

    # Branding. Every one of these is a closed vocabulary validated in
    # `core.chat_widgets.branding`; the model only bounds the length so a
    # megabyte of junk is refused before it reaches the validator.
    font: str = Field(default="system", max_length=20)
    theme: str = Field(default="auto", max_length=10)
    header_style: str = Field(default="accent", max_length=10)
    launcher: str = Field(default="bubble", max_length=10)
    #: The logo is **not** here. It arrives as bytes on its own route, because a
    #: JSON body carrying a 128 KB data URI on every rename is a bad trade, and
    #: because bytes are the only thing we can check the magic numbers of.


async def _done(call):
    """A service call, its refusal the route's answer: the same words and status as before (F1262, one place)."""
    try:
        return await call
    except _service.WidgetRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


@router.get("/api/widgets")
async def list_widgets():
    return await _service.list_widgets()


@router.get("/api/widgets/visitor-tools")
async def visitor_tool_choices(agent_id: str):
    """What an owner can let a widget's visitors use (`service.visitor_tool_choices`)."""
    return {"tools": await _done(_service.visitor_tool_choices(agent_id))}


@router.post("/api/widgets")
async def create_widget(body: WidgetBody):
    return await _done(_service.create(body.model_dump()))


@router.put("/api/widgets/{widget_id}")
async def update_widget(widget_id: str, body: WidgetBody):
    return await _done(_service.replace(widget_id, body.model_dump()))


@router.delete("/api/widgets/{widget_id}")
async def remove_widget(widget_id: str):
    await _done(_service.delete(widget_id))
    return {"deleted": True}


@router.post("/api/widgets/{widget_id}/rotate-key")
async def rotate_key(widget_id: str):
    """Issue a new public key (`service.rotate_key`)."""
    return await _done(_service.rotate_key(widget_id))


@router.post("/api/widgets/{widget_id}/logo")
async def upload_logo(widget_id: str, file: UploadFile = File(...)):
    """Replace this widget's logo (`service.set_logo`): at most one byte over the cap is read."""
    await _done(_service.get(widget_id))
    raw = await file.read(_branding.MAX_LOGO_BYTES + 1)
    return {"logo": await _done(_service.set_logo(widget_id, raw))}


@router.delete("/api/widgets/{widget_id}/logo")
async def remove_logo(widget_id: str):
    await _done(_service.remove_logo(widget_id))
    return {"logo": ""}


@router.post("/api/widgets/{widget_id}/preview")
async def preview_widget(widget_id: str):
    """A token for the owner's own preview, inside the app.

    The origin check is skipped because there is nothing to check: this route is
    already behind the internal token, so the caller is the workspace itself. The
    minted token records `app-preview` as its origin, so a preview is never
    mistaken for real traffic when reading a log.
    """
    widget = await _store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(status_code=404, detail="No such widget.")
    agent_id = widget.get("agent_id") or ""
    if not agent_id:
        raise HTTPException(status_code=409, detail="This widget has no agent yet.")
    token, session_id = mint(widget["id"], agent_id, origin="app-preview", secret=_secret())
    return {"token": token, "session_id": session_id, "widget": _store.public_view(widget)}

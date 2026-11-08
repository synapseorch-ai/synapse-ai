"""Models a provider said it no longer serves this workspace (F1213, CAP3).

A provider can list a model and still refuse it: Google's model list showed `gemini-2.5-flash-lite` while a call
answered 404 "This model … is no longer available to new users". Synapse AI's builder picks the lowest-cost model
that fits an agent (F966) from the workspace's choices, so it picked that one, and CAP3's stage 0 (10-29) saw the
agent fail its first call. Nothing remembered the refusal: the next build would pick it again.

Now a call the provider refuses because the model is gone (`model_gone`) marks that model unavailable for the
workspace, with the provider's own sentence. `model_choices.choices()` (the builder's menu and the agent editor's
picker read it) and `/api/models` (the Models screen, which says why) leave it out.

Some retirements the product knows before any call (F1299, CAP3's cap-01 A2): a provider's own list still names a
model it refuses to new callers, so a new workspace's first build picked it. `RETIRED` is that one list, each with its
provider and the provider's own words: `retired_for(provider)` is read wherever a provider's models are offered
(`model_choices.choices()`, `/api/models` and its fallback lists, cloud's catalogue), for that provider only, and it
never lapses or clears. Its rate stays priced (`model_pricing`): an agent already set to it still calls it.

A mark always has a way back (8a's review): any successful call to that model clears it; the Models screen's "Try
again" clears it (`clear`); it lapses after `KEEP_DAYS`. A local provider's model is never marked: Ollama's or a
local server's "model not found" means "not pulled yet", and a wrong base URL answers the same. Never a reason to fail
a call: every read and write here is best-effort.
"""
from __future__ import annotations

import ipaddress
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from core.log import get_logger

_log = get_logger(__name__)

COLLECTION = "model_availability"
KEEP_DAYS = 14
#: How long this process trusts its list of marked models before reading the store again (a success's check).
_CACHE_S = 60

#: Models their provider retired for new callers, known before any call fails (F1299): `{model: {provider, reason,
#: since}}`, the reason the provider's own sentence. Offered nowhere for that provider; still priced.
RETIRED: dict[str, dict[str, str]] = {
    "gemini-2.5-flash-lite": {
        "provider": "gemini", "since": "2026-10-29",
        "reason": "This model models/gemini-2.5-flash-lite is no longer available to new users.",
    },
    # CAP3's cap-04 (F1369): still listed by OpenAI, refused to a workspace's own key.
    "gpt-4.1-nano": {
        "provider": "openai", "since": "2026-11-04",
        "reason": "The model `gpt-4.1-nano` has been deprecated.",
    },
}

#: The stand-ins said in this step (F1369): a call refused because its model is gone that answered from another of
#: the same provider. The engine reads them after each step (`noting_stand_ins`); a call outside one says nothing.
_STAND_INS: ContextVar[list[str] | None] = ContextVar("model_stand_ins", default=None)


@contextmanager
def noting_stand_ins():
    """The stand-ins said while it is open, in words: `with noting_stand_ins() as said: …`."""
    said: list[str] = []
    token = _STAND_INS.set(said)
    try:
        yield said
    finally:
        _STAND_INS.reset(token)


def begin_stand_ins() -> list[str]:
    """A step's own list of stand-ins said, from here on in this context (the engine's, at each step's start)."""
    said: list[str] = []
    _STAND_INS.set(said)
    return said


def said_stand_in(refused: str, used: str) -> None:
    """Say that a call to `refused`, which its provider no longer serves, answered from `used` (F1369)."""
    said = _STAND_INS.get()
    if said is not None:
        said.append(f"`{refused}` is no longer served by its provider, so this step used `{used}` instead, from the "
                    "same provider and keys. Set the model on the step or its agent to keep that one.")


def retired_for(provider: str) -> dict[str, dict]:
    """The shipped retirements of `provider`'s models, as `gone_from` reads a mark: `{model: {model, reason}}`."""
    return {m: {"model": m, "reason": r["reason"], "at": None, "shipped": True}
            for m, r in RETIRED.items() if r["provider"] == provider}


#: Providers whose models live on the person's own machine or network: never marked.
LOCAL_PROVIDERS = frozenset({"ollama", "local_compatible"})

#: A provider's words for "this model is gone", whatever the status: Gemini's "no longer available", OpenAI's "does
#: not exist", a decommissioned, retired or shut-down model.
_GONE = re.compile(r"no longer available|does not exist|decommissioned|has been (deprecated|retired|shut down)|"
                   r"unknown model", re.I)
#: On a 404 only: "not found" (Gemini's NOT_FOUND, OpenAI's `model_not_found`, Anthropic's `not_found_error`). A 400
#: that says a model does not support something (tools, images) is about the request, never this.
_NOT_FOUND = re.compile(r"not[ _]found", re.I)


def model_gone(err: BaseException) -> str | None:
    """The provider's sentence when a failed call says its model is gone; None for any other failure.

    Only a refusal that names a model: a 404 for something else (a file, an assistant) is not this."""
    text = str(getattr(err, "detail", None) or err or "")
    status = getattr(err, "status", None)
    if status is None and (said := re.search(r"\b(400|404)\b", text)):
        status = int(said.group(1))  # an adapter that writes its status into its words only
    if status not in (400, 404) or not re.search(r"\bmodels?\b", text, re.I):
        return None
    if _GONE.search(text) or (status == 404 and _NOT_FOUND.search(text)):
        return _sentence(text)
    return None


def _sentence(text: str) -> str:
    """The provider's own sentence, for the Models screen: the `message` its error body carries, else the text."""
    said = re.search(r"""['"]message['"]\s*:\s*(['"])(.+?)\1""", text)
    return (said.group(2) if said else text).strip()[:300]


def _plain(text: str) -> str:
    return "".join(c for c in (text or "").casefold() if c.isalnum())


def same_model(listed: str, called: str) -> bool:
    """`called` (as an agent names it) is `listed` (as a provider lists it), a provider's prefix aside:
    `gemini-2.5-flash-lite` is `models/gemini-2.5-flash-lite` and `gemini.gemini-2.5-flash-lite`."""
    a, b = _plain(listed), _plain(called)
    if not a or not b:
        return False
    tails = {_plain(listed[i + 1:]) for i, c in enumerate(listed) if c in "./"}
    return a == b or b in tails or a in {_plain(called[i + 1:]) for i, c in enumerate(called) if c in "./"}


def _private_host(host: str | None) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if not host or host == "localhost" or host.endswith(".localhost") or host == "host.docker.internal":
        return bool(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_local(provider: str | None, settings: dict | None = None) -> bool:
    """Whether a provider's models are the person's own: Ollama, a local server, or an OpenAI-compatible one at a
    loopback or private address."""
    if provider in LOCAL_PROVIDERS:
        return True
    if provider == "openai_compatible":
        return _private_host(urlparse(str((settings or {}).get("openai_compatible_base_url") or "")).hostname)
    return False


#: {tenant: (when read, the marked models)}: a success checks this, not the store.
_marked_cache: dict[str, tuple[float, set[str]]] = {}


def _tenant() -> str:
    from core.tenancy import get_tenant

    try:
        return get_tenant()
    except Exception:  # noqa: BLE001
        return ""


async def _marked() -> set[str]:
    tenant = _tenant()
    at, ids = _marked_cache.get(tenant, (0.0, set()))
    if time.time() - at > _CACHE_S:
        ids = set(await unavailable())
        _marked_cache[tenant] = (time.time(), ids)
    return ids


def _forget_cache() -> None:
    _marked_cache.pop(_tenant(), None)


async def mark(model: str, reason: str) -> None:
    from core.store.collections import put_item

    try:
        await put_item(COLLECTION, model, {"model": model, "reason": reason, "at": time.time()})
        _forget_cache()
        _log.warning("model %s marked unavailable: %s", model, reason[:160])
    except Exception:  # noqa: BLE001 — remembering a refusal never fails the call that met it
        _log.warning("model %s could not be marked unavailable", model, exc_info=True)


async def clear(model: str) -> bool:
    """The mark on `model` (as listed or as called) goes: "Try again", or a call to it that worked. True when there
    was one."""
    from core.store.collections import delete_item

    gone = False
    try:
        for marked in list(await unavailable()):
            if same_model(marked, model):
                gone = await delete_item(COLLECTION, marked) or gone
    except Exception:  # noqa: BLE001
        _log.warning("model %s mark not cleared", model, exc_info=True)
    _forget_cache()
    return gone


async def note_failure(model: str | None, err: BaseException, *, provider: str | None = None,
                       settings: dict | None = None) -> None:
    """Called with every failed model call: marks the model when the provider said it is gone, never a local one."""
    if not model or is_local(provider, settings):
        return
    reason = model_gone(err)
    if reason:
        await mark(model, reason)


async def note_success(model: str | None) -> None:
    """Called with every model call that worked: a marked model that answered is offered again."""
    if not model:
        return
    try:
        if any(same_model(m, model) for m in await _marked()):
            await clear(model)
    except Exception:  # noqa: BLE001
        _log.warning("model %s success not noted", model, exc_info=True)


async def unavailable() -> dict[str, dict]:
    """{model: {reason, at}} for the marks that have not lapsed; {} when they cannot be read."""
    from core.store.collections import load

    try:
        rows = await load(COLLECTION)
    except Exception:  # noqa: BLE001
        _log.warning("model availability not read", exc_info=True)
        return {}
    cutoff = time.time() - KEEP_DAYS * 86400
    return {r["model"]: r for r in rows if isinstance(r, dict) and r.get("model") and float(r.get("at") or 0) >= cutoff}


def gone_from(models: list[str], gone: dict[str, dict]) -> tuple[list[str], list[dict]]:
    """`models` without the unavailable ones, and those left out with their reasons."""
    kept, left = [], []
    for m in models:
        hit = next((g for g in gone.values() if same_model(m, g["model"])), None)
        if hit:
            left.append({"model": m, "reason": hit.get("reason") or "", "at": hit.get("at")})
        else:
            kept.append(m)
    return kept, left

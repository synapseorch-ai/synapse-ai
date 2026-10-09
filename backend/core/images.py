"""
What counts as an image the agent can be sent.

There was no answer to that question anywhere. `ChatRequest.images` was a bare
`list[str]`; the "max 5 per request" in its comment was enforced only by `[:5]`
slicing inside each provider's content builder, so a caller reaching the API
directly could send five hundred. Nothing checked the size, so a 20 MB PNG
became ~27 MB of base64 in a JSON body, and on the worker path went into Redis
as a job argument. And nothing checked the type: the `mime` was taken from the
data URI's own header and handed to Anthropic as `media_type`, which is a
caller-controlled string reaching a provider unvalidated.

The limits are the intersection of what Anthropic, Bedrock Converse and Gemini
accept, so an image that passes here cannot then fail at a provider for being
too big — a rejection the person can act on, rather than a turn that dies
several seconds later with somebody else's error message.

The type is decided by the bytes, not by the header. A `.txt` renamed `.png` is
the cheap version of that mistake; a header saying `image/png` in front of
something else is the deliberate one, and both end the same way — a provider
error blamed on the agent.
"""
from __future__ import annotations

import base64
import binascii

from core.providers.registry import provider_for

#: Every provider that can see an image accepts these four and no others.
ALLOWED_MIME = ("image/png", "image/jpeg", "image/gif", "image/webp")

MAX_IMAGES = 5
#: Per image, decoded. Anthropic's own limit is 5 MB and Bedrock Converse's is
#: smaller still; this is the number a person can be told.
MAX_BYTES = 5 * 1024 * 1024
#: Per request. Five 5 MB images is 25 MB of decoded payload and more than that
#: on the wire, which is past what Gemini accepts inline.
MAX_TOTAL_BYTES = 20 * 1024 * 1024

#: Leading bytes → mime. WEBP is "RIFF????WEBP", so it is matched in two parts.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class ImageRejected(ValueError):
    """An attachment that cannot be sent, with the reason to show a person."""


def sniff(raw: bytes) -> str | None:
    """The mime these bytes actually are, or None if it is not an image."""
    for prefix, mime in _MAGIC:
        if raw.startswith(prefix):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


#: JPEG's start-of-frame markers, which carry the size: every SOFn but DHT (C4),
#: JPG (C8) and DAC (CC), which share the range.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def dimensions(raw: bytes) -> tuple[int, int] | None:
    """An image's width and height read from its header, or None when it has none.

    `sniff` reads the first bytes and nothing after them, so a PNG signature
    followed by zeros, or a file cut short after its first bytes, passed as an
    image and reached the provider — Bedrock answered `ValidationException`
    and the engine retried it for a minute and a half (F410). Every format here
    says how big it is in its first few hundred bytes; a file that does not is
    damaged. Not a full decode: a body truncated after a sound header still
    passes, and the provider's 400 says so.
    """
    import struct

    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(raw) < 24 or raw[12:16] != b"IHDR":
            return None
        size = struct.unpack(">II", raw[16:24])
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        if len(raw) < 10:
            return None
        size = struct.unpack("<HH", raw[6:10])
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind, body = raw[12:16], raw[20:]
        if kind == b"VP8 " and len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", body[6:10])
            size = (width & 0x3FFF, height & 0x3FFF)
        elif kind == b"VP8L" and len(body) >= 5 and body[0] == 0x2F:
            bits = int.from_bytes(body[1:5], "little")
            size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        elif kind == b"VP8X" and len(body) >= 10:
            size = (int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1)
        else:
            return None
    elif raw.startswith(b"\xff\xd8"):
        size = _jpeg_size(raw)
        if size is None:
            return None
    else:
        return None
    return size if size[0] > 0 and size[1] > 0 else None


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """Walk JPEG's markers to the first start-of-frame, which holds the size."""
    at = 2
    while at + 4 <= len(raw):
        if raw[at] != 0xFF:
            return None
        marker = raw[at + 1]
        if marker == 0xFF:                      # fill byte before a marker
            at += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:   # standalone: no length
            at += 2
            continue
        if marker == 0xDA:                      # start of scan with no frame before it
            return None
        length = int.from_bytes(raw[at + 2:at + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if at + 9 > len(raw):
                return None
            height = int.from_bytes(raw[at + 5:at + 7], "big")
            width = int.from_bytes(raw[at + 7:at + 9], "big")
            return width, height
        at += 2 + length
    return None


def parse_data_uri(data_uri: str) -> tuple[str, str]:
    """Split a data URI into (mime, base64), rejecting anything else.

    The permissive version of this — assume `image/png` for a bare string, take
    the header's word for the type — is what let an arbitrary `media_type`
    through to a provider. It also raised `ValueError` on a `data:` string with
    no comma, from inside the ReAct loop.
    """
    if not isinstance(data_uri, str) or not data_uri.strip():
        raise ImageRejected("That attachment was empty.")
    if not data_uri.startswith("data:"):
        raise ImageRejected("Attachments must be sent as data URIs.")
    head, _, b64 = data_uri.partition(",")
    if not b64:
        raise ImageRejected("That attachment was not a readable data URI.")
    mime = head.split(";")[0][len("data:"):].strip().lower()
    return mime, b64


def validate(images: list[str] | None) -> list[str]:
    """Check a request's attachments, or say why not.

    Returns the list unchanged when it passes — the transport does not change,
    only whether we agree to carry it.
    """
    if not images:
        return []
    if len(images) > MAX_IMAGES:
        raise ImageRejected(f"Up to {MAX_IMAGES} images per message; that had {len(images)}.")

    total = 0
    for index, item in enumerate(images, start=1):
        mime, b64 = parse_data_uri(item)
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ImageRejected(f"Image {index} was not valid base64.") from None

        actual = sniff(raw)
        if actual is None:
            raise ImageRejected(
                f"Image {index} is not a PNG, JPEG, GIF or WebP. "
                "Renaming a file does not change what is inside it."
            )
        if actual not in ALLOWED_MIME:
            raise ImageRejected(f"Image {index} is a {actual}, which no provider here can read.")
        if mime and mime != actual:
            # Not fatal — a browser occasionally mislabels — but the bytes win,
            # because the label is what reaches the provider.
            pass
        if len(raw) > MAX_BYTES:
            raise ImageRejected(
                f"Image {index} is {len(raw) // (1024 * 1024)} MB; the limit is "
                f"{MAX_BYTES // (1024 * 1024)} MB."
            )
        if dimensions(raw) is None:
            raise ImageRejected(
                f"Image {index} could not be read: it says it is a {actual.split('/')[1].upper()} "
                "but has no size in it, so it is damaged or cut short."
            )
        total += len(raw)

    if total > MAX_TOTAL_BYTES:
        raise ImageRejected(
            f"Those images come to {total // (1024 * 1024)} MB together; the limit is "
            f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB."
        )
    return list(images)


def model_can_see_images(model: str) -> bool:
    """Whether sending an image to this model does anything.

    The provider table says (`Capabilities.images`). Some providers cannot see
    an image and quietly drop it: DeepSeek and HuggingFace log a warning and
    discard; the CLI providers discard with no warning at all. In every case
    the person watched their attachment go into the composer, get sent, and be
    answered as if it were not there — which reads as the agent ignoring them.
    """
    name = (model or "").strip().lower()
    return bool(name) and provider_for(name).caps.images


async def refusal_for(agent: dict | None, workspace_model: str) -> str | None:
    """Why this agent would not read an attached image, or None when it would.

    The composer's answer (`/api/status`'s `can_see_images`/`attach_problem`),
    for the doors that are not the composer: chat's routes, v1 and v2 refuse
    with it rather than hand the images to a provider that drops them (F396),
    as cloud's `start_turn` does. A workflow agent's steps decide (F313). A
    builder-typed agent is the builder's to refuse (`reject_unseeable_images`).
    """
    if not agent or agent.get("type") == "builder":
        return None
    if agent.get("type") == "orchestrator":
        from core.orchestration.step_models import attach_verdict, walk_agent
        from core.scale.context import resolve_agent, resolve_orchestration

        walked = await walk_agent(agent, load_agent=resolve_agent, load_orchestration=resolve_orchestration,
                                  workspace_model=workspace_model)
        return attach_verdict(walked, {s.model: model_can_see_images(s.model) for s in walked.attaching()})
    from core.orchestration.step_models import model_or_none

    model = model_or_none(agent.get("model")) or workspace_model
    if model_can_see_images(model):
        return None
    return (f"This agent's model ({model}) cannot read images. Send the message without them, "
            "or choose a model that can for this agent.")


def checked_images(field: str = "images"):
    """A Pydantic validator that runs `validate` and reports its reason.

    Three request models carry attachments now — chat, a builder turn and a
    builder resume — and the wrapper that turns "what counts as an image" into
    an HTTP 422 was being copied along with each one. This module exists so
    there is one answer to that question; the answer needs one door as well.

    The reason travels as the validation message rather than being logged and
    replaced with something generic, because it is written to be read by the
    person who picked the file.

        class ChatRequest(BaseModel):
            images: list[str] = []
            _check_images = checked_images()
    """
    from pydantic import field_validator

    def _check(cls, value: list[str]) -> list[str]:
        try:
            return validate(value)
        except ImageRejected as exc:
            raise ValueError(str(exc)) from None

    return field_validator(field)(classmethod(_check))

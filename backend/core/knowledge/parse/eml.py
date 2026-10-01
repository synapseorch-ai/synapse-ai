"""Email: `.eml` (the standard library's `email`) and Outlook `.msg` (olefile), with their attachments.

One document per message: the subject as its heading and title, a header
block (from, to, cc, date), then the body — plain text when the message has
it, else its HTML through the web page extractor (`html.py`), whose hidden
content is left out and counted as on any page.

**Attachments** are parsed by `parse_bytes`, each its own `ParsedDocument`
under `attachments` (the chunker labels their chunks with the attachment's
name): an attached email recursively, at most `MAX_ATTACHMENT_DEPTH` (3) deep
and `MAX_ATTACHMENTS` per message. One that cannot be read here keeps its name
and the reason (`skipped_reason`), so the document ends `partial`, saying
which. Inline images (a logo, a signature) are not attachments and are not
counted. Attachments are not sent to OCR in v1.
"""
from __future__ import annotations

import hashlib
import posixpath
import struct
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

from core.knowledge.parse import limits
from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import ParseRefused
from core.knowledge.parse.tree import DocBuilder
from core.knowledge.parsed import MAX_ATTACHMENT_DEPTH, Attachment, ParsedDocument, ParserInfo

EML_PARSER = ParserInfo(name="eml", version="1")
MSG_PARSER = ParserInfo(name="msg", version="1")
_TOO_DEEP = f"Nested more than {MAX_ATTACHMENT_DEPTH} attachments deep; not read."


class _Message:
    """What both readers produce before the document is built."""

    def __init__(self) -> None:
        self.subject: str | None = None
        self.headers: list[tuple[str, str]] = []
        self.date: str | None = None
        self.plain: str | None = None
        self.html: str | None = None
        self.attachments: list[tuple[str, str, bytes]] = []   # (name, mime, data)
        self.embedded: list[tuple[str, _Message]] = []         # .msg: attached messages, already read


def _document(msg: _Message, ctx: ReadContext, parser: ParserInfo) -> ParsedDocument:
    from core.knowledge.parse.html import html_to_markdown
    from core.knowledge.parse.text import parse_markdown

    builder = DocBuilder()
    path = [msg.subject] if msg.subject else []
    if msg.subject:
        builder.add("heading", msg.subject, level=1, heading_path=path)
    header = "\n".join(f"{name}: {value}" for name, value in msg.headers if value)
    if header:
        builder.add("other", header, heading_path=path)
    if msg.plain and msg.plain.strip():
        from core.knowledge.parse.text import paragraphs

        for block in paragraphs(msg.plain.replace("\r\n", "\n")).blocks:
            builder.add("paragraph", block.text, heading_path=path)
    elif msg.html:
        extract = html_to_markdown(msg.html)
        builder.hidden(extract.hidden_removed)
        blocks, _ = parse_markdown(extract.markdown)
        for block in blocks:
            fields = {k: getattr(block, k) for k in ("level", "items", "table") if getattr(block, k) is not None}
            builder.add(block.kind, block.text, heading_path=path + list(block.heading_path), **fields)

    attachments = [_attachment(name, mime, data, ctx) for name, mime, data in msg.attachments[:limits.MAX_ATTACHMENTS]]
    for name, inner in msg.embedded[:max(0, limits.MAX_ATTACHMENTS - len(attachments))]:
        attachments.append(_embedded(name, inner, ctx))
    dropped = len(msg.attachments) + len(msg.embedded) - len(attachments)
    if dropped > 0:
        builder.warn("attachment_skipped", f"{dropped} more attachment(s) past the first {limits.MAX_ATTACHMENTS} "
                                           "were not read.", count=dropped)
    if any(a.skipped_reason == _TOO_DEEP for a in attachments):
        builder.warn("attachment_depth", f"Attachments nested deeper than {MAX_ATTACHMENT_DEPTH} were not read.")
    return builder.build(ctx.source, parser, title=msg.subject, doc_date=msg.date,
                         attachments=attachments)


def _attachment(name: str, mime: str, data: bytes, ctx: ReadContext) -> Attachment:
    from core.knowledge.parse import UnsupportedFormat, parse_bytes

    sha = hashlib.sha256(data).hexdigest()
    base = Attachment(name=name, sha256=sha, mime=mime, size_bytes=len(data))
    if ctx.depth + 1 > MAX_ATTACHMENT_DEPTH:
        return base.model_copy(update={"skipped_reason": _TOO_DEEP})
    try:
        parsed = parse_bytes(data, path=f"{ctx.path}#{name}", source=ctx.source.source, sha256=sha, mime=mime,
                             max_pages=ctx.max_pages, ocr=False, depth=ctx.depth + 1)
    except (UnsupportedFormat, ParseRefused) as exc:
        return base.model_copy(update={"skipped_reason": str(exc)[:300]})
    except Exception as exc:  # noqa: BLE001 — one bad attachment never loses the message
        return base.model_copy(update={"skipped_reason": f"could not be read: {type(exc).__name__}: {exc}"[:300]})
    return base.model_copy(update={"parsed": parsed})


def _embedded(name: str, inner: _Message, ctx: ReadContext) -> Attachment:
    """An Outlook message attached to another, read from the same container."""
    from core.knowledge.parse.tree import source_info

    path = f"{ctx.path}#{name}"
    sha = hashlib.sha256("\n".join(filter(None, (inner.subject, inner.plain, inner.html))).encode()).hexdigest()
    base = Attachment(name=name, sha256=sha, mime="application/vnd.ms-outlook", size_bytes=0)
    if ctx.depth + 1 > MAX_ATTACHMENT_DEPTH:
        return base.model_copy(update={"skipped_reason": _TOO_DEEP})
    sub = ReadContext(path=path, source=source_info(path=path, sha256=sha, size=0, mime=base.mime,
                                                    source=ctx.source.source), max_pages=ctx.max_pages,
                      depth=ctx.depth + 1)
    return base.model_copy(update={"parsed": _document(inner, sub, MSG_PARSER)})


# ── .eml ───────────────────────────────────────────────────────────────────────

def read_eml(data: bytes, ctx: ReadContext) -> ParsedDocument:
    message = BytesParser(policy=policy.default).parsebytes(data)
    if not isinstance(message, EmailMessage) or not (message.keys() or message.get_payload()):
        raise ParseRefused("The file is not an email message.")
    return _document(_from_email(message), ctx, EML_PARSER)


def _from_email(message: EmailMessage) -> _Message:
    msg = _Message()
    msg.subject = str(message.get("subject") or "").strip() or None
    for name in ("From", "To", "Cc", "Date"):
        value = message.get(name)
        if value:
            msg.headers.append((name, " ".join(str(value).split())))
    try:
        msg.date = parsedate_to_datetime(str(message["date"])).date().isoformat() if message["date"] else None
    except (TypeError, ValueError):
        msg.date = None
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    msg.plain = _content(plain) if plain is not None else None
    msg.html = _content(html) if html is not None else None
    for k, part in enumerate(message.iter_attachments(), start=1):
        if part.get_content_maintype() == "image" and part.get_content_disposition() != "attachment":
            continue  # an inline image (Content-ID): a logo, not an attachment
        name = posixpath.basename((part.get_filename() or "").replace("\\", "/")) or f"attachment-{k}"
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else inner
            data = inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(part.get_payload(decode=True) or b"")
            name = name if posixpath.splitext(name)[1] else f"{name}.eml"
            msg.attachments.append((name, "message/rfc822", data))
            continue
        msg.attachments.append((name, part.get_content_type(), part.get_payload(decode=True) or b""))
    return msg


def _content(part: Any) -> str | None:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):  # an unknown charset
        raw = part.get_payload(decode=True) or b""
        return raw.decode("utf-8", errors="replace")


# ── .msg ───────────────────────────────────────────────────────────────────────

_PT_UNICODE, _PT_STRING8, _PT_BINARY, _PT_SYSTIME, _PT_OBJECT = 0x001F, 0x001E, 0x0102, 0x0040, 0x000D


class _Msg:
    """An Outlook message (or one embedded in it) inside an OLE container: MAPI properties as streams."""

    def __init__(self, ole: Any, prefix: list[str], *, top: bool):
        self.ole, self.prefix, self.top = ole, prefix, top

    def _stream(self, name: str) -> bytes | None:
        path = [*self.prefix, name]
        return self.ole.openstream(path).read() if self.ole.exists("/".join(path)) else None

    def string(self, prop: int) -> str | None:
        for kind, codec in ((_PT_UNICODE, "utf-16-le"), (_PT_STRING8, "cp1252")):
            raw = self._stream(f"__substg1.0_{prop:04X}{kind:04X}")
            if raw is not None:
                return raw.decode(codec, errors="replace").rstrip("\x00").strip() or None
        return None

    def binary(self, prop: int) -> bytes | None:
        return self._stream(f"__substg1.0_{prop:04X}{_PT_BINARY:04X}")

    def time(self, prop: int) -> datetime | None:
        """A PT_SYSTIME property from the fixed-size property stream."""
        raw = self._stream("__properties_version1.0")
        if not raw:
            return None
        start = 32 if self.top else 24
        for offset in range(start, len(raw) - 15, 16):
            tag, value = struct.unpack_from("<I", raw, offset)[0], struct.unpack_from("<Q", raw, offset + 8)[0]
            if tag == (prop << 16) | _PT_SYSTIME and value:
                return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value // 10)
        return None

    def storages(self, kind: str) -> list[list[str]]:
        found = set()
        for entry in self.ole.listdir(streams=True, storages=True):
            if entry[:len(self.prefix)] == self.prefix and len(entry) > len(self.prefix) \
                    and entry[len(self.prefix)].startswith(kind):
                found.add(entry[len(self.prefix)])
        return [[*self.prefix, name] for name in sorted(found)]


def read_msg(data: bytes, ctx: ReadContext) -> ParsedDocument:
    import olefile

    if not olefile.isOleFile(data):
        raise ParseRefused("The file is not an Outlook message.")
    ole = olefile.OleFileIO(data)
    try:
        root = _Msg(ole, [], top=True)
        if root.string(0x0037) is None and root.string(0x1000) is None and not root.storages("__attach_version1.0_"):
            raise ParseRefused("The file is not an Outlook message (no subject, body or attachments).")
        return _document(_from_msg(root), ctx, MSG_PARSER)
    finally:
        ole.close()


def _from_msg(m: _Msg) -> _Message:
    msg = _Message()
    msg.subject = m.string(0x0037)
    sender = m.string(0x0C1A)
    address = m.string(0x5D01) or m.string(0x0C1F)
    sent = m.time(0x0039) or m.time(0x0E06)
    msg.headers = [("From", f"{sender} <{address}>" if sender and address and address not in sender
                    else sender or address or ""),
                   ("To", m.string(0x0E04) or ""), ("Cc", m.string(0x0E03) or ""),
                   ("Date", sent.strftime("%Y-%m-%d %H:%M UTC") if sent else "")]
    msg.date = sent.date().isoformat() if sent else None
    msg.plain = m.string(0x1000)
    html = m.binary(0x1013)
    msg.html = html.decode("utf-8", errors="replace") if html else m.string(0x1013)
    for k, storage in enumerate(m.storages("__attach_version1.0_"), start=1):
        part = _Msg(m.ole, storage, top=False)
        name = posixpath.basename((part.string(0x3707) or part.string(0x3704) or "").replace("\\", "/")) \
            or f"attachment-{k}"
        embedded = [*storage, f"__substg1.0_3701{_PT_OBJECT:04X}"]
        if m.ole.exists("/".join(embedded)):
            msg.embedded.append((name if name.endswith(".msg") else f"{name}.msg",
                                 _from_msg(_Msg(m.ole, embedded, top=False))))
            continue
        mime = part.string(0x370E) or "application/octet-stream"
        if part.string(0x3712) and mime.startswith("image/"):
            continue  # an inline image (it has a content id), as in .eml
        data = part.binary(0x3701)
        if data is not None:
            msg.attachments.append((name, mime, data))
    return msg

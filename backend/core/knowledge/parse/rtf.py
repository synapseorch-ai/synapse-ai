"""RTF, read by a small built-in reader: the text a person sees, paragraph by paragraph.

RTF is text with control words. The reader keeps the document's text and
paragraph breaks and drops what is not text: the font, colour and style
tables, pictures, embedded objects, headers and footers, field instructions
(a field's result is kept), and any `{\\*\\…}` destination it does not know.
Characters come from `\\'hh` (in the document's code page, `\\ansicpgN`) and
`\\uN` (skipping its `\\ucN` fallback characters). Text under `\\v` (hidden)
is left out and counted. The title comes from the `\\info` group.
"""
from __future__ import annotations

import re

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import ParseRefused
from core.knowledge.parse.text import paragraphs
from core.knowledge.parse.tree import DocBuilder
from core.knowledge.parsed import ParsedDocument, ParserInfo

PARSER = ParserInfo(name="rtf", version="1")

_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+",
                    re.DOTALL)
#: Groups whose content is never the document's text.
_SKIP = frozenset({"fonttbl", "colortbl", "stylesheet", "listtable", "listoverridetable", "revtbl", "rsidtbl",
                   "generator", "pict", "object", "header", "headerl", "headerr", "headerf", "footer", "footerl",
                   "footerr", "footerf", "fldinst", "themedata", "colorschememapping", "latentstyles",
                   "datastore", "xmlnstbl", "listtext", "pntext", "pntxta", "pntxtb", "bkmkstart", "bkmkend",
                   "filetbl", "operator", "company", "author", "keywords", "comment", "doccomm", "subject"})
_BREAKS = {"par": "\n\n", "line": "\n", "sect": "\n\n", "page": "\n\n", "row": "\n", "cell": " | ", "tab": "\t",
           "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“",
           "rdblquote": "”", "emspace": " ", "enspace": " ", "qmspace": " ", "~": " "}


def rtf_text(data: bytes) -> tuple[str, str | None, int]:
    """The visible text, the title from `\\info`, and how many hidden runs were left out."""
    if not data.lstrip().startswith(b"{\\rtf"):
        raise ParseRefused("The file is not RTF.")
    codepage = "cp1252"
    out: list[str] = []
    title: list[str] = []
    # Per group: (skipping, hidden, unicode skip count, in title)
    stack: list[tuple[bool, bool, int, bool]] = []
    skipping, hidden, uc, in_title = False, False, 1, False
    pending_skip = 0
    hidden_runs, hidden_open = 0, False
    star = False

    def emit(text: str) -> None:
        nonlocal hidden_open, hidden_runs
        if skipping:
            return
        if in_title:
            title.append(text)
            return
        if hidden:
            if text.strip() and not hidden_open:
                hidden_runs += 1
                hidden_open = True
            return
        hidden_open = False
        out.append(text)

    for match in _TOKEN.finditer(data):
        word, arg, hexchar, symbol, brace, text = match.groups()
        if pending_skip and (hexchar or text):
            if text:
                take = min(pending_skip, len(text))
                pending_skip -= take
                text = text[take:]
                if not text:
                    continue
            else:
                pending_skip -= 1
                continue
        if brace == b"{":
            stack.append((skipping, hidden, uc, in_title))
            star = False
        elif brace == b"}":
            if stack:
                skipping, hidden, uc, in_title = stack.pop()
        elif symbol is not None:
            if symbol == b"*":
                star = True
            elif symbol in (b"\\", b"{", b"}"):
                emit(symbol.decode())
            elif symbol == b"~":
                emit(" ")
            elif symbol in (b"\n", b"\r"):
                emit("\n\n")
        elif hexchar is not None:
            emit(bytes([int(hexchar, 16)]).decode(codepage, errors="replace"))
        elif word is not None:
            name = word.decode()
            if star or name in _SKIP:
                skipping = True
                star = False
            elif name == "title":  # in the info group; its other children are in _SKIP or hold no text
                in_title = True
            elif name == "ansicpg" and arg:
                codepage = f"cp{int(arg)}"
                try:
                    "".encode(codepage)
                except LookupError:
                    codepage = "cp1252"
            elif name == "u" and arg:
                value = int(arg)
                emit(chr(value + 65536 if value < 0 else value))
                pending_skip = uc
            elif name == "uc" and arg is not None:
                uc = int(arg)
            elif name == "v":
                hidden = arg != b"0"
            elif name in _BREAKS:
                emit(_BREAKS[name])
            elif name == "plain":  # character formatting back to defaults: not hidden
                hidden = False
        elif text is not None:
            emit(text.decode(codepage, errors="replace"))
    joined = "".join(out)  # unbalanced braces: what there is was read
    joined = "\n".join(line.strip() for line in joined.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", joined).strip(), ("".join(title).strip() or None), hidden_runs


def read_rtf(data: bytes, ctx: ReadContext) -> ParsedDocument:
    text, title, hidden = rtf_text(data)
    builder = DocBuilder()
    builder.hidden(hidden)
    for block in paragraphs(text).blocks:
        builder.add("paragraph", block.text)
    return builder.build(ctx.source, PARSER, title=title)

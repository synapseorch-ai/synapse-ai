"""Text that reaches the index is text a person could see.

Documents are untrusted input to an agent (OWASP LLM08, EchoLeak): a prompt
hidden in a zero-width run, a bidi override that reorders what a reader sees,
or an HTML comment in Markdown is invisible on screen and fully visible to the
model. `sanitize_text` removes them **before** blocks are cut, so every block's
offsets slice the canonical text the chunker, the citations and the quote check
all read. Each kind of removal is one warning with a count; nothing is removed
silently.

A2 extends this for hidden text in Office files and PDFs (white-on-white, tiny
fonts, hidden slides), which only a structured parser can see.
"""
from __future__ import annotations

import re
import unicodedata

from core.knowledge.parsed import ParseWarning

#: Zero-width characters, the byte-order mark inside text, soft hyphens, and
#: the bidi embedding / override / isolate controls.
_INVISIBLE = re.compile("[​‌‍⁠⁡⁢⁣⁤﻿­"
                        "‪‫‬‭‮⁦⁧⁨⁩؜‎‏]")
#: C0 controls other than tab and newline, and DEL.
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def sanitize_text(text: str, *, html_comments: bool = False) -> tuple[str, list[ParseWarning]]:
    """`text` with invisible characters (and, for Markdown or HTML, comments) removed.

    Line endings become `\\n` and the text is NFC-normalized; neither is a
    warning, because neither hides anything.
    """
    warnings: list[ParseWarning] = []
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    if html_comments:
        found = _HTML_COMMENT.findall(clean)
        if found:
            clean = _HTML_COMMENT.sub("", clean)
            warnings.append(ParseWarning(code="html_comments_removed", count=len(found),
                                         message=f"{len(found)} HTML comment(s) removed: a reader never sees them."))
    invisible = len(_INVISIBLE.findall(clean)) + len(_CONTROLS.findall(clean))
    if invisible:
        clean = _CONTROLS.sub("", _INVISIBLE.sub("", clean))
        warnings.append(ParseWarning(code="zero_width_removed", count=invisible,
                                     message=f"{invisible} invisible character(s) removed (zero-width, bidi "
                                             "controls or control codes)."))
    return unicodedata.normalize("NFC", clean), warnings

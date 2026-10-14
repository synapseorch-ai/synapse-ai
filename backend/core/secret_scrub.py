"""Credential shapes in free text, and the one scrubber over them.

First written for the AI builder's pasted Agents SDK exports (SW1, D253: a
hosted MCP server's `authorization` is a bearer token written into the
source), and moved here for boards (BOARD1a, D280), whose card text is
refused when it holds one. `core.native_builder.export` re-exports both names.
"""
from __future__ import annotations

import re

#: What a credential is replaced with. The guide tells the builder that each
#: one is a credential the export held, to be listed under "Not carried over".
CREDENTIAL_MARK = "‹credential removed›"

#: Credential shapes, most specific first. Each pattern's last group (or the
#: whole match, when it has none) is what is replaced. Ids that are not
#: secrets — `vs_…` vector stores, `wf_…` workflows — match none of them.
_CREDENTIALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # A JWT: what a hosted MCP server's `authorization` usually holds.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # OpenAI's and Anthropic's keys (`sk-…`, `sk-proj-…`, `sk-ant-…`); Stripe's.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # `Bearer <token>`: the token only. A reference — `Bearer ${process.env.X}`,
    # `Bearer {{ global.X }}` — starts with a character the class leaves out.
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
    # A quoted literal assigned to a name that says what it is. A value with a
    # space in it is prose or a placeholder, not a key; so is one already marked.
    re.compile(
        r"""(?i)(\b[\w-]*(?:api[_-]?key|secret|token|password|passwd|authorization)["']?\s*[:=]\s*)"""
        r"""(["'])([^"'\s]{12,})\2"""
    ),
)


def scrub_credentials(text: str) -> tuple[str, int]:
    """`text` with each credential replaced by `CREDENTIAL_MARK`, and how many."""
    removed = 0

    def _mark(match: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        groups = match.groups()
        if not groups:
            return CREDENTIAL_MARK
        if len(groups) == 2:  # (prefix, secret)
            return f"{groups[0]}{CREDENTIAL_MARK}"
        prefix, quote, _ = groups  # (prefix, quote, secret)
        return f"{prefix}{quote}{CREDENTIAL_MARK}{quote}"

    for pattern in _CREDENTIALS:
        text = pattern.sub(_mark, text)
    return text, removed


def holds_credential(text: str) -> bool:
    """Whether `text` holds anything `scrub_credentials` would take out."""
    return bool(text) and any(pattern.search(text) for pattern in _CREDENTIALS)

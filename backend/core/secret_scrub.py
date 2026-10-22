"""Credential shapes in free text, and the one scrubber over them.

First written for the AI builder's pasted Agents SDK exports (SW1, D253: a
hosted MCP server's `authorization` is a bearer token written into the
source), and moved here for boards (BOARD1a, D280), whose card text is
refused when it holds one. `core.native_builder.export` re-exports both names.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

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
    # Any provider's key shaped like Stripe's, `<name>_live_…` / `<name>_test_…` (Shippo's, and others): the tail
    # mixes letters and digits, so a snake_case name never matches (F576, BWA1). Shapes only: a key with no
    # shape we know stays the person's to keep out of chat (Naveen's call, 2026-10-15).
    re.compile(r"\b[a-z][a-z0-9]{1,15}_(?:live|test)_(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # `Bearer <token>`: the token only. A reference — `Bearer ${process.env.X}`,
    # `Bearer {{ global.X }}` — starts with a character the class leaves out.
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
    # A value right after a credential label, said in prose ("the API key is X", "token: X", "client secret = X"),
    # whatever its shape: a vendor's own key matched no shape above and reached a run (F896, SAI2-P2's chg-09). It
    # must read like one: letters with digits, 8+ characters, starting with neither a quote nor a reference's `{`,
    # `$` or `<`, and not a URL. A sentence about a key ("the key is set", "token is expired") has no such value.
    re.compile(
        r"(?i)(\b(?:api[\s_-]?key|access[\s_-]?token|auth(?:entication)?[\s_-]?token|client[\s_-]?secret|"
        r"secret[\s_-]?key|secret|token|key)\b\s*(?:\bis\b|\bwas\b|[:=-])\s*)"
        r"(?![^\s]*://)(?=[A-Za-z0-9._~+/=-]*\d)(?=[A-Za-z0-9._~+/=-]*[A-Za-z])"
        r"([A-Za-z0-9][A-Za-z0-9._~+/=-]{6,}[A-Za-z0-9=])"
    ),
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


# ── Secret variables, by value (F854) ────────────────────────────────────────
#
# `{{ global.KEY }}` is rendered into prompts, tool requests and step templates
# from `core.variables.load()`, and a variable whose stored value is a reference
# (cloud's `synsec://`) comes back as its plaintext: the model and the tool need
# it. Everything after that — an event a client follows, a log a member reads, a
# step's recorded error, a checkpoint — must show the name, not the value. So
# `variables.load()` holds the secrets it resolved for the task that read them,
# and each place that stores or shows text scrubs them out. The engine drives a
# run or a turn from one task (`iter_with_heartbeat`), so every such place in it
# sees what its load held.

#: A value shorter than this is not scrubbed: it would replace ordinary words.
SECRET_MIN = 6

_HELD: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("held_secrets", default=())


def secret_mark(name: str) -> str:
    """What a secret's value is shown as."""
    return f"‹secret: {name}›"


def hold_secrets(secrets: dict[str, Any]) -> None:
    """Scrub these `{name: value}` from everything this task stores or shows from now on."""
    held = dict(_HELD.get())
    held.update({k: v for k, v in secrets.items() if isinstance(v, str) and len(v) >= SECRET_MIN})
    # Longest first: a secret that contains another is replaced whole.
    _HELD.set(tuple(sorted(held.items(), key=lambda kv: -len(kv[1]))))


def held_secrets() -> dict[str, str]:
    return dict(_HELD.get())


def scrub_secrets(text: str) -> str:
    """`text` with each held secret's value replaced by its name."""
    held = _HELD.get()
    if not held or not isinstance(text, str):
        return text
    for name, value in held:
        if value in text:
            text = text.replace(value, secret_mark(name))
    return text


def scrub_secrets_in(value: Any) -> Any:
    """`value` — an event, a snapshot — with every string in it scrubbed. The same object when nothing is held."""
    held = _HELD.get()
    if not held:
        return value
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: scrub_secrets_in(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_secrets_in(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_secrets_in(v) for v in value)
    return value


def reveal_secrets(text: str, values: dict[str, Any]) -> str:
    """The inverse, for the engine's own use only: a scrubbed checkpoint's text with each
    secret's current value put back, so a resumed turn reads what it was given."""
    if not isinstance(text, str) or "‹secret: " not in text:
        return text
    for name, value in values.items():
        if isinstance(value, str):
            text = text.replace(secret_mark(name), value)
    return text

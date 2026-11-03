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
    # The same with a shorter tail, when it mixes upper case, lower case and digits as no snake_case name does:
    # team res-04's `shippo_test_…` canary was ten characters long (F1092, SAI2-P4).
    re.compile(r"\b[a-z][a-z0-9]{1,15}_(?:live|test)_(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])"
               r"[A-Za-z0-9]{8,15}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # A chat channel's token (F1286): rule 14 connects a channel on a card, and one pasted into the chat matched no
    # shape. Telegram's bot token: the bot's id, a colon, 35 characters. Discord's: the bot's id in base64 (it starts
    # M, N or O), a 6-character timestamp and a 27–38 character HMAC, dot-joined, held to those lengths so three dotted
    # runs of any other size stay. Meta's access token (WhatsApp, the Graph API): EAA and 80+ letters and digits.
    re.compile(r"(?<![\w:])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![\w.-])[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}(?![\w.-])"),
    re.compile(r"\bEAA[A-Za-z0-9]{80,}\b"),
    # `Bearer <token>`: the token only. A reference — `Bearer ${process.env.X}`,
    # `Bearer {{ global.X }}` — starts with a character the class leaves out.
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
    # A value right after a credential label, said in prose ("the API key is X", "token: X", "client secret = X"),
    # whatever its shape: a vendor's own key matched no shape above and reached a run (F896, SAI2-P2's chg-09). It
    # must read like one: letters with digits, 8+ characters, starting with neither a quote nor a reference's `{`,
    # `$` or `<`, and not a URL. A sentence about a key ("the key is set", "token is expired") has no such value.
    re.compile(
        r"(?i)(\b(?:api[\s_-]?key|access[\s_-]?token|auth(?:entication)?[\s_-]?token|client[\s_-]?secret|"
        r"secret[\s_-]?key|credential|secret|token|key)\b\s*(?:\bis\b|\bwas\b|[:=-])\s*)"
        r"(?![^\s]*://)(?=[A-Za-z0-9._~+/=-]*\d)(?=[A-Za-z0-9._~+/=-]*[A-Za-z])"
        r"([A-Za-z0-9][A-Za-z0-9._~+/=-]{6,}[A-Za-z0-9=])"
    ),
    # A value straight after a label that can only mean a credential, with no "is" or ":" ("with the credential X",
    # "API key X"): team res-04 (F1092). Never after a bare "key", "token" or "secret", and 12+ characters, so "the
    # key user_id1" or "my credential manager2" is a sentence, not a key.
    re.compile(
        r"(?i)(\b(?:api[\s_-]?key|access[\s_-]?token|auth(?:entication)?[\s_-]?token|client[\s_-]?secret|"
        r"secret[\s_-]?key|credential)s?\s+)"
        r"(?![^\s]*://)(?=[A-Za-z0-9._~+/=-]*\d)(?=[A-Za-z0-9._~+/=-]*[A-Za-z])"
        r"([A-Za-z0-9][A-Za-z0-9._~+/=-]{10,}[A-Za-z0-9=])"
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


#: Keys whose values are ids, times and cursors, never anyone's words: the card mask leaves them alone (F1184).
_NOT_WORDS = frozenset({"chat_turn", "timestamp", "cursor", "id", "turn"})


def _words_under(key: Any) -> bool:
    return not (isinstance(key, str) and (key in _NOT_WORDS or key.endswith("_id") or key.endswith("_ids")))


def for_storage(value: Any, held: dict[str, str] | None = None) -> Any:
    """A chat turn as it is kept (F1184): every string with the held secrets named (F854's mark), and every number
    that passes the engine's card test masked, as `gate.without_cards` masks it — except ids, times and cursors. The
    model reads the turn as it was sent; this is the stored copy only. Stable: what is kept already comes back the
    same. `held` is for a caller whose secrets were held in another task (the fleet's chat job holds what its loop's
    task held); without it, this task's."""
    from core.browser.gate import without_cards

    pairs = (tuple(sorted(((k, v) for k, v in held.items() if isinstance(v, str) and len(v) >= SECRET_MIN),
                          key=lambda kv: -len(kv[1])))
             if held is not None else _HELD.get())

    def kept(item: Any, key: Any = None) -> Any:
        if isinstance(item, str):
            for name, secret in pairs:
                if secret in item:
                    item = item.replace(secret, secret_mark(name))
            return without_cards(item) if _words_under(key) else item
        if isinstance(item, dict):
            return {k: kept(v, k) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return type(item)(kept(v, key) for v in item)
        return item

    return kept(value)


def reveal_secrets(text: str, values: dict[str, Any]) -> str:
    """The inverse, for the engine's own use only: a scrubbed checkpoint's text with each
    secret's current value put back, so a resumed turn reads what it was given."""
    if not isinstance(text, str) or "‹secret: " not in text:
        return text
    for name, value in values.items():
        if isinstance(value, str):
            text = text.replace(secret_mark(name), value)
    return text


# ── a secret asked for (F1093) ────────────────────────────────────────────────────────────────────────────────────
# What Synapse AI says to the person never asks for a key, a token or a password: one typed in chat is kept with the
# chat (chg-09). A connection comes through a Connect card, a key through Settings → Vault. H45's res-05 (P5's measure,
# 10-27): the build planner's open question read "you'll need to provide your Stripe Secret API Key (starts with
# sk_test_ or sk_live_) and your SendGrid API Key (starts with SG.)", and nothing checked the plan's own words.

#: A secret named: a key, a token, a password, a PIN or a code, or a login, as something of theirs or a service's.
#: Not a count of tokens, a password reset or a licence to buy (SAI2's review, 17). A question naming one asks for it.
SECRET_NAMED = re.compile(
    r"(?i)\b(?:(?:api|access|private|secret|signing)[\s_-]?keys?|(?:api|access|auth|bearer|refresh|secret|personal)"
    r"[\s_-]?tokens?|client[\s_-]?secret|credentials?|bearer|pass(?:word|wd|code|phrase)s?(?!\s+reset)|"
    r"(?:2fa|mfa|otp|one[\s-]time|verification|security|access)\s+codes?|(?-i:\bPIN\b)|pin\s+(?:code|number)s?|"
    r"(?:log|sign)[\s-]?in(?:\s+details)?\s+(?:for|to)\b|"
    r"(?:your|their|its|the)\s+(?:[\w.-]+\s+){0,2}(?:keys?|tokens?|logins?|secrets?)\b(?!\s+(?:dates?|points?|metrics?|"
    r"words?|people|accounts?|questions?|steps?|results?)))")
#: Prose that asks someone for something: the person addressed ("you'll need to provide", "once you have", "please
#: paste") or told to be asked ("ask the user for"), then a verb of handing over; or what Synapse AI needs ("I'll need"),
#: or what is handed to it ("give me your …", "send it to us": F1313's prod-10, "Just give me your bot token"; never
#: "send me a digest", a workflow's own words, TRI1's review). "Reminds
#: the team to rotate API keys" and "the key is kept in the Vault" ask for nothing.
_ASKING = re.compile(
    r"(?i)\b(?:(?:you(?:'ll|'d|'re| will| would| can| could| should| must| need)?|please|"
    r"(?:ask|prompt|request)(?:s|ed|ing)?\s+(?:the\s+)?(?:user|person|customer|client|caller|them|him|her)s?)"
    r"\b[^.?!\n]{0,60}?\b(?:provide|give|share|send|paste|enter|type|supply|tell|have|ready|for)\b|"
    r"(?:i|we)(?:'ll| will| would)?\s+need\b|"
    r"(?:give|send|share|paste|tell|post|dm)\s+(?:(?:me|us)\s+(?:your|the)\b|(?:it|them)\s+(?:over\s+)?to\s+(?:me|us)\b))")
_SENTENCES = re.compile(r"(?<=[.?!])\s+|\n+")

#: Code's line in place of what asked (the manager's net, 10-27).
NEVER_IN_CHAT = ("A key, a token or a password never goes in this chat: connect the service on its Connect card, or "
                 "add the key in Settings → Vault, where it is kept encrypted.")


#: Where a secret is said to go, never where it comes from (TRI1's review of F1313): put on its Connect card, in
#: Settings, the Vault or Variables; the card above or below; not in this chat; never pasted or sent. "Find it in your
#: dashboard's settings", "I'll keep it secure" and "send it to me privately" name no place for it.
SAFE_PLACE = re.compile(
    r"(?i)\b(?:add|enter|type|put|paste|save|store)\b[^.?!\n]{0,40}?\b(?:on|in|into)\s+(?:the\s+|its\s+|a\s+)?"
    r"(?:connect\s+card|card|settings|vault|variables)\b|"
    r"\bcard\s+(?:above|below)\b|\bconnect\s+card\b|\bsettings\s*[›>→]\s*vault\b|"
    r"\bnot\s+(?:here|in\s+(?:this|the)\s+chat)\b|"
    r"\b(?:never|don'?t|do\s+not)\s+(?:paste|send|share|type|enter|give|post)\b")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _asks(sentence: str) -> bool:
    asking = _ASKING.search(sentence)
    return asking is not None and SECRET_NAMED.search(sentence, asking.start()) is not None


#: Emphasis and code marks, read through; `_` is a space ("`TELEGRAM_BOT_TOKEN`" reads as its words, TRI1's review).
_MARKUP = re.compile(r"[*`]+")
#: A secret an item under an asking lead-in names with no determiner ("1. **Bot token** – from BotFather"): the ask is
#: the lead-in's, so a bare name counts there, and only there (TRI1's review).
_ITEM_SECRET = re.compile(r"(?i)\b(?:[\w-]+\s+){0,2}(?:tokens?|keys?|passwords?|secrets?)\b")
#: Words with a secret's name in them that name none: a token limit, a password reset, the key points. Struck from an
#: item before it is read, so "I'll need: - the token limit for the model" asks for nothing (CI2's single on F1313).
#: Never "page": a screenshot of an API key page shows the key.
_NOT_A_SECRET = re.compile(
    r"(?i)\b(?:tokens?|keys?|passwords?|secrets?)\s+(?:limits?|counts?|budgets?|usage|resets?|dates?|points?|"
    r"metrics?|words?|people|accounts?|questions?|steps?|results?)\b")


def _lead_ins(lines: list[str]) -> dict[int, list[int]]:
    """Each asking lead-in, by line, with the list items under it: a line that asks and ends in ":" ("To set it up,
    I'll need:"), then the numbered or bulleted lines right after it (blank lines between them kept)."""
    out: dict[int, list[int]] = {}
    for i, lead in enumerate(lines):
        if not lead.rstrip().endswith(":") or _ASKING.search(lead) is None:
            continue
        items = []
        for j in range(i + 1, len(lines)):
            if not lines[j].strip():
                continue
            if not _LIST_ITEM.match(lines[j]):
                break
            items.append(j)
        out[i] = items
    return out


def _asked_item(line: str) -> bool:
    """An item under an asking lead-in that names a secret with no safe place for it asks for it (F1313's prod-10:
    "I'll need:" on one line, "1. **Your Telegram bot token** – from BotFather" on the next; TRI1's shape). Markup is
    read through. A list under no asking lead-in ("Steps: 1. rotate your API keys") asks for nothing."""
    plain = _NOT_A_SECRET.sub("", _MARKUP.sub("", line).replace("_", " "))
    named = SECRET_NAMED.search(plain) is not None or _ITEM_SECRET.search(plain) is not None
    return named and SAFE_PLACE.search(plain) is None


def asks_for_a_secret(text: str, *, strict: bool = True) -> bool:
    """Whether `text` asks the person for a key, a token or a password. `strict`, for a question (or a `cannot`,
    which is said to the person as it is): naming one is asking for it. Otherwise, for prose that describes a
    workflow (a summary, a step, an agent, a reply): only a sentence that asks for one, so a workflow that reminds a
    team to rotate API keys may still be described; and a list item under an asking lead-in that names one with no
    safe place for it (`_lead_ins`, `_asked_item`: F1313)."""
    if not isinstance(text, str) or not text:
        return False
    if strict:
        return SECRET_NAMED.search(text) is not None
    lines = text.split("\n")
    return any(_asks(s) for s in _SENTENCES.split(text)) or any(
        _asked_item(lines[j]) for items in _lead_ins(lines).values() for j in items)


def without_secret_asks(text: str, *, line: str | None = NEVER_IN_CHAT) -> tuple[str, bool]:
    """`text` with each sentence that asks for a secret taken out, each list item an asking lead-in carries its ask to
    (whole, F1313) and the lead-in with them when every item went, and code's `line` once in their place (none when
    `line` is None); `(text, whether any was)`. Lines and paragraphs stay as they were."""
    if not asks_for_a_secret(text, strict=False):
        return text, False
    raw_lines = text.split("\n")
    gone: set[int] = set()
    for lead, items in _lead_ins(raw_lines).items():
        asked = {j for j in items if _asked_item(raw_lines[j])}
        gone |= asked
        if asked and asked == set(items):
            gone.add(lead)  # every item asked: the lead-in says nothing now
    out = []
    for i, raw in enumerate(raw_lines):
        if i in gone:
            continue
        parts = re.split(r"(?<=[.?!])(\s+)", raw)
        kept = "".join(p for n, p in enumerate(parts) if n % 2 or not _asks(p)).strip()
        if kept or not raw.strip():
            out.append(kept if raw.strip() else raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if line:
        cleaned = f"{cleaned}\n\n{line}" if cleaned else line
    return cleaned, True

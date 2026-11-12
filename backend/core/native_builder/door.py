"""What a person types to the AI builder, checked before any run, model or log sees it (BWA1, D274).

Measured on cloud dev (BWA1's Nova Pro baseline, `drafts/bwa1/baseline.md`): the builder asked people to paste
an insurance portal's password, an OpenAI key, GitHub and Linear tokens into the chat. Every one of them, and a
pasted card number, then sat in the run's state, its events and its checkpoints. A prompt rule ("never ask for a
secret") is a sentence the model can ignore. This is the door instead, in code, on every way a message reaches the
builder: a new message, and an answer to one of its questions.

- **A card number or a password is refused.** Nothing runs and nothing is kept; the person gets a fixed sentence
  that says why and what to do instead. A booking or payment goes through the provider's own checkout or payment
  link, never through card data in a workspace, and a service is connected through its own sign-in or a key on a
  secure screen.
- **A key or a token is masked** (`CREDENTIAL_MARK`, SW1's patterns, now on every message rather than only on
  pasted Agents SDK code). The rest of the message is still worth answering, so it runs, and the person is told the
  key was taken out and should be rotated.

Both products' builder routes call `check` on a new message and `check_answer` on an answer to a builder run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from core.native_builder.export import CREDENTIAL_MARK, scrub_credentials
from core.secret_scrub import LOOKALIKES, mask_as_written

Refusal = Literal["card", "password"]

#: What the person reads, word for word. Shown by the panel as the builder's reply (`builder_notice`).
REFUSALS: dict[str, str] = {
    "card": (
        "I didn't keep that message: it had a card number in it. Card details never go in this chat. A payment "
        "or a booking goes through the provider's own checkout or a payment link, so the card never passes "
        "through your workspace. If that card is real, keep an eye on it."
    ),
    "password": (
        "I didn't keep that message: it had a password in it. Passwords never go in this chat. To connect a "
        "service, use its own sign-in or add a key on a secure settings screen; ask me which one and I'll point "
        "you to it. If that password is real, consider changing it."
    ),
}

MASKED_NOTICE = (
    "I took a key out of your message before reading it (it shows as ‹credential removed›). Please don't paste keys "
    "in this chat again: if it was real, rotate it at the provider, then connect the service on its Connect card, or "
    "add the new key in Settings → Vault, where it is kept encrypted."
)

#: A secret the workspace already holds, not a connection's (a setting's or a variable's), taken out (F1307).
HELD_NOTICE = (
    "I took a secret this workspace already holds out of your message before reading it (it shows as ‹credential "
    "removed›). It is still saved where it was, so there's no need to paste it here."
)

#: What a conversation keeps, and the panel shows, in place of a message the door refused.
NOT_KEPT = "‹message not kept: it held a card number or a password›"


@dataclass(frozen=True)
class Checked:
    """A message after the door: the text to run, or why nothing runs."""

    text: str
    refused: Refusal | None = None
    removed: int = 0
    #: "held" when all that was taken out is a secret the workspace holds that is no connection's (F1307): its notice
    #: says it is still saved where it was; otherwise "masked", the key's notice (connect it on its card).
    reason: Literal["masked", "held"] = "masked"

    @property
    def notice(self) -> str | None:
        if self.refused:
            return REFUSALS[self.refused]
        if not self.removed:
            return None
        return HELD_NOTICE if self.reason == "held" else MASKED_NOTICE

    def event(self) -> dict | None:
        """The panel's event for this check, or None when there is nothing to say.

        `kept` is the message as it was kept (`NOT_KEPT` for a refused one). The panel shows it in place of what
        the person typed, so the screen matches the conversation and a later turn's history never carries the
        secret back."""
        if not self.notice:
            return None
        return notice(self.refused or self.reason, NOT_KEPT if self.refused else self.text, final=bool(self.refused))


# ── cards ────────────────────────────────────────────────────────────────────

#: 13–19 digits, in groups split by single spaces or dashes, not part of a longer run of digits.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d])")

#: Issuer prefixes (IIN ranges) and the lengths each issues. A run of digits that passes Luhn but starts with none
#: of these is an order number, a tracking number or an id, and is left alone.
_ISSUERS: tuple[tuple[tuple[int, int], tuple[int, ...]], ...] = (
    ((4, 4), (13, 16, 19)),                     # Visa
    ((51, 55), (16,)), ((2221, 2720), (16,)),   # Mastercard
    ((34, 34), (15,)), ((37, 37), (15,)),       # American Express
    ((6011, 6011), (16, 19)), ((644, 649), (16, 19)), ((65, 65), (16, 19)),  # Discover
    ((3528, 3589), (16, 19)),                   # JCB
    ((300, 305), (14, 16, 19)), ((36, 36), (14, 16, 19)), ((38, 39), (16, 19)),  # Diners Club
    ((62, 62), (16, 17, 18, 19)),               # UnionPay
    ((50, 50), (16, 19)), ((56, 58), (16, 19)),  # Maestro
)

#: A card's security code or PIN said as such ("cvc 123", "CVV: 4321", "card pin 1234", "pin is 1234"). A bare
#: "PIN 560001" is an Indian postal code, so a PIN counts only as a card's or an ATM's, or as exactly four digits
#: given as one ("pin is 1234", "pin: 1234").
_CARD_CODE = re.compile(
    r"(?i)\b(?:(?:cvc|cvv2?|cid|csc|security\s+code)\b\s*(?:is|:|=|#)?\s*\d{3,4}"
    r"|(?:card|atm|debit|credit)\s+pin\b\s*(?:is|:|=|#)?\s*\d{4,6}"
    r"|\bpin\s*(?:is|:|=)\s*\d{4})\b"
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _issued(digits: str) -> bool:
    for (lo, hi), lengths in _ISSUERS:
        width = len(str(lo))
        prefix = int(digits[:width])
        if lo <= prefix <= hi and len(digits) in lengths:
            return True
    return False


def has_card(text: str) -> bool:
    for match in _CARD_CANDIDATE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and _issued(digits):
            return True
    return bool(_CARD_CODE.search(text or ""))


# ── passwords ────────────────────────────────────────────────────────────────

#: "password: X", "my password is X", "pwd=X", "passcode X". The value is the next word.
_PASSWORD = re.compile(
    r"(?i)\b(?:pass(?:word|wd|code|phrase)|pwd)\b(?:\s+(?:for|to|on|of)(?:\s+[\w.@-]+){1,3})?\s*(?:is|was|:|=|-)\s*"
    r"[\"'`]?(?P<value>[^\s\"'`]{4,})"
)

#: Words that follow "password is" in a sentence about passwords rather than one that gives one.
_NOT_A_PASSWORD = frozenset("""
    required optional set unset wrong right correct incorrect invalid valid expired expiring saved stored missing
    needed empty blank reset changed change changing the a an my your our their his her its not too still same
    secure insecure weak strong protected hidden shown visible case ok okay fine working broken locked unlocked
    stolen leaked compromised forgotten lost known unknown different new old long short simple complex manual
    automatic encrypted hashed there here this that what which managed rotated enabled disabled
""".split())


def has_password(text: str) -> bool:
    for match in _PASSWORD.finditer(text or ""):
        value = match.group("value").strip(".,;:!?)")
        low = value.lower()
        if len(value) < 4 or low in _NOT_A_PASSWORD:
            continue
        if value[0] in "<[{(*" or set(low) <= {"*", "x", "."} or low.startswith(("xxx", "***", "your", "my_", "<")):
            continue  # a placeholder: <password>, ****, xxxx, your-password
        return True
    return False


# ── the door ─────────────────────────────────────────────────────────────────

#: Characters that look like a dash, a space or like nothing, folded wherever the door reads a message (F1280: a key
#: pasted with U+2011 hyphens went past every pattern, and the planner wrote it back out in ASCII to its logs): the one
#: table held values are matched through too (`secret_scrub.LOOKALIKES`, F1339). Read, never kept (F1551): the message
#: runs as it was written, each key masked where it stands (`secret_scrub.mask_as_written`, F1386's way).
_LOOKALIKES = LOOKALIKES


def _without_held(text: str, held: Any) -> tuple[str, int, set[str]]:
    """`text` with each secret the workspace holds taken out by its exact value (F1307), how many, and their kinds.
    `held` is `secret_scrub.held_in(text)`'s answer: (value, kind) pairs, the longest first."""
    removed, kinds = 0, set()
    for value, kind in held or ():
        if isinstance(value, str) and value and value in text:
            removed += text.count(value)
            kinds.add(str(kind))
            text = text.replace(value, CREDENTIAL_MARK)
    return text, removed, kinds


def check(text: str, held: Any = (), *, card_up: bool = False) -> Checked:
    """A new message to the builder: refused (card, password), or the text to run with keys masked. `held`: the
    secrets the workspace holds that occur in it (`secret_scrub.held_in`), taken out by exact value first (F1307),
    before the lookalikes are folded, so a held value is matched as it was saved. `card_up`: the conversation's latest
    turn put up a Connect card (`turn_facts.card_up_before`), so a message that is one secret-looking word alone is the
    card's key, masked as one (F1345). Every rule reads the message with its lookalikes folded (F1280); what runs is
    the message as written, each key masked where it stands (F1551: a pasted prompt keeps its dashes and spaces)."""
    text, from_held, kinds = _without_held(text or "", held)
    seen = text.translate(_LOOKALIKES)
    if has_card(seen):
        return Checked(text="", refused="card")
    if has_password(seen):
        return Checked(text="", refused="password")
    masked, removed = scrub_credentials(text)  # each shape reads it folded and masks as written (F1386)
    lone = _LONE_WORD.match(masked.translate(_LOOKALIKES)) if card_up and CREDENTIAL_MARK not in masked else None
    if lone and _secret_looking(lone.group("value")):
        masked, removed = CREDENTIAL_MARK, removed + 1
    # The held notice only when what came out is the workspace's own secret alone: a connection's, or a key by its
    # shape, gets the key's notice (connect it on its card).
    reason = "held" if from_held and not removed and kinds <= {"workspace"} else "masked"
    return Checked(text=_as_read(masked), removed=removed + from_held, reason=reason)


def _as_read(text: str) -> str:
    """`text` as written, but an approval said with lookalikes ("looks good" with a no-break space) as its gates read it
    (F1551): a bare yes carries no typography worth keeping, and the builder's gates and `review.is_yes` compare its
    words exactly, as they read every message before the door kept them as written."""
    from core.native_builder.review import is_yes

    folded = text.translate(_LOOKALIKES)
    return folded if folded != text and is_yes(folded) and not is_yes(text) else text


#: A question that asks the person for a secret. Its answer is read in that light: a lone secret-looking word in
#: reply to "what's the portal's password?" is the password, though it says no "password" itself.
_ASKS_PASSWORD = re.compile(r"(?i)\b(?:pass(?:word|wd|code|phrase)|pwd|log\s*in\s+details|credentials)\b")
_ASKS_KEY = re.compile(r"(?i)\b(?:api[\s_-]?key|key|token|secret|bearer|access\s+code|client\s+secret|"
                       r"authenticat\w*)\b")
_LONE_WORD = re.compile(r"^\s*[\"'`]?(?P<value>[^\s\"'`]{6,200})[\"'`]?\s*[.!]?\s*$")
#: A value after a bare "key" in an answer while a key is asked for (F1387): 12+ characters mixing letters and digits.
#: "the key user_id1" is a sentence; a model's name, with no "key" before it, is not a key.
_BARE_KEY = re.compile(r"(?i)(\bkey\s+)(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])"
                       r"([A-Za-z0-9][A-Za-z0-9_-]{10,}[A-Za-z0-9])(?![A-Za-z0-9_-])")


def _secret_looking(value: str) -> bool:
    """One word that reads like a secret: letters with digits or symbols, not a URL, an email or a plain word."""
    if "://" in value or ("@" in value and "." in value.split("@")[-1]):
        return False
    low = value.lower()
    if low in _NOT_A_PASSWORD or low in ("approve", "approved", "yes", "no", "stop", "done", "skip"):
        return False
    has_alpha = any(c.isalpha() for c in value)
    has_other = any(c.isdigit() or not c.isalnum() for c in value)
    return has_alpha and has_other


def notice(reason: Refusal | Literal["masked", "held"], kept: str, *, final: bool) -> dict:
    """A `builder_notice` event: the fixed sentence for `reason`, and the message as kept (omitted when empty)."""
    said = {"masked": MASKED_NOTICE, "held": HELD_NOTICE}.get(reason) or REFUSALS[reason]
    event = {"type": "builder_notice", "reason": reason, "message": said, "final": final}
    return {**event, "kept": kept} if kept else event


class Refused(ValueError):
    """An answer to a builder run that the door refuses; `str()` is the sentence the person reads."""

    def __init__(self, reason: Refusal):
        self.reason = reason
        super().__init__(REFUSALS[reason])


#: The builder's waits at a Connect card (F946): the key belongs on the card, so a lone key-like answer there is one.
CONNECT_WAITS = frozenset({"step_wait_connect"})


def check_answer(response: Any, question: str | None = None, *, step: str | None = None,
                 held: Any = (), card_up: bool = False) -> tuple[Any, int]:
    """An answer to a builder run's question with every string checked: `(answer with keys masked, how many)`.

    Raises `Refused` for a card number or a password anywhere in it, before anything is resumed or kept.
    `question` is what the run asked: in reply to one that asks for a password, a lone secret-looking word is
    refused as one; in reply to one that asks for a key or token, it is masked as one. `step`: the step the run waits
    at; at a Connect card's (`CONNECT_WAITS`), whose key belongs on the card, a lone secret-looking word is masked as a
    key whatever the card's words say (F1280: a key of no known shape, pasted alone at the wait, reached the run, its
    events and logs). `card_up`: the run put up a Connect card, or its plan asks for a key on one after the yes
    (`turn_facts.card_up`), whatever step asks now — its plan's review, a question — and a lone secret-looking word is
    the card's key the same (F1345, F1387). The run's own state, never a guess from words. While a key is asked for, a
    value after a bare "key" in the answer is that key (F1387). `held`: the secrets the workspace holds that occur in it
    (`secret_scrub.held_in`), taken out by exact value (F1307).
    """
    removed = 0
    asks_password = bool(question and _ASKS_PASSWORD.search(question))
    asks_key = step in CONNECT_WAITS or card_up or bool(question and _ASKS_KEY.search(question))

    def walk(value: Any) -> Any:
        nonlocal removed
        if isinstance(value, str):
            checked = check(value, held)
            if checked.refused:
                raise Refused(checked.refused)
            removed += checked.removed
            text = checked.text
            if asks_key:
                # While a key is asked for, a value after a bare "key" is that key (F1387), read folded and masked as
                # written (F1551).
                text, bare = mask_as_written(_BARE_KEY, text)
                removed += bare
            lone = _LONE_WORD.match(text.translate(_LOOKALIKES))
            if lone and (asks_password or asks_key) and CREDENTIAL_MARK not in text \
                    and _secret_looking(lone.group("value")):
                if asks_password:
                    raise Refused("password")
                removed += 1
                return CREDENTIAL_MARK
            return _as_read(text)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(response), removed


def kept_answer(response: Any) -> str:
    """The text of a checked answer the panel shows in place of the person's: the one string a panel's answer
    holds (`{"answer": "…"}`), or "" when the answer is not one string."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict) and len(response) == 1:
        (only,) = response.values()
        return only if isinstance(only, str) else ""
    return ""


def mask_history(history: list[dict] | None, held: Any = ()) -> list[dict]:
    """Earlier turns the client sends back: a refused one is dropped, keys (and the workspace's held secrets, F1307)
    are masked in the rest."""
    out = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        checked = check(str(turn.get("content") or ""), held)
        if checked.refused:
            continue
        out.append({**turn, "content": checked.text})
    return out


__all__ = ["CONNECT_WAITS", "CREDENTIAL_MARK", "Checked", "HELD_NOTICE", "MASKED_NOTICE", "NOT_KEPT", "REFUSALS", "Refused", "check",
           "check_answer", "has_card", "has_password", "kept_answer", "mask_history", "notice"]

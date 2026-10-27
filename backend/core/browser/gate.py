"""What the browser's agent may do, and what its pages may send.

Two gates, both code, so neither depends on what the model was told:

* **Tools.** Playwright MCP sits behind the component; only `ALLOWED` reaches it.
  `browser_evaluate` and `browser_run_code_unsafe` run arbitrary code (Playwright's
  own docs call the second RCE-equivalent), the file, storage, network and devtools
  tools reach past the page, and screenshots are the component's own.
* **The network.** Every request of every page passes `verdict`: http(s) only, a
  public address only (`core.egress_guard`, the scraper's rule), and in read-only
  mode nothing that changes state. The check sits on the browser's own network
  layer, so redirects, scripts and in-page fetches pass it too. A page's WebSocket
  passes `socket_verdict`, its own check, before it opens.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from core.egress_guard import check_host

#: Tools the agent may call, with the tags the component vouches for (TAG1).
#: Clicking and typing are not read-only, but what one click *does* is decided at
#: the network, not by a tag.
ALLOWED: dict[str, dict[str, bool]] = {
    "browser_navigate": {"read_only": True, "open_world": True},
    "browser_navigate_back": {"read_only": True, "open_world": True},
    "browser_snapshot": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_find": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_wait_for": {"read_only": True, "open_world": True, "idempotent": True},
    "browser_tabs": {"read_only": True, "open_world": True},
    "browser_hover": {"read_only": True, "open_world": True},
    "browser_click": {"open_world": True},
    "browser_type": {"open_world": True},
    "browser_fill_form": {"open_world": True},
    "browser_select_option": {"open_world": True},
    "browser_press_key": {"open_world": True},
    "browser_handle_dialog": {"open_world": True},
    # Ours, not Playwright MCP's: the person takes the wheel (V2-CONTRACT "Final buttons and sends").
    "browser_hand_over": {"read_only": True, "open_world": False},
}

#: Actions after which the page may look different: each gets a screenshot.
CHANGES_PAGE = frozenset(ALLOWED) - {"browser_snapshot", "browser_find", "browser_wait_for", "browser_hand_over"}

#: Methods that read. Anything else changes state somewhere.
_READS = frozenset({"GET", "HEAD", "OPTIONS"})

READ_ONLY = "read_only"
CO_DRIVE = "co_drive"
MODES = frozenset({READ_ONLY, CO_DRIVE})


def refusal(name: str) -> str | None:
    """Why the agent may not call `name` here, or None when it may."""
    if name in ALLOWED:
        return None
    return f"{name} is not available in Synapse's browser."


async def verdict(url: str, method: str, mode: str, *, sites: list[str] | None = None) -> str | None:
    """Why this request must not leave the browser, or None to let it go.

    `sites`, given for the agent's top-level navigation only, is the agent's scope:
    a page elsewhere is refused, redirects included. Never fails open: a
    resolution failure or a bug here refuses the request.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "a malformed address"
    if parsed.scheme not in {"http", "https"}:
        return f"{parsed.scheme or 'a schemeless'} address"
    host = parsed.hostname or ""
    try:
        await check_host(host)
    except Exception:
        return f"{host or 'that host'} is not a public address"
    if sites and not site_allowed(host, sites):
        return f"{host} is not one of this agent's sites"
    if mode != CO_DRIVE and method.upper() not in _READS:
        return f"a {method.upper()} to {host}: this browser is read-only"
    return None


async def socket_verdict(url: str, mode: str, *, scope: list[str] | None = None) -> str | None:
    """Why a page's WebSocket must not open, or None to let it (F1122).

    Once open, a socket carries anything both ways with no request to check, so a
    read-only browser opens none, and co-drive opens one only to a public address.
    `scope`, given while the agent drives, is its listed and once-allowed sites: a
    socket opens only on one of those sites (a subdomain counts, where a site's live
    socket usually is), and with none, nowhere (V2-CONTRACT, Naveen 2026-10-28).
    Never fails open, as `verdict`.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "a malformed address"
    if parsed.scheme not in {"ws", "wss"}:
        return f"{parsed.scheme or 'a schemeless'} socket"
    host = parsed.hostname or ""
    try:
        await check_host(host)
    except Exception:
        return f"{host or 'that host'} is not a public address"
    if mode != CO_DRIVE:
        return f"a WebSocket to {host}: this browser is read-only"
    if scope is not None and not any(same_site(host, site.removeprefix("*.")) for site in scope):
        return f"a WebSocket to {host}: not one of the agent's sites"
    return None


def site_allowed(host: str, sites: list[str]) -> bool:
    """`host` is one of `sites` ("portal.example.com", or "*.example.com" for it and its subdomains)."""
    host = (host or "").lower().rstrip(".")
    for site in sites:
        if site.startswith("*."):
            base = site[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == site:
            return True
    return False


def _site_of(host: str) -> str:
    """The registrable part, near enough: the last two labels (three under a two-letter country's
    second level, as in example.co.uk)."""
    labels = (host or "").lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "net", "org", "gov", "ac", "edu"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _site_of(a) == _site_of(b)


#: Tools that put the agent's text into a page.
TYPES_TEXT = frozenset({"browser_type", "browser_fill_form", "browser_press_key", "browser_select_option"})

#: Frames that are a CAPTCHA or a bot check: the person's to solve (DESIGN.md §3.6).
CAPTCHA_HOSTS = ("google.com/recaptcha", "recaptcha.net", "hcaptcha.com", "challenges.cloudflare.com")


def is_captcha_frame(url: str) -> bool:
    return any(marker in (url or "") for marker in CAPTCHA_HOSTS)


#: What makes a field the person's: a password, a card, a one-time code (DESIGN.md §2.4).
SENSITIVE_AUTOCOMPLETE = ("cc-", "one-time-code", "current-password", "new-password")


#: What the box says when an answer holds a card number (DESIGN.md §2.4, F1100).
CARD_WORDS = "Card details go in the browser; you'll be asked there."

_CARD_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def looks_like_card(text: str) -> bool:
    """A 13–19 digit run (spaces or dashes allowed) that passes the Luhn check: a payment card's number."""
    for match in _CARD_RUN.finditer(text or ""):
        digits = [int(c) for c in match.group(0) if c.isdigit()]
        if not 13 <= len(digits) <= 19:
            continue
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2:
                d *= 2
                d -= 9 if d > 9 else 0
            total += d
        if total % 10 == 0:
            return True
    return False


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings_in(v)]
    return []


def is_sensitive_field(input_type: str, autocomplete: str) -> bool:
    if (input_type or "").lower() == "password":
        return True
    tokens = (autocomplete or "").lower().split()
    return any(t.startswith(SENSITIVE_AUTOCOMPLETE) for t in tokens)


# ── final buttons (Naveen 2026-10-28; V2-CONTRACT "Final buttons and sends") ──
#
# The stop sits at the agent's press, before anything runs: holding a request the page's own code waits on breaks
# the page. Who decided matters more than how serious a button looks, so a press that acts for the person runs only
# on a quote of their own words (`quote_found`, plain matching, never a model's judgement), and money, permanent
# deletes and closing an account are always theirs.

#: Tools that can press something: a click, typing that submits, Enter, accepting a page's dialog.
PRESSES = frozenset({"browser_click", "browser_type", "browser_press_key", "browser_handle_dialog"})
#: The agent's own way to give the person the wheel (no Playwright MCP tool).
HAND_OVER = "browser_hand_over"
FREE, ACTS, MONEY = "free", "acts", "money"

#: Where payments are made: any press inside one of their frames is the person's.
PAYMENT_SITES = ("stripe.com", "stripe.network", "paypal.com", "paypalobjects.com", "braintreegateway.com",
                 "braintree-api.com", "adyen.com", "adyenpayments.com", "checkout.com", "razorpay.com",
                 "squareup.com", "squarecdn.com", "klarna.com", "affirm.com", "afterpay.com", "paddle.com",
                 "chargebee.com", "recurly.com", "authorize.net", "2checkout.com", "worldpay.com", "payu.in",
                 "payu.com", "cashfree.com", "paytm.com", "phonepe.com", "mollie.com", "gocardless.com",
                 "pay.google.com")

_MONEY_WORDS = re.compile(
    r"\b(pay|payment|buy|purchase|order|checkout|check out|subscribe|subscription|upgrade|donate|donation|"
    r"transfer|send money|withdraw|deposit|trade|sell|tip|renew|book now|reserve|add (a )?card|"
    r"start (my |your |a )?(free )?trial)\b")
_GONE_WORDS = re.compile(
    r"\b(delete|erase|destroy|deactivate|terminate|wipe|close (my |your |the |this )?account|"
    r"remove (my |your |the |this )?account|cancel (my |your |the |this )?"
    r"(subscription|membership|plan|order|account|booking|reservation))\b")
_ACTS_WORDS = re.compile(
    r"\b(submit|send|post|publish|share|apply|sign up|signup|register|join|enrol|enroll|accept|approve|confirm|"
    r"agree|invite|reply|respond|comment|save|update|rsvp|vote|rate|review|request|schedule|book|upload|"
    r"unsubscribe|follow|connect|message|report|claim|redeem|sign|verify|log in|login|sign in|create|remove|"
    r"archive|forward|repost|retweet|like|finish|complete)\b")
#: Buttons that only move around or close something, said exactly (a cookie banner's Accept among them).
_FREE_LABELS = re.compile(
    r"^(accept( all)?( cookies)?|allow all( cookies)?|reject all( cookies)?|got it|ok|okay|close|dismiss|cancel|"
    r"no thanks|not now|skip|back|previous|next|continue|search|go|filter|filters|apply filters?|sort|"
    r"load more|show more|see more|view|open|more|menu)$")
_CHECKOUT_PAGE = re.compile(r"\b(checkout|check out|payment|billing|place (your )?order|(order|purchase) (review|summary)|"
                            r"review (your )?(order|purchase)|cart|basket)\b")
#: Words that only read or take a copy, whatever else the label says ("Download report").
_READ_WORDS = re.compile(r"\b(download|export|print|preview|copy)\b")
_CHECKOUT_STEP = re.compile(r"^(next|continue|confirm|place|complete|proceed|review)\b")


def _words(text: str) -> str:
    """Lower case, apostrophes dropped ("don't" → "dont"), anything else that isn't a letter or digit a space."""
    text = (text or "").casefold().replace("’", "'").replace("'", "")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def classify_press(info: dict, page_url: str) -> tuple[str, str]:
    """`(tier, what it does)` for a press the agent is about to make, from what the page says about the element.

    `info` is the element's own description, read in the page: `label`, `tag`, `type`, `href`, `in_form`,
    `submits`, `form_method`, `form_search`, `field` (a text box or a select, not a button), `editable`,
    `nothing` (Enter with nothing pressable focused), `frame` (its frame's address), `dialog` for a page's own
    dialog, `key` for Enter. Read in that order: a payment frame, nothing to press, a field the cursor goes into,
    a real link, money and permanent words, a checkout page, exact free labels, the form it submits, words that
    only read, a dialog, Enter in a message box, words that act for the person.
    """
    host = urlparse(page_url or "").hostname or "this site"
    frame_host = urlparse(info.get("frame") or "").hostname or ""
    label = _words(info.get("label") or "")
    tag = (info.get("tag") or "").lower()
    if frame_host and any(same_site(frame_host, site) for site in PAYMENT_SITES):
        return MONEY, f"it is inside {frame_host}'s payment form"
    if info.get("nothing"):
        return FREE, "nothing that sends is focused"
    if info.get("field") and not info.get("key"):
        return FREE, "it only puts the cursor in a field"
    href = (info.get("href") or "").strip()
    if tag == "a" and href and not href.startswith(("#", "javascript:")):
        return FREE, "it opens a page"
    if _MONEY_WORDS.search(label):
        return MONEY, f"it may pay, order or subscribe on {host}"
    if _GONE_WORDS.search(label):
        return MONEY, f"it may delete or close something on {host} for good"
    submits = bool(info.get("submits"))
    page_words = _words(urlparse(page_url or "").path.replace("/", " ") + " " + (info.get("title") or ""))
    if _CHECKOUT_PAGE.search(page_words) and (submits or _CHECKOUT_STEP.search(label)):
        return MONEY, f"it is a step of the checkout on {host}"
    if label and _FREE_LABELS.match(label):
        return FREE, "it only moves around the page"
    if submits:
        if (info.get("form_method") or "get").lower() != "post" or info.get("form_search"):
            return FREE, "it only searches or reads"
        return ACTS, f"it submits the form on {host}"
    if _READ_WORDS.search(label):
        return FREE, "it only reads or takes a copy"
    if info.get("dialog"):
        return (ACTS, f"it confirms '{info.get('label')}' on {host}") if _ACTS_WORDS.search(label) \
            else (FREE, "it closes a message")
    if info.get("key") and info.get("editable") and tag != "input":
        return ACTS, f"Enter sends what is in the message box on {host}"
    if _ACTS_WORDS.search(label):
        return ACTS, f"it may send or change something on {host} for the person"
    return FREE, "it only moves around the page"


#: Words that turn a request around. One in the quote, or before it in the same sentence, and it is not a yes.
_NEGATIONS = frozenset({"not", "no", "never", "dont", "don", "doesnt", "didnt", "without", "stop", "nothing",
                        "neither", "nor", "cannot", "cant", "wont", "shouldnt", "mustnt", "avoid", "neednt"})
#: A quote must ask for an action in so many words.
_ACTION_WORDS = frozenset(set(re.findall(r"[a-z]+", _ACTS_WORDS.pattern)) - {"b", "in", "up"}
                          | {"press", "click", "hit", "fill", "enter", "go", "ahead"})
#: The fewest words a quote may be: one word ("submit") could come from anywhere.
QUOTE_MIN_WORDS = 3


def quote_found(quote: str, texts) -> bool:
    """Whether `quote` is the person's own words asking for an action: it appears in one of `texts` (their
    messages, the agent's instructions, the step's own text) whole-word, ignoring case, spacing and punctuation;
    it is at least `QUOTE_MIN_WORDS` words and names an action; and no word in it, or before it in the same
    sentence, turns it around ("don't submit the form" is no yes to "submit the form").
    """
    q = _words(quote)
    words = q.split()
    if len(words) < QUOTE_MIN_WORDS or _NEGATIONS & set(words) or not _ACTION_WORDS & set(words):
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(q)}(?!\w)")
    for text in texts or ():
        for sentence in re.split(r"[.!?;\n]+", text or ""):
            s = _words(sentence)
            for match in pattern.finditer(s):
                if not _NEGATIONS & set(s[:match.start()].split()):
                    return True
    return False


#: Addresses that take money, by their words: a write to one is held at the network for the person (F1125).
_MONEY_PATH = re.compile(r"\b(checkout|check out|payments?|pay|billing|purchases?|orders?|subscribe|subscriptions?|"
                         r"donate|donations?|charges?|transactions?|transfers?)\b")


def is_money_send(method: str, url: str, *, page_url: str) -> bool:
    """The network's backstop behind the click stop (F1125): a write to a payment provider, to an address that
    takes money, or from a checkout page. The person sends it themselves; nothing else is held."""
    if method.upper() in _READS:
        return False
    parsed = urlparse(url)
    if any(same_site(parsed.hostname or "", site) for site in PAYMENT_SITES):
        return True
    page = urlparse(page_url or "")
    return bool(_MONEY_PATH.search(_words(parsed.path.replace("/", " ")))
                or _CHECKOUT_PAGE.search(_words(page.path.replace("/", " "))))

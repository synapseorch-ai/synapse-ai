"""What a customer may change about how their widget looks.

Deliberately a **closed vocabulary** rather than free CSS. The widget renders on
somebody else's website; every knob here is a thing we then have to keep looking
right at 320px, in both colour schemes, next to whatever else is on that page.
Three constraints shaped the list, and they are worth stating because the
temptation to widen it will come back:

**No webfonts.** The fonts below are stacks the visitor's device already has, so
they cost zero bytes and make no third-party request. A Google Font would be a
request from the customer's visitors to a third party — a live GDPR problem in
the EU, where sites have been ruled against for exactly that — and a font that
loads late repaints a widget sitting on a page that is not ours. Intercom offers
no Messenger font at all; this is strictly more than that, for nothing.

**One colour, derived.** The customer picks an accent and everything else is
computed from it or from the theme. Crisp arrived at the same answer after
starting with a fuller palette: people do not want to pick eleven colours, and
the eleven they pick rarely agree with each other.

**A logo, not a stylesheet.** The one thing every competitor offers and Dify
still does not.
"""
from __future__ import annotations

#: Font stacks, by the name the settings screen shows. The *values* live in the
#: widget's stylesheet as `[data-font=…]` rules; these are the names the API
#: validates, so a font added in one place and not the other fails loudly here
#: rather than rendering as the default and looking like a bug.
FONTS = ("system", "sans", "serif", "rounded", "mono")

#: `auto` follows the visitor's own OS setting, which is the right default: a
#: widget that ignores dark mode on a dark site is the complaint this answers.
THEMES = ("auto", "light", "dark")

#: A solid accent header, or a quiet one with the accent kept for buttons and the
#: visitor's own bubbles. Brands with a loud primary colour want the quiet one.
HEADER_STYLES = ("accent", "neutral")

#: The floating button: our chat glyph, or the customer's logo. The logo is the
#: most visible piece of branding on the page — it is what a visitor sees before
#: they click anything — so it is worth the one line it costs.
LAUNCHERS = ("bubble", "logo")

POSITIONS = ("right", "left")

#: Offered on the settings screen beside a free hex field. Not a restriction —
#: a palette is faster than a colour picker for the common case, and these are
#: chosen to stay legible with white text on top, which a customer's own brand
#: hex may not be. See `readable_on`.
PRESET_ACCENTS = (
    "#4f46e5",  # indigo
    "#2563eb",  # blue
    "#0891b2",  # cyan
    "#059669",  # emerald
    "#ca8a04",  # amber
    "#dc2626",  # red
    "#db2777",  # pink
    "#7c3aed",  # violet
    "#475569",  # slate
    "#18181b",  # near-black
)

#: A logo is inlined into the embed page as a data URI, so its size is paid on
#: every render. 128 KB is generous for something drawn at 28px and small enough
#: that inlining beats inventing a public asset route with its own cache and
#: abuse story.
MAX_LOGO_BYTES = 128 * 1024

_HEX = "0123456789abcdefABCDEF"


def _one_of(value: object, allowed: tuple[str, ...], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def normalise_accent(value: object) -> str:
    """A `#rrggbb`, or the default. Shorthand is expanded; anything else falls
    back rather than reaching a stylesheet — this string is interpolated into a
    CSS custom property, and the only reason that is safe is that nothing but a
    hex colour ever gets through here."""
    default = PRESET_ACCENTS[0]
    if not isinstance(value, str):
        return default
    raw = value.strip()
    if not raw.startswith("#"):
        return default
    body = raw[1:]
    if len(body) == 3 and all(c in _HEX for c in body):
        return "#" + "".join(c * 2 for c in body).lower()
    if len(body) == 6 and all(c in _HEX for c in body):
        return "#" + body.lower()
    return default


def readable_on(accent: str) -> str:
    """`"#ffffff"` or `"#18181b"` — whichever can be read on top of `accent`.

    A customer whose brand colour is a pale yellow gets black text instead of
    white-on-yellow, without being asked to think about it. Relative luminance
    per WCAG, with the sRGB gamma step, because the cheap `(r+g+b)/3` version
    calls mid-greens dark and picks the wrong one on exactly the colours people
    choose.
    """
    accent = normalise_accent(accent)
    channels = [int(accent[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    # 0.45 rather than 0.5: white text holds up slightly further down the range
    # than black does, and this is the crossover where both stay above 4.5:1.
    return "#18181b" if luminance > 0.45 else "#ffffff"


def clean(document: dict) -> dict:
    """Every branding field, validated, with the rest of the document untouched.

    Called on save in both products so the embed page never has to defend itself
    against a value the API let through.
    """
    document["accent"] = normalise_accent(document.get("accent"))
    document["font"] = _one_of(document.get("font"), FONTS, "system")
    document["theme"] = _one_of(document.get("theme"), THEMES, "auto")
    document["header_style"] = _one_of(document.get("header_style"), HEADER_STYLES, "accent")
    document["launcher"] = _one_of(document.get("launcher"), LAUNCHERS, "bubble")
    document["position"] = _one_of(document.get("position"), POSITIONS, "right")
    return document


def public_theme(widget: dict) -> dict:
    """What the embed page and the preview both render from.

    One shape, so a preview cannot drift from the thing it previews — the whole
    reason the preview renders the real widget rather than a mock.
    """
    accent = normalise_accent(widget.get("accent"))
    return {
        "accent": accent,
        "on_accent": readable_on(accent),
        "font": _one_of(widget.get("font"), FONTS, "system"),
        "theme": _one_of(widget.get("theme"), THEMES, "auto"),
        "header_style": _one_of(widget.get("header_style"), HEADER_STYLES, "accent"),
        "launcher": _one_of(widget.get("launcher"), LAUNCHERS, "bubble"),
        "position": _one_of(widget.get("position"), POSITIONS, "right"),
        "logo": widget.get("logo") or "",
        "title": widget.get("title") or "",
        "greeting": widget.get("greeting") or "",
        "launcher_label": widget.get("launcher_label") or "Chat",
    }

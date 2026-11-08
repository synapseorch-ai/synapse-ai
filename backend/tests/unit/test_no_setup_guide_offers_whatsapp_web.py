"""No setup guide on the Channels screen offers WhatsApp Web (F1399, REACH1; the rest of F1355).

F1355 removed WhatsApp Web, the QR sign-in, in 2.0: saving one is refused and a 1.x channel is kept switched off. The
screen's WhatsApp setup guide still listed it as a second way in ("--- OR ---", "UNOFFICIAL PATH: … scan the QR code"),
so a person following the guide was told of a path the save then refuses. These pin the guides as the screen draws them,
`SETUP_GUIDES` in MessagingTab.tsx: no guide names WhatsApp Web, a QR code or an unofficial path, none offers another
path, and WhatsApp's still walks the Cloud API's.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCREEN = REPO / "frontend/src/components/settings/MessagingTab.tsx"
#: What a guide that offered the browser path would say, in any case.
OFFERS_THE_WEB = re.compile(r"whatsapp web|\bqr\b|unofficial|no business account|--- *or *---", re.IGNORECASE)


def _guides() -> dict[str, list[str]]:
    """Each platform's guide steps, read from the screen's `SETUP_GUIDES` object as it is written."""
    text = SCREEN.read_text(encoding="utf-8")
    start = text.index("const SETUP_GUIDES")
    block = text[start:text.index("\n};", start)]
    guides: dict[str, list[str]] = {}
    for platform, body in re.findall(r"^    (\w+): \{\n(.*?)^    \},", block, re.MULTILINE | re.DOTALL):
        steps = body[body.index("steps: ["):]
        guides[platform] = [m.group(2) for m in re.finditer(r"(['\"])((?:\\.|(?!\1).)*)\1,?\s*$", steps, re.MULTILINE)]
    return guides


def test_every_platform_has_its_guide_read():
    guides = _guides()
    assert set(guides) == {"telegram", "discord", "slack", "teams", "whatsapp"}, sorted(guides)
    assert all(guides.values()), {p: len(s) for p, s in guides.items()}


def test_no_guide_offers_whatsapp_web_or_another_path():
    offered = {p: [s for s in steps if OFFERS_THE_WEB.search(s)] for p, steps in _guides().items()}
    assert not any(offered.values()), f"a setup guide still offers the removed WhatsApp Web path: {offered}"


def test_whatsapps_guide_walks_the_cloud_api():
    steps = " ".join(_guides()["whatsapp"])
    for needed in ("developers.facebook.com", "/api/messaging/whatsapp/webhook/", "Verify Token", "Phone Number ID",
                   "App Secret"):
        assert needed in steps, f"WhatsApp's guide no longer says {needed!r}"
    assert not re.search(r"\bPATH\b", steps), "with one way in, a step names no path"

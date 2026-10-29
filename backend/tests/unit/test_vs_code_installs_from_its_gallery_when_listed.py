"""VS Code's sign-in link asks its gallery first, once the product is listed there (GUIDE1, F1215).

VS Code shows a server's logo and description on its install page and server list only from its gallery (the MCP
registry it reads, GitHub's by default), looked up by the name in the link; `"gallery": true` asks it to, and a name it
doesn't find opens the link's own config as before (VS Code's mcpWorkbenchService.ts). A listed entry installs the
gallery's config, which carries no key, so only a sign-in link may ask: a keyed link keeps its own. The product says
whether it is listed (`ConnectHost.registryName`); OSS never is, since each install's address is its own, and cloud
only once it is published (Naveen 10-29: prepare now, publish at launch). `components/connect/` is synced into cloud's
web, whose vitest runs the link itself.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
CONNECT = FRONTEND / "src" / "components" / "connect"


def _body(source: str, name: str) -> str:
    start = source.index(f"export function {name}")
    end = source.find("\nexport ", start + 1)
    return source[start:end if end != -1 else None]


def test_a_host_says_whether_it_is_listed():
    host = (CONNECT / "ConnectAiTools.tsx").read_text("utf-8")
    assert re.search(r"registryName\?: string", host), "ConnectHost names its registry entry, when it has one"


def test_the_vs_code_link_asks_the_gallery_only_without_a_key():
    link = _body((CONNECT / "clients.ts").read_text("utf-8"), "installLink")
    assert "registryName" in link and "gallery: true" in link
    assert re.search(r"registryName && !key", link), "a keyed link never asks the gallery: its entry has no key"


def test_the_card_passes_it_to_the_sign_in_links_only():
    card = (CONNECT / "ConnectCard.tsx").read_text("utf-8")
    assert "registryName: host.registryName" in card
    # The keyed link is built with the key and nothing from the registry.
    assert re.search(r"installLink\(client, \{ url: host\.mcpUrl, key \}\)", card)


def test_oss_is_never_listed():
    hosts = [p for p in (FRONTEND / "src").rglob("*.tsx") if "ConnectHost" in p.read_text("utf-8")
             and "connect/" not in p.as_posix()]
    assert hosts, "OSS's own hosts are found"
    for path in hosts:
        assert "registryName" not in path.read_text("utf-8"), f"{path.name}: OSS has no registry entry"

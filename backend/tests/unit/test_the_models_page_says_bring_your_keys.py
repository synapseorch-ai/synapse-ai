"""Settings › Models leads with the keys (GUIDE1, D318, F912).

Naveen: "it's not that evident where to add the key ... the content is not saying something like bring your keys ...
and grouping heading can be better". So the page opens on Providers, says "Bring your own keys" first, every
provider sits in a group that says what it is for, and a card with no key says "Add key" before it is opened.
`ProviderCard.tsx` is synced into cloud's web, so its half holds for both products.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
MODELS_TAB = FRONTEND / "components" / "settings" / "ModelsTab.tsx"
PROVIDER_CARD = FRONTEND / "components" / "providers" / "ProviderCard.tsx"
PROVIDER_CARDS = FRONTEND / "lib" / "providerCards.ts"


def _groups() -> list[dict]:
    source = MODELS_TAB.read_text("utf-8")
    table = re.search(r"const PROVIDER_GROUPS: [^=]+= \[(.+?)\n\];", source, re.S)
    assert table, "PROVIDER_GROUPS is gone from ModelsTab.tsx"
    groups = []
    for row in re.findall(r"\{ title: .+?\] \}", table.group(1), re.S):
        title = re.search(r"title: (['\"])(.*?)\1", row)
        blurb = re.search(r"blurb: (['\"])(.*?)\1", row)
        ids = re.search(r"ids: \[([^\]]*)\]", row)
        groups.append({
            "title": title.group(2) if title else "",
            "blurb": blurb.group(2) if blurb else "",
            "ids": re.findall(r"'([a-z_]+)'", ids.group(1)) if ids else [],
        })
    return groups


def _provider_ids() -> set[str]:
    source = PROVIDER_CARDS.read_text("utf-8")
    table = re.search(r"export const PROVIDER_META: [^=]+= \{(.+?)\n\};", source, re.S)
    assert table, "PROVIDER_META is gone from lib/providerCards.ts"
    return set(re.findall(r"^    ([a-z_]+): \{", table.group(1), re.M))


def test_every_provider_sits_in_exactly_one_group():
    placed = [i for g in _groups() for i in g["ids"]]
    assert len(placed) == len(set(placed)), "a provider is in two groups"
    assert set(placed) == _provider_ids(), "a provider has no group, or a group names one that doesn't exist"


def test_every_group_says_what_it_is_for():
    groups = _groups()
    assert len(groups) >= 4
    for g in groups:
        assert g["title"] and g["blurb"], f"group {g['title']!r} has no heading or no line"


def test_local_and_cli_models_are_apart():
    by_title = {g["title"]: set(g["ids"]) for g in _groups()}
    cli = {"anthropic_cli", "gemini_cli", "codex_cli", "github_copilot_cli"}
    assert any(ids == cli for ids in by_title.values()), "the CLI models are not a group of their own"
    assert any("ollama" in ids and not ids & cli for ids in by_title.values())


def test_the_page_opens_on_the_keys():
    source = MODELS_TAB.read_text("utf-8")
    assert "Bring your own keys" in source
    assert source.index('title="Providers"') < source.index('title="Defaults"')


def test_a_card_with_no_key_says_add_key():
    source = PROVIDER_CARD.read_text("utf-8")
    assert re.search(r">\s*Add key\s*<", source), "a card with no key has no 'Add key'"

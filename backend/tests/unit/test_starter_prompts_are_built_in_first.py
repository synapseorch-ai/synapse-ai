"""The prompts written for a new person are buildable with what any workspace has (ONB3, D326, F1059).

`frontend/src/components/starters/table.ts` is reviewed copy that lands in a new Synapse AI chat. Every row must
work on the built-in tools (web search, read and scrape a page, screenshots, the Vault) unless it names the app it
needs, and then its card says so. A row that sends someone to a scraping or search vendor breaks Naveen's
built-in-first rule (the Firecrawl offer on gethubservice, rc4.2), so the vendors' names fail here, in the words or
in `needs`. OSS Home's "I work in" offers each persona, so each must have rows of its own.
"""
from __future__ import annotations

import re
from pathlib import Path

STARTERS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "starters"
SRC_APP = STARTERS.parent / "app"

#: Scraping, crawling and search vendors: the built-ins do their job.
VENDORS = ("firecrawl", "apify", "browserbase", "simplescraper", "exa", "tavily", "serpapi", "scrapingbee",
           "brightdata", "bright data", "zenrows", "jina", "perplexity")


def _rows() -> list[dict]:
    table = (STARTERS / "table.ts").read_text("utf-8")
    body = table[table.index("export const STARTERS"):]
    rows = []
    for block in re.findall(r"\{\s*id: '[^']+'.*?\n    \}", body, re.S):
        rows.append({
            "id": re.search(r"id: '([^']+)'", block).group(1),
            "text": re.search(r"text: '([^']+)'", block).group(1),
            "personas": re.findall(r"'([a-z_]+)'", re.search(r"personas: \[([^\]]*)\]", block).group(1)),
            "goals": re.findall(r"'([a-z_]+)'", re.search(r"goals: \[([^\]]*)\]", block).group(1)),
            "needs": re.findall(r"'([a-z_]+)'", re.search(r"needs: \[([^\]]*)\]", block).group(1)),
            "cloud_only": "cloudOnly: true" in block,
        })
    return rows


def _need_labels() -> set[str]:
    table = (STARTERS / "table.ts").read_text("utf-8")
    block = re.search(r"NEED_LABELS: Record<string, string> = \{(.*?)\};", table, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+):", block, re.M))


def test_the_table_has_rows():
    rows = _rows()
    assert len(rows) >= 20
    assert len({r["id"] for r in rows}) == len(rows), "a starter id is used twice"


def test_no_row_names_a_scraping_or_search_vendor():
    offenders = [r["id"] for r in _rows()
                 if any(re.search(rf"\b{re.escape(v)}\b", (r["text"] + " " + " ".join(r["needs"])).lower())
                        for v in VENDORS)]
    assert not offenders, f"rows that send people to a vendor the built-ins replace: {offenders}"


def test_every_needed_app_is_named_on_its_card():
    labels = _need_labels()
    missing = sorted({n for r in _rows() for n in r["needs"]} - labels)
    assert not missing, f"needs with no NEED_LABELS entry: {missing}"


def test_a_row_that_needs_an_app_says_so_in_its_words():
    # The card's "Needs X" line is not the only place: the prompt itself asks to connect it, so Synapse AI's plan
    # puts the Connect card after the yes.
    for r in _rows():
        for need in r["needs"]:
            assert "connect" in r["text"].lower(), f"{r['id']} needs {need} but its words never say connect"


def test_rows_are_short_enough_to_read_on_a_card():
    long = [r["id"] for r in _rows() if len(r["text"]) > 240]
    assert not long, f"rows over 240 characters: {long}"


def test_every_home_persona_has_rows_of_its_own():
    pick = (STARTERS / "pick.ts").read_text("utf-8")
    block = re.search(r"export const WORK_IN.*?\];", pick, re.S).group(0)
    personas = re.findall(r"key: '([a-z_]+)'", block)
    assert personas, "WORK_IN lists no personas"
    rows = [r for r in _rows() if not r["cloud_only"]]  # OSS Home picks from self-hosted's rows
    thin = [p for p in personas
            if sum(1 for r in rows if p in r["personas"]) < 2]
    assert not thin, f"personas Home offers with fewer than two self-hosted rows: {thin}"


def test_every_row_asks_for_something_to_build():
    # F1105 (ONB3's measure on Claude Code Haiku, 2026-10-27): "Do a design audit of a page I give you" was answered
    # as a question, not built ("I can't take screenshots from here"); "Edit a draft I give you" read as a change to
    # nothing. Synapse AI builds: a starter says so in its first word.
    asks = [r["id"] for r in _rows() if not r["text"].startswith("Make ")]
    assert not asks, f"rows that do not ask for a workflow or an assistant to be made: {asks}"


def test_no_row_promises_a_start_the_product_does_not_have():
    # F1105: "When I upload a spreadsheet to the Vault" — a workflow cannot start on a file landing in the Vault, and
    # the builder said so instead of planning. A row starts its workflow by hand, on a schedule, or from what it is given.
    promised = [r["id"] for r in _rows()
                if re.search(r"\bwhen (i|we|you) (add|upload|drop|put|save)\b", r["text"].lower())]
    assert not promised, f"rows that promise a start on a file being added: {promised}"


def test_the_starters_naveen_named_are_there():
    # Naveen (2026-10-27): "e commerce agent for their business they'll paste open ai spec and we'll create tools and
    # agent, developer connects github and checks PRs, product manager connect jira and analyse and report, connect
    # figma for designers... and little more vast and varieties".
    rows = {r["id"]: r for r in _rows()}
    wanted = {
        "ecomagent": ("ecommerce", None, "openapi"),
        "prs": ("development", "github", "pull request"),
        "jirareport": ("reporting", "jira", "report"),
        "figmareview": ("design", "figma", "figma"),
    }
    for rid, (goal, need, words) in wanted.items():
        assert rid in rows, f"{rid} is not in the table"
        assert goal in rows[rid]["goals"], f"{rid} is not offered for the {goal} goal"
        assert (need in rows[rid]["needs"]) if need else not rows[rid]["needs"], f"{rid} needs {rows[rid]['needs']}"
        assert words in rows[rid]["text"].lower(), f"{rid} does not say {words!r}"
    assert rows["prs"]["personas"] == ["engineer"] and "product" in rows["jirareport"]["personas"]
    assert "designer" in rows["figmareview"]["personas"]
    assert len(rows) >= 40, "the table should cover far more than two prompts per person"


def test_rows_self_hosted_cannot_connect_yet_are_cloud_only():
    # ONB3's re-measure on Claude Code Haiku (2026-10-27): on self-hosted, GitHub, Gmail, Jira and Slack stop at "can't be
    # built as it is yet" — the builder finds their APIs but no Connect card for the token (no Apps catalogue there),
    # while Figma, Linear, Notion and Stripe reach one. Naveen: keep them on cloud (its catalogue connects them in a
    # click), hide them on self-hosted. OSS Home picks from `SELF_HOSTED_STARTERS`.
    rows = {r["id"]: r for r in _rows()}
    assert {i for i, r in rows.items() if r["cloud_only"]} == {"prs", "inbox", "jirareport", "slackbrief"}
    home = (SRC_APP / "Dashboard.tsx").read_text("utf-8")
    assert re.search(r"pickStarters\(\{[^)]*\},\s*2,\s*SELF_HOSTED_STARTERS\)", home), \
        "OSS Home must pick from the self-hosted rows"


def test_a_row_that_failed_the_measure_is_out():
    # ONB3's re-measure: "Make tools from the OpenAPI spec I paste, then a workflow…" planned the tools inside the daily
    # run and stopped at "can't be built" twice; the shopping assistant from a spec (ecomagent) is the way in.
    assert "apitools" not in {r["id"] for r in _rows()}


def test_most_rows_need_no_app():
    rows = _rows()
    built_in = [r for r in rows if not r["needs"]]
    assert len(built_in) >= len(rows) * 0.8, "built-in rows should be the rule, connected apps the exception"

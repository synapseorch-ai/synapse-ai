"""A page is read around what research reads it for, in 3000 characters (SAI2-P5, D300).

Research read about 150k tokens a persona after F765: up to 20 pages, each its first 6000 characters, mostly menus
and intros, while what decides a verdict (who may use the API, how one signs in, what it costs) is often further
down. A model now sees the page's title and the paragraphs that hold those words or the provider's, each with the
paragraph after it (a heading's body), in the page's order, at most 3000 characters; a page with no such words gives
its head.
"""
from __future__ import annotations

from core.builder_research import _PAGE_CHARS, page_extract

FILLER = "\n\n".join(f"Our story, part {i}. We love our customers and our team very much." for i in range(300))


def test_what_decides_a_verdict_deep_in_a_long_page_is_kept():
    page = ("# Booking.com for Business\n\n" + FILLER + "\n\n## Connectivity\n\nOur API is open only to approved "
            "partners. Apply to become a connectivity partner.\n\n" + FILLER)
    assert len(page) > 20_000
    got = page_extract(page)
    assert got.startswith("# Booking.com for Business")
    assert "open only to approved partners" in got and "Apply to become" in got
    assert len(got) <= _PAGE_CHARS == 3000
    assert "Our story, part 150" not in got


def test_a_heading_keeps_the_paragraph_after_it():
    page = "# Docs\n\n" + FILLER + "\n\n## Authentication\n\nSend your key in the X-Secret header.\n\n" + FILLER
    got = page_extract(page)
    assert "## Authentication\n\nSend your key in the X-Secret header." in got
    assert "…" in got, "a gap in the page is marked"


def test_the_providers_own_words_are_hits_too():
    page = "# Home\n\n" + FILLER + "\n\nHenry Schein orders are placed by phone.\n\n" + FILLER
    assert "placed by phone" in page_extract(page, ["henry", "schein"])


def test_a_page_with_nothing_to_find_gives_its_head():
    page = "# About\n\n" + FILLER
    got = page_extract(page)
    assert got == page[:3000]


def test_a_short_page_is_given_whole():
    assert page_extract("# Short\n\nAn API.") == "# Short\n\nAn API."


def test_the_cap_holds_however_many_hits():
    page = "\n\n".join(f"The API key {i} is sent in a header, with OAuth for partners." for i in range(500))
    assert len(page_extract(page)) <= 3000


def test_who_may_use_it_survives_sixty_api_sections():
    """SAI2's probe: the gating sentence after sixty sections of API reference was cut."""
    sections = "\n\n".join(f"## Endpoint {i}\n\nThe API reference for endpoint {i}, with OAuth." for i in range(60))
    page = "# Hotel Connectivity API\n\n" + sections + "\n\nAvailable to approved partners only. Apply here."
    got = page_extract(page)
    assert "approved partners only" in got and len(got) <= 3000


def test_a_page_with_no_api_word_keeps_its_gate():
    page = "# Integrations\n\n" + FILLER + "\n\nIntegrations are not offered to the public; contact our sales team."
    assert "not offered to the public" in page_extract(page)


def test_json_and_read_only_words_never_crowd_out_the_partner_line():
    """SAI2's review, S2: "application/json", "private key" and "read-only" are not who may use it."""
    body = "\n\n".join(f"Send `Content-Type: application/json` with a private key; field {i} is read-only."
                         for i in range(80))
    page = "# API reference\n\n" + body + "\n\nThis API is available to approved partners only."
    got = page_extract(page)
    assert "approved partners only" in got and len(got) <= 3000


# ── F1128: the calls a spec needs (P3's re-measure: 0 of 17 `api` options had a spec) ──────────────────────────────
# A method's section holds no access word ("#### sendMessage / Use this method to send text messages"), so the
# extract dropped it, and the section the model asked for (core.telegram.org/bots/api#sendmessage) was not read for.

METHODS = "\n\n".join(
    f"#### [](https://core.telegram.org/bots/api#method{i})method{i}\n\nUse this method to do thing {i}. On success, "
    f"True is returned.\n\n| Parameter | Type | Required |\n| --- | --- | --- |\n| chat_id | Integer | Yes |"
    for i in range(120))
TELEGRAM = ("# Telegram Bot API\n\n> The Bot API is an HTTP-based interface created for developers keen on building "
            "bots for Telegram.\n\n### Authorizing your bot\n\nEach bot is given a unique authentication token when "
            "it is created. The token looks something like 123456:ABC-DEF.\n\n### Making requests\n\nAll queries to "
            "the Telegram Bot API must be served over HTTPS: https://api.telegram.org/bot<token>/METHOD_NAME\n\n"
            + METHODS + "\n\n#### [](https://core.telegram.org/bots/api#sendmessage)sendMessage\n\nUse this method "
            "to send text messages. On success, the sent Message is returned.\n\n| Parameter | Type | Required |\n"
            "| --- | --- | --- |\n| chat_id | Integer or String | Yes |\n| text | String | Yes |\n\n"
            "#### [](https://core.telegram.org/bots/api#forwardmessage)forwardMessage\n\nUse this method to forward "
            "messages of any kind.\n\n" + METHODS)


def test_a_fragment_reads_the_section_it_names():
    assert len(TELEGRAM) > 20_000
    got = page_extract(TELEGRAM, ["telegram"], section="sendmessage")
    assert "Use this method to send text messages." in got
    assert "| text | String | Yes |" in got, "the section's table comes with it"
    assert "forward messages of any kind" not in got, "the next method is another section"
    assert got.startswith("# Telegram Bot API") and len(got) <= 3000


def test_a_fragment_matches_a_plain_heading_too():
    page = "# Docs\n\n" + FILLER + "\n\n## Create a member\n\nAdds one person to a list.\n\n## Next\n\nOther."
    got = page_extract(page, section="create-a-member")
    assert "## Create a member\n\nAdds one person to a list." in got and "Other." not in got


def test_a_fragment_that_names_no_heading_reads_as_today():
    assert page_extract(TELEGRAM, ["telegram"], section="nosuchmethod") == page_extract(TELEGRAM, ["telegram"])


def test_a_method_and_a_path_is_a_hit():
    page = ("# Marketing API reference\n\n" + FILLER + "\n\nAdd a member to the list.\n\n"
            "POST /lists/{list_id}/members\n\n" + FILLER + "\n\n| Method | Path |\n| --- | --- |\n"
            "| GET | /lists/{list_id}/members/{subscriber_hash} |\n\n" + FILLER)
    got = page_extract(page, ["mailchimp"])
    assert "POST /lists/{list_id}/members" in got
    assert "| GET | /lists/{list_id}/members/{subscriber_hash} |" in got, "a table row with a method and a path"


def test_a_code_block_is_a_hit():
    page = ("# Quick start\n\n" + FILLER + "\n\n```bash\ncurl -X POST https://us6.example.com/3.0/lists/abc/members "
            "-H 'Content-Type: application/json'\n```\n\n" + FILLER)
    assert "curl -X POST https://us6.example.com/3.0/lists/abc/members" in page_extract(page)


def test_get_and_post_in_prose_are_no_hit():
    page = "# About\n\n" + FILLER + "\n\nGET in touch and POST your questions to us.\n\n" + FILLER
    assert page_extract(page) == page[:3000]


def test_who_may_use_it_still_comes_before_a_section_and_its_calls():
    page = ("# Hotel API\n\n" + "\n\n".join(f"POST /hotels/{i}/rooms\n\nBooks room {i}." for i in range(200))
            + "\n\n## Book\n\n" + "\n\n".join(f"GET /book/{i}" for i in range(200))
            + "\n\nAvailable to approved partners only.")
    got = page_extract(page, section="book")
    assert "approved partners only" in got and "## Book" in got and len(got) <= 3000


# What the extract gave before F1128, for pages with no method, path, code block or fragment: unchanged.

def _mixed_page() -> str:
    parts = ["# Acme Developer Docs"]
    for i in range(40):
        parts += [f"## Section {i}", f"Our story, part {i}. We love our customers and our team very much."]
        if i % 3 == 0:
            parts.append(f"Authenticate with an API key {i} sent in the Authorization header; tokens expire.")
        if i % 7 == 0:
            parts.append(f"Production access {i} is available to approved partners only. Apply here.")
        if i % 5 == 0:
            parts.append(f"Acme webhooks for event {i} are retried for a day.")
    return "\n\n".join(parts)


def test_a_page_with_no_calls_and_no_fragment_extracts_as_before():
    import hashlib

    def pin(text: str) -> tuple[int, str]:
        return len(text), hashlib.sha256(text.encode()).hexdigest()

    assert pin(page_extract(_mixed_page(), ["acme"])) == (
        2093, "f0b4b9585b6691060922f4d1a6e88debea051e51b6c6359b6eb6811c0fddad30")
    assert pin(page_extract("# About\n\n" + FILLER)) == (
        3000, "4eeab0e3b3ce2bc6463249dc2918a6e78509ecc5889d498b87bc11d65c16977d")


async def test_read_web_page_reads_the_section_its_address_names(monkeypatch):
    """bld-02's Researcher read core.telegram.org/bots/api#sendmessage and was given the page's head."""
    import core.builder_research as br

    br._LOOSE.clear()

    async def scrape(server_module, args):
        return {"markdown": TELEGRAM}

    monkeypatch.setattr(br, "_scrape", scrape)
    try:
        got = await br.read_web_page({"url": "https://core.telegram.org/bots/api#sendmessage"}, None, [])
    finally:
        br._LOOSE.clear()
    assert "Use this method to send text messages." in got["page_text"] and got["cut"] is True

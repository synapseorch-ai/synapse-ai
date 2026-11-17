"""The live-web check's harness: real public pages through each fetcher the product ships, outcome by outcome.

Unit tests never touch the web, by design, so they cannot show what real sites look like. A detector judged only on a
challenge page and a clean page passed while it called ordinary pages "blocked", because ordinary pages carry the
same vendor script (F1633). This harness fetches a list of real pages, including ordinary pages that share a block
page's surface features, through:

- ``scrape``: the web scraper's ``scrape_url`` (a headless browser with a browser user agent; reads no robots.txt);
- ``knowledge``: a knowledge base's web fetch (SynapseBot; honours robots.txt), the same decisions as its crawl's page
  step (``core.knowledge.web._page``) without writing anything;
- ``browser``: the built-in browser's ``browser_navigate`` in a read-only session.

Each URL × fetcher gets an outcome class (below) and is compared with the site list's expectation, and with the
previous run's outcome when the expectation is ``record``. Politeness is the harness's job, whatever a fetcher does:
robots.txt is read once per host (as SynapseBot) and a page it disallows is fetched by no fetcher at all, every page
load waits at least ``MIN_GAP_S`` after the last one, and each URL is loaded at most once per fetcher per run.

The site list is JSON (``sites.sample.json`` beside this file is the shape)::

    {"sites": [{"id": "python-json-docs", "url": "https://docs.python.org/3/library/json.html",
                "expect": {"scrape": {"outcome": "content", "contains": "JSON encoder"},
                           "knowledge": "record", "browser": "skip"}}]}

An expectation is ``{"outcome": ..., "contains": ..., "reason_has": ...}`` (``contains`` is checked case-blind in the
content, ``reason_has`` in the reason), ``"record"`` (no fixed answer: any change from the previous run is reported), or
``"skip"`` / absent (that fetcher is not run for the URL).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FETCHERS = ("knowledge", "scrape", "browser")

#: Every outcome a row can have. ``not_run_robots`` is the harness's own: robots.txt says no, could not be read, or the
#: host was refused, so nothing was fetched (``robots_skip`` says which).
OUTCOMES = frozenset({
    "content", "blocked", "robots", "robots_unreadable", "not_found", "http_error", "empty", "unsupported",
    "too_large", "egress", "excluded", "error", "not_run_robots",
})

#: Seconds between the start of one page load and the next, across every fetcher and host (at most one a second).
MIN_GAP_S = 1.0

#: The user agent each fetcher sends, recorded beside each row so the results say who asked.
SCRAPER_UA_NOTE = "the scraper's browser user agent (tools.web_scraper.USER_AGENT)"
BROWSER_UA_NOTE = "headless Chromium's own user agent"

#: The scraper's reasons, by the words it starts them with (tools.web_scraper). Only its error field is read: a page
#: whose content mentions a block is content.
_SCRAPE_REASONS = (
    ("Blocked by anti-bot protection", "blocked"),
    ("URL blocked", "egress"),
    ("Page returned no extractable content", "empty"),
)

SAMPLE = Path(__file__).with_name("sites.sample.json")


@dataclass
class Row:
    id: str
    url: str
    fetcher: str
    user_agent: str
    outcome: str
    reason: str = ""
    status: int | None = None
    chars: int = 0
    final_url: str | None = None
    contains_ok: bool | None = None
    expected: Any = None
    verdict: str = ""          # pass | fail | recorded | changed | new
    previous: str | None = None
    seconds: float = 0.0
    text: str = field(default="", repr=False)  # kept for the checks, never written to the results file


# ── the site list ─────────────────────────────────────────────────────────────

def load_sites(path: str | os.PathLike | None = None) -> list[dict]:
    """The site list at ``path`` (the private list in CI), else the public sample."""
    data = json.loads(Path(path or SAMPLE).read_text(encoding="utf-8"))
    sites = data["sites"]
    seen: set[str] = set()
    for site in sites:
        if site["id"] in seen:
            raise ValueError(f"site id {site['id']!r} appears twice")
        seen.add(site["id"])
        for fetcher, want in (site.get("expect") or {}).items():
            if fetcher not in FETCHERS:
                raise ValueError(f"{site['id']}: unknown fetcher {fetcher!r}")
            if isinstance(want, dict):
                accepted = want.get("outcome")
                accepted = accepted if isinstance(accepted, list) and accepted else [accepted]
                for one in accepted:
                    if one not in OUTCOMES:
                        raise ValueError(f"{site['id']}/{fetcher}: unknown outcome {one!r}")
            if not isinstance(want, dict) and want not in ("record", "skip"):
                raise ValueError(f"{site['id']}/{fetcher}: an expectation is an object, 'record' or 'skip'")
    return sites


def wanted(site: dict, fetcher: str) -> Any:
    """The expectation for one fetcher, or None when that fetcher is not run for the site."""
    want = (site.get("expect") or {}).get(fetcher, "skip")
    return None if want == "skip" else want


# ── outcome classes (pure: the unit tests hold these) ─────────────────────────

def scrape_outcome(texts: list[str]) -> tuple[str, str, str]:
    """(outcome, reason, content) from what ``web_scraper.call_tool('scrape_url', …)`` returned."""
    raw = "\n".join(texts)
    try:
        answer = json.loads(raw)
    except ValueError:
        return "error", raw[:300], ""
    if answer.get("success"):
        return "content", "", str(answer.get("markdown") or "")
    reason = str(answer.get("error") or "")
    # The scraper passes the crawler's own failure on as "Crawl failed: <the crawler's words>", a block among them.
    # Only a failed answer's error field is read, never the page's text.
    words = reason.removeprefix("Crawl failed: ")
    for start, outcome in _SCRAPE_REASONS:
        if words.startswith(start):
            return outcome, reason, ""
    return "error", reason, ""


def knowledge_outcome_for_status(status: int) -> str | None:
    """The outcome a non-2xx answer gets, as the crawl's page step treats it; None for a 2xx."""
    if status in (404, 410):
        return "not_found"
    if not 200 <= status < 300:
        return "http_error"
    return None


def browser_outcome(*, status: int | None, handover: str | None, is_error: bool, refused: str | None,
                    text: str) -> tuple[str, str]:
    """(outcome, reason) for one ``browser_navigate``: a bot check the person is handed is a block; the main
    document's status says a missing page; anything else that loaded is content, as the agent reads it."""
    if refused:
        return "egress" if "egress" in refused.lower() or "blocked" in refused.lower() else "error", refused
    if handover == "captcha":
        return "blocked", "A CAPTCHA or bot check is showing"
    if status in (404, 410):
        return "not_found", f"HTTP {status}"
    if status is not None and not 200 <= status < 400:
        return "http_error", f"HTTP {status}"
    if is_error:
        return "error", text[:300]
    if not text.strip():
        return "empty", ""
    return "content", ""


def judge(row: Row, want: Any, previous: dict[tuple[str, str], str]) -> Row:
    """Set the row's verdict against its expectation, and against the previous run for a ``record`` one."""
    before = previous.get((row.id, row.fetcher))
    row.previous = before
    row.expected = want
    if want == "record":
        row.verdict = "new" if before is None else ("recorded" if before == row.outcome else "changed")
        return row
    # An outcome is one name, or a list of names any of which passes: for a page whose answer depends on where the
    # fetch comes from (a CDN that challenges some addresses and not others). Then `contains` is checked only when
    # the page came back as content, and `reason_has` only when it did not.
    accepted = want.get("outcome")
    either = isinstance(accepted, list)
    ok = row.outcome in accepted if either else row.outcome == accepted
    contains = want.get("contains")
    if ok and contains is not None and (not either or row.outcome == "content"):
        row.contains_ok = contains.lower() in row.text.lower()
        ok = row.contains_ok
    reason_has = want.get("reason_has")
    if ok and reason_has is not None and (not either or row.outcome != "content"):
        ok = reason_has.lower() in row.reason.lower()
    row.verdict = "pass" if ok else "fail"
    return row


def previous_outcomes(path: str | os.PathLike | None) -> dict[tuple[str, str], str]:
    """(site id, fetcher) → outcome from an earlier results file; empty when there is none."""
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(r["id"], r["fetcher"]): r["outcome"] for r in data.get("rows", [])}


def summary_markdown(rows: list[Row], run: dict) -> str:
    """A short table for the CI run's summary: every row that is not a plain pass first."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    lines = [f"## Live web check ({run['vantage']}, {run['started']})", "",
             ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
             "| site | fetcher | outcome | verdict | was | reason |", "|---|---|---|---|---|---|"]
    order = {"fail": 0, "changed": 1, "new": 2, "recorded": 3, "pass": 4}
    for r in sorted(rows, key=lambda r: (order.get(r.verdict, 5), r.id, r.fetcher)):
        reason = r.reason.replace("|", "/").replace("\n", " ")[:120]
        lines.append(f"| {r.id} | {r.fetcher} | {r.outcome} | {r.verdict} | {r.previous or ''} | {reason} |")
    return "\n".join(lines) + "\n"


def write_results(rows: list[Row], run: dict, path: str | os.PathLike) -> None:
    out = {"run": run, "rows": [{k: v for k, v in asdict(r).items() if k != "text"} for r in rows]}
    Path(path).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ── the fetchers (live) ───────────────────────────────────────────────────────

class _Gap:
    """At most one page load starts per ``MIN_GAP_S``, whichever fetcher and host."""

    def __init__(self, gap: float = MIN_GAP_S):
        self.gap, self.last = gap, 0.0

    async def wait(self) -> None:
        delay = self.last + self.gap - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last = time.monotonic()


def robots_skip(robots, url: str) -> str | None:
    """Why the scraper and the browser fetch nothing for ``url``, or None when they may. ``robots`` is the knowledge
    fetch's own reading of the host's robots.txt, or None when the host was refused outright. An unread robots.txt
    counts as a no (RFC 9309: an unreachable one means "disallow everything"), but is said apart from a real no: a
    site that never answers the bot's robots.txt read is not a site that forbids the page."""
    if robots is None:
        return "the host was refused outright, so the harness fetched nothing"
    if robots.unreachable_:
        return "robots.txt could not be read, which counts as a no, so the harness fetched nothing"
    if not robots.allows(url):
        return "robots.txt does not allow this page, so the harness fetched nothing"
    return None


async def _knowledge(fetcher, site: dict) -> Row:
    """The crawl's page step for one named URL (``core.knowledge.web._page``), minus the writing."""
    from core import safe_fetch
    from core.knowledge.fetch import (
        HTML_MAX_BYTES, HTML_TYPES, USER_AGENT, RobotsDisallowed, RobotsUnreachable, media_type,
    )
    from core.knowledge.web import _render, document_ext, normalize_url

    row = Row(site["id"], site["url"], "knowledge", USER_AGENT, "error")
    url = normalize_url(site["url"])
    try:
        got = await fetcher.get(url, max_bytes=25 * 1024 * 1024)
    except RobotsDisallowed as exc:
        row.outcome, row.reason = "robots", str(exc)
        return row
    except RobotsUnreachable as exc:
        row.outcome, row.reason = "robots_unreadable", str(exc)
        return row
    except safe_fetch.FetchBlocked as exc:
        row.outcome, row.reason = "egress", str(exc)
        return row
    except safe_fetch.FetchTooLarge as exc:
        row.outcome, row.reason = "too_large", str(exc)
        return row
    except Exception as exc:  # noqa: BLE001 — a refused or failed fetch is this row's outcome
        row.outcome, row.reason = "error", f"{type(exc).__name__}: {exc}"[:300]
        return row
    row.status, row.final_url = got.status, str(got.url)
    bad = knowledge_outcome_for_status(got.status)
    if bad:
        row.outcome, row.reason = bad, f"The server answered {got.status}."
        return row
    media = media_type(got.headers)
    sniff = got.body[:512].lstrip().lower()
    if media in HTML_TYPES or (not media and sniff.startswith((b"<!doctype html", b"<html"))):
        if len(got.body) > HTML_MAX_BYTES:
            row.outcome = "too_large"
            return row
        links, extract = await asyncio.to_thread(_render, got.body, got.headers.get("content-type"),
                                                 normalize_url(str(got.url)), got.headers.get("x-robots-tag"))
        if links.noindex:
            row.outcome, row.reason = "excluded", "noindex"
            return row
        if not extract.markdown.strip():
            row.outcome, row.reason = "empty", "The page has no readable text"
            return row
        row.outcome, row.text = "content", extract.markdown
        row.chars = len(row.text)
        return row
    if document_ext(str(got.url), media) is None:
        row.outcome, row.reason = "unsupported", media or "unknown type"
        return row
    row.outcome, row.chars = "content", len(got.body)
    if media.startswith("text/") or media in ("application/json", "application/xml"):
        row.text = got.body.decode("utf-8", errors="replace")
    return row


async def _scrape(site: dict) -> Row:
    from tools import web_scraper

    started = time.monotonic()
    result = await web_scraper.call_tool("scrape_url", {"url": site["url"]})
    outcome, reason, content = scrape_outcome([getattr(c, "text", "") for c in result])
    row = Row(site["id"], site["url"], "scrape", SCRAPER_UA_NOTE, outcome, reason=reason, text=content,
              chars=len(content))
    row.seconds = round(time.monotonic() - started, 2)
    return row


async def _browser(session, site: dict) -> Row:
    session._doc = None  # the last page's status must not stand in for a load that never answered
    out = await session.call("browser_navigate", {"url": site["url"]})
    doc = getattr(session, "_doc", None)
    status = doc[1] if doc else None
    handover = (out.get("handover") or {}).get("kind")
    text = str(out.get("text") or "")
    outcome, reason = browser_outcome(status=status, handover=handover, is_error=bool(out.get("is_error")),
                                      refused=out.get("refused"), text=text)
    row = Row(site["id"], site["url"], "browser", BROWSER_UA_NOTE, outcome, reason=reason, status=status,
              text=text if outcome == "content" else "", chars=len(text))
    try:
        row.final_url = session._page_url()
    except Exception:  # noqa: BLE001 — the URL is a note, not the outcome
        pass
    return row


async def run(sites: list[dict], *, previous: dict[tuple[str, str], str], workdir: Path,
              gap: float = MIN_GAP_S) -> list[Row]:
    """Every site through every fetcher it names, politely, in list order."""
    from core.browser import gate
    from core.knowledge.fetch import Fetcher

    pace = _Gap(gap)
    rows: list[Row] = []
    browser_needed = any(wanted(s, "browser") is not None for s in sites)
    pw_cm = session = None
    async with Fetcher(rps=1.0) as fetcher:
        try:
            if browser_needed:
                from playwright.async_api import async_playwright

                from core.browser.host import Session

                pw_cm = async_playwright()
                pw = await pw_cm.__aenter__()
                session = Session("live-check", "live-check", gate.READ_ONLY, {"actions": 10_000,
                                  "loads_per_min": 60}, workdir / "browser")
                await session.start(pw)
            for site in sites:
                try:
                    skip = robots_skip(await fetcher.robots(site["url"]), site["url"])
                except Exception:  # noqa: BLE001 — a host refused outright: the knowledge row says why
                    skip = robots_skip(None, site["url"])
                for name in FETCHERS:
                    want = wanted(site, name)
                    if want is None:
                        continue
                    started = time.monotonic()
                    if name != "knowledge" and skip:
                        row = Row(site["id"], site["url"], name, "", "not_run_robots", reason=skip)
                    else:
                        await pace.wait()
                        if name == "knowledge":
                            row = await _knowledge(fetcher, site)
                        elif name == "scrape":
                            row = await _scrape(site)
                        else:
                            row = await _browser(session, site)
                    row.seconds = row.seconds or round(time.monotonic() - started, 2)
                    rows.append(judge(row, want, previous))
        finally:
            if session is not None:
                await session.close()
            if pw_cm is not None:
                await pw_cm.__aexit__(None, None, None)
            if any(wanted(s, "scrape") is not None for s in sites):
                from tools import web_scraper

                await web_scraper.close_crawler()
    return rows


def run_info(vantage: str | None = None) -> dict:
    return {"started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "vantage": vantage or os.environ.get("SYNAPSE_LIVE_VANTAGE", "local"),
            "ref": os.environ.get("SYNAPSE_LIVE_REF") or os.environ.get("GITHUB_SHA", ""), "min_gap_s": MIN_GAP_S}

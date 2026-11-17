"""Real public pages answer each fetcher as expected: content where a page is ordinary (even one carrying a bot
vendor's routine script), the specific reason where it is not (live; skipped by default).

This is the one place the test suite touches the web, so it never runs by accident: it is marked ``live`` (which
pyproject's addopts leaves out) and also skips unless ``SYNAPSE_LIVE=1``. Run it with::

    SYNAPSE_LIVE=1 python -m pytest -m live backend/tests/live/test_live_web.py

Environment:
- ``SYNAPSE_LIVE_SITES``: a site list (JSON, the shape of ``sites.sample.json``); the public sample when unset.
- ``SYNAPSE_LIVE_PREVIOUS``: an earlier run's results file, so a ``record`` row that changed fails.
- ``SYNAPSE_LIVE_RESULTS``: where this run's results file goes.
- ``SYNAPSE_LIVE_SUMMARY``: a markdown file the run's table is appended to (CI's step summary).
- ``SYNAPSE_LIVE_VANTAGE``: where the run comes from (``ci``, ``local``): bot vendors treat a datacenter differently.
"""

import asyncio
import os

import live_web
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("SYNAPSE_LIVE") != "1",
                       reason="touches the live web: set SYNAPSE_LIVE=1 and run with -m live"),
]

SITES = live_web.load_sites(os.environ.get("SYNAPSE_LIVE_SITES"))
CASES = [(s["id"], f) for s in SITES for f in live_web.FETCHERS if live_web.wanted(s, f) is not None]


@pytest.fixture(scope="module")
def live_rows(tmp_path_factory):
    """One polite pass over every site, shared by the cases below. Its own data folders, never a real store."""
    root = tmp_path_factory.mktemp("live")
    previous = live_web.previous_outcomes(os.environ.get("SYNAPSE_LIVE_PREVIOUS"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SYNAPSE_HOME", str(root / "home"))
        mp.setenv("SYNAPSE_DB_URL", f"sqlite+aiosqlite:///{root / 'store.db'}")
        for name in ("BLOB", "SCRATCH", "STATE"):
            mp.setenv(f"SYNAPSE_{name}_DIR", str(root / name.lower()))
        rows = asyncio.run(live_web.run(SITES, previous=previous, workdir=root))
    run = live_web.run_info()
    if out := os.environ.get("SYNAPSE_LIVE_RESULTS"):
        live_web.write_results(rows, run, out)
    if summary := os.environ.get("SYNAPSE_LIVE_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(live_web.summary_markdown(rows, run))
    return {(r.id, r.fetcher): r for r in rows}


@pytest.mark.parametrize(("site_id", "fetcher"), CASES, ids=[f"{s}-{f}" for s, f in CASES])
def test_a_page_answers_its_fetcher_as_expected(live_rows, site_id, fetcher):
    row = live_rows[(site_id, fetcher)]
    assert row.verdict in ("pass", "recorded", "new"), (
        f"{site_id} via {fetcher}: {row.outcome} ({row.reason or 'no reason'}); expected {row.expected}, "
        f"previous run {row.previous or 'none'}")

"""An answer says only the signed-in sessions the browser keeps, and why a page could not be read (F1688).

Every answer reads that a Browser agent reads pages "signed in with a session the person kept". Asked to read a page
behind a sign-in, with no session kept for its site, Synapse AI promised a saved session as if one were ready, and gave
a made-up reason for not reading the page. Now every answer's facts name the sites the browser keeps a usable session
for, read as the plan check reads them (F1300), and the pages a one-off read names are each marked with whether there
is one: a site with a session can be read signed in by a Browser agent; any other needs signing in to once, on the
Browser screen's Sessions, before it can be; and where a Browser agent can't be used, that is said with its reason.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from core.native_builder import answer_pack as ap
from core.product_tools.roles import Role

GITHUB = "https://github.com/settings/profile"
WIKI = "https://en.wikipedia.org/wiki/Special:Random"
SCREEN = "Agents › Browser › Sessions"

#: A site's name as it is written in text: labels of letters, digits and hyphens, joined by dots, ending in a word.
_SITE = re.compile(r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?![\w-])", re.IGNORECASE)


def sites(text: str) -> set[str]:
    """Every site's name the text says, whole: a site is found by its name, never as a part of another's (F1706)."""
    return {m.group(0).lower().rstrip(".") for m in _SITE.finditer(text)}


def test_a_site_is_found_by_its_whole_name_and_a_near_miss_is_not():
    assert {"github.com", "notion.so"} <= sites("Signed in: github.com, notion.so. See https://github.com/x.")
    assert sites("notgithub.com and github.com.example.org") == {"notgithub.com", "github.com.example.org"}


def test_this_file_finds_a_site_by_its_name_never_by_a_part_of_the_text():
    """A check that a site's name is somewhere in a text is a part-of-text check, which also passes on a near miss
    (`notgithub.com`): each is a comparison of the sites the text names (F1706)."""
    looked_up = [node.lineno for node in ast.walk(ast.parse(Path(__file__).read_text()))
                 if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                 and isinstance(node.left.value, str) and _SITE.fullmatch(node.left.value)
                 and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)]
    assert looked_up == [], f"a site looked for inside text on lines {looked_up}"


def _pack(**kw) -> str:
    base = dict(routed={"needs": "pages"}, workspace_name="Harbor & Pine", profile="", asker_name="",
                asker_role=Role.OWNER, last="", capabilities="", help_hits=[], named=[], pages=[GITHUB, WIKI])
    return ap.build_pack(**{**base, **kw})


def _line(pack: str, url: str) -> str:
    return next(x for x in pack.splitlines() if x.startswith(f"- {url}"))


def test_a_page_on_a_site_with_a_kept_session_is_marked_and_no_other_is():
    pack = _pack(kept_sites=["github.com"])
    assert "github.com" in _line(pack, GITHUB).split(GITHUB, 1)[1], _line(pack, GITHUB)
    assert _line(pack, WIKI).strip() == f"- {WIKI}", _line(pack, WIKI)


def test_a_page_read_is_read_as_no_one_and_a_sign_in_is_said_plainly():
    pack = _pack(kept_sites=[])
    assert "as no one" in pack and "sign-in" in pack, pack
    assert "github.com" not in pack.replace(GITHUB, ""), "no session is said for a site that has none"


def test_the_sessions_kept_are_named_and_any_other_site_needs_a_sign_in_first():
    said = ap.sessions_said(["github.com", "notion.so"])
    assert "github.com" in said and "notion.so" in said, said
    assert SCREEN in said and "never" in said.lower(), said


def test_with_no_session_kept_none_is_said_as_ready():
    said = ap.sessions_said([])
    assert "no signed-in session" in said and SCREEN in said, said


def test_where_a_browser_agent_cant_be_used_its_reason_is_said_and_no_screen_offered():
    reason = "Browser agents come with Solo and up."
    said = ap.sessions_said(["github.com"], refused=reason)
    assert reason in said and SCREEN not in said and "github.com" not in said, said


def test_every_answer_reads_the_sessions_line():
    pack = _pack(routed={"needs": "none"}, pages=None, sessions=ap.sessions_said([]))
    assert ap.sessions_said([]) in pack, pack
    source = inspect.getsource(ap.answer_pack)
    assert "sessions=sessions_said(kept, refused)" in source and "kept_sites=kept" in source, \
        "the pack builder passes the kept sessions on every route"


@pytest.mark.asyncio
async def test_the_kept_sessions_are_the_usable_ones_as_the_plan_check_reads_them(monkeypatch):
    from core import site_sessions

    async def rows(caller=None):
        return [{"site": "github.com", "usable": True}, {"site": "example.com", "usable": False},
                {"site": "notion.so", "usable": True}]

    monkeypatch.setattr(site_sessions, "_lister", rows)
    kept, refused = await ap.kept_sessions()
    assert (kept, refused) == (["github.com", "notion.so"], ""), (kept, refused)

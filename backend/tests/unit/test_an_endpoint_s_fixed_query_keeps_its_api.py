"""An endpoint written with its fixed query keeps its API (F1485; TRI2, pro drafts/tri2/TRIAGE.md, "Second run").

TRI2's prod run (run_01a1bfda…, 11-09): the research read GitHub's docs and gave the endpoint as
`GET /repos/{owner}/{repo}/pulls?state=open&per_page=100`. `check_api_spec` takes no `?` in a path, so the whole spec
was dropped ("is not a method and a path under its base") and the tool was left a lead the build could make nothing
from. A query is the call's own: the path is checked as a path, and each pair of the query is kept, a name with a
literal value or one {param}, and put back on the URL of the tool the build makes. A query that carries a key is the
key-in-the-URL verdict it always was: a URL is no place a key is kept safely.
"""
from __future__ import annotations

import pytest

from core.builder_research import _NO_WAY, check_api_spec
from core.native_builder import objects as O
from core.native_builder.build_save import _tool_call, made_schema

DOCS = "https://docs.github.com/en/rest/pulls/pulls"
OWN = ["github.com"]


def _raw(path: str, **more) -> dict:
    return {"base_url": "https://api.github.com", "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
            "docs_url": DOCS, "endpoints": [{"method": "GET", "path": path, "does": "Lists open pull requests",
                                            "tool": "list_open_prs", **more}]}


def _check(path: str):
    return check_api_spec(_raw(path), "GitHub", OWN, [DOCS])


def test_the_prod_run_s_endpoint_keeps_its_api_and_its_query():
    spec, why = _check("/repos/{owner}/{repo}/pulls?state=open&per_page=100")
    assert why == "" and spec is not None
    [endpoint] = spec["endpoints"]
    assert endpoint["path"] == "/repos/{owner}/{repo}/pulls"
    assert endpoint["query"] == "state=open&per_page=100"
    assert endpoint["tool"] == "github_list_open_prs"


def test_a_path_without_a_query_is_as_it_was():
    spec, why = _check("/repos/{owner}/{repo}/pulls")
    assert why == "" and "query" not in spec["endpoints"][0]


def test_the_made_tool_calls_the_path_with_its_query():
    spec, _ = _check("/repos/{owner}/{repo}/pulls?state=open&per_page=100")
    status = O.ToolStatus(name="github_list_open_prs", status="found", provider="GitHub", via="http_tool",
                          api={**{k: v for k, v in spec.items() if k != "endpoints"}, "endpoint": spec["endpoints"][0]})
    args = _tool_call(status)["args"]
    assert args["url"] == "https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=100"
    assert args["base_lock"] == "https://api.github.com" and args["read_only"] is True
    assert made_schema(status)["required"] == ["owner", "repo"]


def test_a_query_value_may_be_one_placeholder_which_the_tool_takes():
    spec, why = _check("/repos/{owner}/{repo}/pulls?state={state}")
    assert why == "" and spec["endpoints"][0]["query"] == "state={state}"
    status = O.ToolStatus(name="github_list_open_prs", status="found", provider="GitHub", via="http_tool",
                          api={**{k: v for k, v in spec.items() if k != "endpoints"}, "endpoint": spec["endpoints"][0]})
    assert made_schema(status)["required"] == ["owner", "repo", "state"]


@pytest.mark.parametrize("path", [
    "/user/repos?access_token={token}",
    "/user/repos?api_key=abc",
    "/user/repos?per_page=5&key={key}",
])
def test_a_query_that_carries_a_key_is_the_key_in_the_url_verdict(path):
    spec, why = _check(path)
    assert spec is None and why == "query" and why in _NO_WAY


@pytest.mark.parametrize("path", [
    "/repos/{owner}/{repo}/pulls?state=open#top",          # a fragment
    "/repos/{owner}/{repo}/pulls?next=https://evil.example",  # a URL in a value
    "/repos/{owner}/{repo}/pulls?state={state}{x}",        # two placeholders in one value
    "/repos/{owner}/{repo}/pulls?=open",                   # a pair with no name
])
def test_a_query_that_is_not_plain_pairs_is_refused(path):
    spec, why = _check(path)
    assert spec is None and why

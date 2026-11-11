"""The metrics answer only a token (F1534, BRW5; Naveen's pick 11-11).

`/api/v2/metrics` is under /api/v<N>, which the internal-token middleware skips (the versioned API checks its own
keys), and the route checked METRICS_TOKEN only when one was set: on an install without one, which is every install,
anyone who could reach the port read the process's metrics, through the frontend too. Now it answers
`Authorization: Bearer <METRICS_TOKEN>` or the frontend's internal token, and says what it needs to anyone else:
401 with no token, 403 with a wrong one.
"""
from __future__ import annotations

URL = "/api/v2/metrics"


async def test_without_a_token_it_says_what_it_needs(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    resp = await client.get(URL)
    assert resp.status_code == 401
    assert "METRICS_TOKEN" in resp.json()["detail"]


async def test_metrics_token_opens_them_and_a_wrong_one_is_refused_in_words(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-f1534")
    assert (await client.get(URL)).status_code == 401
    wrong = await client.get(URL, headers={"Authorization": "Bearer not-it"})
    assert wrong.status_code == 403 and "METRICS_TOKEN" in wrong.json()["detail"]
    right = await client.get(URL, headers={"Authorization": "Bearer metrics-token-f1534"})
    assert right.status_code == 200 and right.headers["content-type"].startswith("text/plain")


async def test_the_frontends_internal_token_opens_them_and_only_the_right_one(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", "internal-token-f1534")
    assert (await client.get(URL, headers={"X-Synapse-Internal": "internal-token-f1534"})).status_code == 200
    wrong = await client.get(URL, headers={"X-Synapse-Internal": "not-it"})
    assert wrong.status_code == 403 and "METRICS_TOKEN" in wrong.json()["detail"]

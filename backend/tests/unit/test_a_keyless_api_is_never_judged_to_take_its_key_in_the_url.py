"""An API that takes no key is never judged to take its key in the URL, and a key in an endpoint's query is still found
whatever its name's case or separators (F1647).

`report_findings`' api schema makes `auth` required, with `in` one of header or query, so a spec of an API that takes
no key comes as blanks. It came as `{"in": "header", "name": "", "scheme": ""}` (F1411, kept as keyless), and also as
`{"in": "query", "name": "", "scheme": ""}`: Open-Meteo's spec, whose research said "No account or API key required".
That was the key-in-the-URL verdict ("this API takes its key in the URL, which I can't keep safely"), so its plan put
the forecast under Missing or off. Behind that, its endpoint's `timezone=Europe/Lisbon` was refused too: a fixed query
value could not hold a `/`.

Now an auth that names no key (no name and no scheme) takes no key, whatever its `in` says, and a fixed query value may
hold a single `/`. A value that could be a URL (`//`) is still refused.

What the first rule lets through is held by the second check: a key written as an endpoint's query pair is still the
key-in-the-URL verdict. The pair's name is read word by word, in any case, split at `-`, `_`, `.`, brackets and a
camelCase hump, so `apiKey`, `X-Api-Key`, `client_secret` and OpenWeatherMap's `appid` are keys, and `monkey`,
`keyword` and `tokens_used` are not.

Near misses: a key named in the auth, in the query or a header, is as it was (refused, or a card); Basic is still a
sign-in; `app_id` is an id, not a key; wttr.in, keyless with a plain query, is kept.
"""
from __future__ import annotations

import pytest

from core import builder_research as br

DOCS = "https://open-meteo.com/en/docs"
OWN = ["open-meteo.com", "api.open-meteo.com"]
#: Open-Meteo's spec as the plan researcher wrote it (a public API; the coordinates are Porto's).
OPEN_METEO = {
    "base_url": "https://api.open-meteo.com/v1",
    "auth": {"in": "query", "name": "", "scheme": ""},
    "endpoints": [{"tool": "get_weather_forecast", "method": "GET",
                   "path": "/forecast?latitude=41.15&longitude=-8.61&hourly=precipitation_probability,wind_speed_10m"
                           "&timezone=Europe/Lisbon&forecast_days=2",
                   "does": "Hourly rain probability and wind speed for the next two days"}],
    "docs_url": DOCS,
}


def check(spec: dict, provider: str = "Open-Meteo", own: list[str] = OWN, docs: str = DOCS):
    return br.check_api_spec(spec, provider, own, [docs])


def with_query(query: str, auth=None) -> dict:
    return {**OPEN_METEO, "auth": auth, "endpoints": [{**OPEN_METEO["endpoints"][0], "path": f"/forecast?{query}"}]}


class TestAnApiThatTakesNoKey:
    def test_open_meteo_as_its_research_wrote_it_is_kept_taking_no_key(self):
        spec, why = check(OPEN_METEO)
        assert spec is not None, why
        assert spec["auth"] is None and spec["keyless"] is True
        [endpoint] = spec["endpoints"]
        assert endpoint["path"] == "/forecast" and "timezone=Europe/Lisbon" in endpoint["query"], endpoint

    @pytest.mark.parametrize("given", ["header", "query", None])
    def test_an_auth_that_names_no_key_takes_none_whatever_its_in(self, given):
        spec, why = check({**OPEN_METEO, "auth": {"in": given, "name": "", "scheme": ""}})
        assert spec is not None and spec["keyless"] is True, why

    @pytest.mark.parametrize("value", ["Europe/Lisbon", "America/Argentina/Buenos_Aires", "a/b"])
    def test_a_query_value_may_hold_a_slash(self, value):
        spec, why = check(with_query(f"latitude=41.15&timezone={value}"))
        assert spec is not None, why

    @pytest.mark.parametrize("value", ["https://evil.example", "//evil.example", "a//b"])
    def test_a_value_that_could_be_a_url_is_still_refused(self, value):
        spec, why = check(with_query(f"latitude=41.15&next={value}"))
        assert spec is None and why and why != "query", why

    def test_wttr_in_keyless_with_a_plain_query_is_kept(self):
        spec, why = check({"base_url": "https://wttr.in", "auth": None,
                           "endpoints": [{"tool": "wttr_weather", "method": "GET", "path": "/{city}?format=j1",
                                          "does": "Today's weather for a city"}],
                           "docs_url": "https://wttr.in/:help"}, "wttr.in", ["wttr.in"], "https://wttr.in/:help")
        assert spec is not None and spec["keyless"] is True, why


#: A query pair's name that carries a credential, by family: each is the key-in-the-URL verdict. A new name is a line.
KEYS = [
    ("key", "key"), ("key", "KEY"), ("key", "private_key"), ("key", "sort-key"),
    ("api key", "api_key"), ("api key", "apikey"), ("api key", "api-key"), ("api key", "API_KEY"),
    ("api key", "apiKey"), ("api key", "APIKey"), ("api key", "x-api-key"), ("api key", "X-Api-Key"),
    ("token", "token"), ("token", "access_token"), ("token", "accessToken"), ("token", "auth_token"),
    ("token", "authToken"), ("token", "oauth_token"), ("token", "Token"),
    ("secret", "secret"), ("secret", "client_secret"), ("secret", "clientSecret"), ("secret", "CLIENT-SECRET"),
    ("signature", "sig"), ("signature", "signature"), ("signature", "Signature"),
    ("password", "password"), ("password", "passwd"), ("password", "Password"),
    ("auth", "auth"), ("auth", "authorization"),
    ("app key", "appid"), ("app key", "APPID"), ("app key", "app_key"), ("app key", "appKey"),
]
#: Names that only share letters with a key, or name an id: never the verdict.
NOT_KEYS = ["appearance", "application", "keyword", "monkey", "tokens_used", "app_id", "appId", "latitude", "timezone",
            "keys", "format", "max_tokens", "signal", "author", "passengers"]


class TestAKeyInAnEndpointsQueryIsStillFound:
    @pytest.mark.parametrize("family, name", KEYS, ids=[n for _, n in KEYS])
    def test_a_credentials_name_in_any_case_or_separator_is_the_verdict(self, family, name):
        spec, why = check(with_query(f"latitude=41.15&{name}={{secret}}"))
        assert spec is None and why == "query", (family, name, why)

    @pytest.mark.parametrize("name", NOT_KEYS)
    def test_a_name_that_only_shares_its_letters_is_kept(self, name):
        spec, why = check(with_query(f"latitude=41.15&{name}=1"))
        assert spec is not None, (name, why)


class TestAKeyNamedInTheAuthIsAsItWas:
    @pytest.mark.parametrize("auth", [{"in": "query", "name": "apikey", "scheme": ""},
                                      {"in": "query", "name": "api_key", "scheme": ""},
                                      {"in": "query", "name": "", "scheme": "Bearer"}])
    def test_a_key_in_the_query_named_or_with_a_scheme_is_the_verdict(self, auth):
        spec, why = check({**OPEN_METEO, "auth": auth})
        assert spec is None and why == "query", why

    def test_a_header_key_is_kept_to_ask_for_on_a_card(self):
        spec, why = check({**OPEN_METEO, "auth": {"in": "header", "name": "X-Api-Key", "scheme": ""}})
        assert spec is not None and spec["auth"] == {"in": "header", "name": "X-Api-Key", "scheme": ""}, why
        assert "keyless" not in spec

    def test_basic_is_still_a_sign_in(self):
        spec, why = check({**OPEN_METEO, "auth": {"in": "header", "name": "Authorization", "scheme": "Basic"}})
        assert spec is None and why == "basic"


class TestTheResearchReachesThePlanAsAnApi:
    @pytest.fixture(autouse=True)
    def loose(self):
        br._LOOSE.clear()
        br._LOOSE[""] = {**br._fresh(), "read_hosts": ["open-meteo.com"]}
        yield
        br._LOOSE.clear()

    async def test_open_meteo_is_an_api_to_build_not_missing(self):
        option = {"provider": "Open-Meteo", "how": "api",
                  "detail": "Free, no-key weather forecast API: hourly rain probability and wind speed",
                  "needs": "No account or API key required", "sources": [DOCS], "own_domains": OWN,
                  "api": OPEN_METEO}
        out = await br.report_findings({"goal": "decide whether to put the patio tables out", "options": [option]},
                                       None, [])
        [found] = out["options"]
        assert found["how"] == "api" and "takes its key in the URL" not in found["detail"], found
        assert found["api"]["keyless"] is True and found["api"]["auth"] is None

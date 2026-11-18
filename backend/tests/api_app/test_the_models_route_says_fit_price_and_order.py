"""`GET /api/models` says each model's fit for building, its price and the engine's order (F1598, commit A).

The pickers read the models list. For Synapse AI's picker to show what builds well first and leave out what is not
for building, and for every picker to say a model's size and cost, the list now says three more things beside each
model's window:
- `builder_fit`: "recommended" or "not_for_building", for the models with a known fit only;
- `prices`: `{in, out}` in USD per 1M tokens, for a model with a price (a CLI's subscription and a local model have
  none);
- `ranked`: the models in the engine's one order, the order its pick reads.

Near misses: a model with no known fit or price is left out of those maps, never given a guess; a not-for-building
model is listed with its fit, so a picker can leave it out and still say why; what every reader read before is still
there.
"""
from __future__ import annotations

import pytest

from core.native_builder import models_menu as MM
from core.providers import model_facts as F


@pytest.fixture(autouse=True)
def _no_measure():
    """Each case from no measured rows, then the shipped table back."""
    F.set_measured({})
    yield
    F.set_measured(None)


class TestTheRoutesFacts:
    async def test_each_known_fit_price_and_the_order(self):
        listed = ["claude-sonnet-4-6", "gemma-3-4b-it", "acme-model-9", "cli.claude.claude-haiku-4-5",
                  "gemini-3.1-flash-lite-preview", "claude-opus-4-6"]
        said = await MM.route_facts(listed)
        assert said["builder_fit"] == {"claude-sonnet-4-6": "recommended", "gemma-3-4b-it": "not_for_building",
                                       "claude-opus-4-6": "recommended"}
        from core.model_pricing import DEFAULT_MODEL_PRICING as RATES

        sonnet = RATES["claude-sonnet-4-6"]
        assert said["prices"]["claude-sonnet-4-6"] == {"in": sonnet["input_per_1m"], "out": sonnet["output_per_1m"]}
        assert "cli.claude.claude-haiku-4-5" not in said["prices"] and "acme-model-9" not in said["prices"]
        assert sorted(said["ranked"]) == sorted(listed)
        assert said["ranked"].index("claude-opus-4-6") < said["ranked"].index("claude-sonnet-4-6")

    async def test_nothing_listed_says_nothing(self):
        assert await MM.route_facts([]) == {"builder_fit": {}, "prices": {}, "ranked": []}


class TestTheRoute:
    async def test_it_says_them_for_what_it_lists(self, client):
        listed = (await client.get("/api/models")).json()
        names = list(dict.fromkeys(listed["all_available"]))
        assert listed["ranked"] == (await MM.route_facts(names))["ranked"]
        assert set(listed["builder_fit"]) <= set(names) and set(listed["prices"]) <= set(names)
        assert set(listed["builder_fit"].values()) <= {"recommended", "not_for_building"}
        assert all(set(p) == {"in", "out"} for p in listed["prices"].values())
        for key in ("providers", "all_available", "windows", "synapse_ai_min_window", "cloud", "reasoning_capable"):
            assert key in listed, f"{key}: what every reader reads is still there"

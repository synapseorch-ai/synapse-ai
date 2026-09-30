"""
Every model id goes where it went before the provider table (PF1, D156).

`fixtures/providers/routing_golden.json` was recorded from the prefix chains the
table replaced (`v2.0` `48e6683`), with every provider call stubbed: for each id
in `_fakes.provider_routing.CORPUS`, the provider and mode, the chat call reached
and its arguments (with tools and without, with an agent's cap and without), the
embedding call reached and its arguments, and the provider the usage log was
given — and, for `_fakes.provider_routing.MODE_OVERRIDES`, where a caller that
hands over its own mode is sent. The table must reproduce every recording.

The one intended difference is `MIXED_CASE`: ids whose case the old chat
dispatch did not ignore. Each must route as its lower-case form does.

`PF1_WRITE_GOLDEN=1` rewrites the file from whatever the code does now — only
ever from the chains' code, or to record a change a commit makes on purpose.
"""
import json
import os

import pytest

from _fakes import provider_routing as pr


async def test_every_model_id_routes_as_the_chains_did(monkeypatch):
    recordings = await pr.record_all(monkeypatch)

    if os.environ.get("PF1_WRITE_GOLDEN") == "1":
        pr.write_golden(recordings)
        pytest.skip(f"wrote {pr.GOLDEN}")

    golden = pr.load_golden()
    assert sorted(recordings) == sorted(golden), "the corpus and the golden file list different ids"
    moved = [key for key in recordings if recordings[key] != golden[key] and key not in pr.MIXED_CASE]
    assert not moved, f"{len(moved)} ids route differently: {moved[:20]}"


async def test_an_id_in_mixed_case_routes_as_its_lower_case_form(monkeypatch):
    """The one intended change: the chat dispatch used to match case-sensitively."""
    for model in pr.MIXED_CASE:
        assert model.lower() in pr.CORPUS, model
        mixed = json.dumps(await pr.record(model, monkeypatch), sort_keys=True)
        lower = json.dumps(await pr.record(model.lower(), monkeypatch), sort_keys=True)
        assert mixed.lower() == lower.lower(), model

"""Our model tables are checked against a public model list, a pinned extract that only reports (F1598b).

The window table, the rate card and the size table are written by hand, each row from its provider's page. A public
list (LiteLLM's model_prices_and_context_window.json, MIT) is checked against them at release time, so a row that
drifts is seen:
- `scripts/model_list.json` is an extract of it at one commit. It holds only the ids the rate card knows, each with
  the source's own fields and values as the source writes them, under a header that names the source, the commit and
  the source's MIT licence in full with its copyright line. An id the source does not list is marked as ours.
- `scripts/model_list.py` refreshes the extract from a pinned commit, by hand. It never runs in CI, and the product
  never fetches or reads the list.
- `scripts/model_list_diff.py` says where our tables and the extract disagree, and never writes to our tables.

Near misses: a commit that is not one (a branch name) is refused before anything is fetched; a source entry's other
fields are left out, never reworded; an id only in the source is not taken.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EXTRACT = ROOT / "scripts/model_list.json"
REFRESH = ROOT / "scripts/model_list.py"
DIFF = ROOT / "scripts/model_list_diff.py"
#: What names the list's files or imports them: never a word that only holds "model_list" (`by_model_list`).
NAMES_IT = re.compile(r"(?<![A-Za-z0-9_])(?:model_list(?:_diff)?\.(?:json|py)|(?:import|from) model_list(?:_diff)?\b)")
KEPT = {"key", "max_input_tokens", "max_output_tokens", "input_cost_per_token", "output_cost_per_token",
        "supports_function_calling"}


@pytest.fixture(scope="module")
def extract() -> dict:
    return json.loads(EXTRACT.read_text(encoding="utf-8"))


def _load(path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheExtract:
    def test_its_header_names_the_source_the_commit_and_the_licence_in_full(self, extract):
        assert extract["source"] == "https://github.com/BerriAI/litellm"
        assert extract["file"] == "model_prices_and_context_window.json"
        assert re.fullmatch(r"[0-9a-f]{40}", extract["commit"]), extract["commit"]
        licence = extract["licence"]
        for words in ("MIT License", "Copyright (c) 2023 Berri AI", "Permission is hereby granted, free of charge",
                      "The above copyright notice and this permission notice shall be included in all",
                      'THE SOFTWARE IS PROVIDED "AS IS"'):
            assert words in licence, words

    def test_every_rate_card_row_is_in_it_or_marked_as_ours(self, extract):
        from core.model_pricing import DEFAULT_MODEL_PRICING

        rows = set(DEFAULT_MODEL_PRICING)
        listed, ours = set(extract["models"]), set(extract["ours"])
        assert rows <= listed | ours, sorted(rows - listed - ours)
        assert not listed & ours
        assert listed <= rows and ours <= rows, "only ids our tables know"

    def test_each_entry_is_the_sources_own_fields_as_it_writes_them(self, extract):
        for model, entry in extract["models"].items():
            assert set(entry) <= KEPT and "key" in entry, (model, entry)
            for field in ("max_input_tokens", "max_output_tokens", "input_cost_per_token", "output_cost_per_token"):
                assert field not in entry or isinstance(entry[field], (int, float)), (model, field)


class TestTheRefresh:
    def test_a_branch_name_is_refused_before_anything_is_fetched(self):
        done = subprocess.run([sys.executable, str(REFRESH), "--commit", "main", "--out", "/nonexistent/x.json"],
                              capture_output=True, text=True, timeout=30)
        assert done.returncode != 0 and "40" in (done.stderr + done.stdout), done

    def test_it_takes_the_sources_fields_verbatim_and_finds_a_providers_own_spelling(self):
        refresh = _load(REFRESH)
        source = {"gpt-4.1": {"max_input_tokens": 1047576, "input_cost_per_token": 2e-06, "mode": "chat",
                              "supports_function_calling": True},
                  "groq/openai/gpt-oss-120b": {"max_input_tokens": 131072, "output_cost_per_token": 7.5e-07},
                  "together_ai/moonshotai/Kimi-K3": {"max_input_tokens": 262144},
                  "only-in-the-source": {"max_input_tokens": 1}}
        models, ours = refresh.extract(source, ["gpt-4.1", "groq.openai/gpt-oss-120b",
                                                "together.moonshotai/Kimi-K3", "acme-model-9"])
        assert models["gpt-4.1"] == {"key": "gpt-4.1", "max_input_tokens": 1047576, "input_cost_per_token": 2e-06,
                                     "supports_function_calling": True}
        assert models["groq.openai/gpt-oss-120b"]["key"] == "groq/openai/gpt-oss-120b"
        assert models["together.moonshotai/Kimi-K3"]["key"] == "together_ai/moonshotai/Kimi-K3"
        assert ours == ["acme-model-9"] and "only-in-the-source" not in models

    def test_its_licence_is_the_sources_mit_section_whole(self):
        refresh = _load(REFRESH)
        text = ("Portions of this software are licensed as follows:\n\n* All content under enterprise/ ...\n\n"
                "MIT License\n\nCopyright (c) 2023 Berri AI\n\nPermission is hereby granted, free of charge, ...\n")
        assert refresh.licence_section(text) == ("MIT License\n\nCopyright (c) 2023 Berri AI\n\n"
                                                 "Permission is hereby granted, free of charge, ...\n")


class TestTheDiffOnlyReports:
    def test_it_says_each_difference_and_writes_nothing(self):
        ours = [ROOT / "backend/core/model_pricing.py", ROOT / "backend/core/providers/windows.py",
                ROOT / "backend/core/providers/model_facts.py", EXTRACT]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in ours]
        done = subprocess.run([sys.executable, str(DIFF), "--json"], capture_output=True, text=True, timeout=120,
                              cwd=ROOT)
        assert done.returncode == 0, done.stderr[-2000:]
        rows = json.loads(done.stdout)
        assert isinstance(rows, list) and all({"model", "what", "ours", "list"} <= set(r) for r in rows), rows[:3]
        assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in ours] == before

    def test_a_price_is_compared_per_1m_tokens_exactly(self):
        diff = _load(DIFF)
        assert diff.per_million(3e-06) == diff.per_million(0.000003) and str(diff.per_million(3e-06)) == "3"
        assert str(diff.per_million(1.5e-05)) == "15" and str(diff.per_million(7.5e-08)) == "0.075"


class TestNeverInCiNorAtRunTime:
    def test_no_workflow_runs_the_refresh(self):
        flows = ROOT / ".github/workflows"
        named = [p.name for p in flows.glob("*.y*ml") if "model_list" in p.read_text(encoding="utf-8")]
        assert not named, named

    def test_the_product_never_reads_or_fetches_it(self):
        found = []
        for base in ("backend/core", "synapse", "frontend/src"):
            for p in (ROOT / base).rglob("*"):
                if p.suffix in (".py", ".ts", ".tsx") and NAMES_IT.search(p.read_text(encoding="utf-8", errors="ignore")):
                    found.append(str(p.relative_to(ROOT)))
        assert not found, found

    @pytest.mark.parametrize("text, names", [
        ("json.load(open('scripts/model_list.json'))", True), ("import model_list", True),
        ("from model_list_diff import rows", True), ("model_list.py --commit", True),
        ("by_model_list = sorted(by_model.values())", False), ('"by_model": by_model_list', False)])
    def test_the_check_finds_the_files_not_a_word_that_holds_their_name(self, text, names):
        assert bool(NAMES_IT.search(text)) is names

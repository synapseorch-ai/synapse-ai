"""Every model picker says what each model is, and Synapse AI's offer what builds well first (F1598, commits B and C).

`/api/models` now says each model's fit for building, its price and the engine's order (commit A). The pickers read
it.

**Every picker** shows one line under each model with what is known of it: its context (small under 128K, standard
from 128K, large from 500K), a cost tier on the engine's own price bands ($ to $1, $$ to $5, $$$ to $15, $$$$ above,
where it never picks on its own) and its price per 1M tokens. What is not known is left out, never guessed. Every
option each picker had is kept.

**Synapse AI's pickers** (the panel's, Settings → Models' Builder model, onboarding's):
- the models recommended for building come first, under their own heading, each with the words "Recommended";
- then "Other models", with no label, in the engine's order;
- a model that is not for building is left out;
- a saved model now left out stays visible as the one row saying why;
- the list is never empty: with nothing rated, what fits is listed, and the note says none is rated yet.

Near misses: with no fit said (an older server), Synapse AI's pickers group as they did; a model the engine did not
rank keeps its place after the ranked ones; a hint an option already has is kept.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
MODELS = SRC / "lib/models.ts"

#: Node resolves the app's `@/` imports to `frontend/src`, and a relative import without its extension to the `.ts`
#: beside it, as the bundler does.
HOOKS = r"""
import { existsSync } from 'node:fs';
import { dirname, resolve as join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const SRC = %s;
export async function resolve(specifier, context, next) {
  let base = null;
  if (specifier.startsWith('@/')) base = join(SRC, specifier.slice(2));
  else if (specifier.startsWith('.') && context.parentURL && !/\.[cm]?[jt]sx?$/.test(specifier))
    base = join(dirname(fileURLToPath(context.parentURL)), specifier);
  if (base) for (const ext of ['.ts', '.tsx', '/index.ts']) {
    if (existsSync(base + ext)) return next(pathToFileURL(base + ext).href, context);
  }
  return next(specifier, context);
}
"""


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the pickers' helper is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(script: str):
        program = (f"import * as M from {json.dumps(MODELS.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


def hint(node, model: str, said: dict) -> str:
    return node(f"return M.modelHint({json.dumps(model)}, {json.dumps(said)});")


class TestTheLineUnderEachModel:
    def test_its_context_cost_tier_and_price(self, node):
        said = {"windows": {"m": 1_048_576}, "prices": {"m": {"in": 3.3, "out": 15}}}
        assert hint(node, "m", said) == "Large context (1M) · $$$ · $3.30 in / $15.00 out per 1M tokens"

    @pytest.mark.parametrize("tokens, words", [(32_768, "Small context (32K)"), (127_999, "Small context (128K)"),
                                               (128_000, "Standard context (128K)"), (400_000, "Standard context (400K)"),
                                               (500_000, "Large context (500K)"), (2_000_000, "Large context (2M)")])
    def test_the_context_bands(self, node, tokens, words):
        assert hint(node, "m", {"windows": {"m": tokens}}) == words

    @pytest.mark.parametrize("out, tier", [(0.4, "$"), (1, "$"), (1.01, "$$"), (5, "$$"), (15, "$$$"), (15.01, "$$$$")])
    def test_the_cost_tiers_are_the_engines_price_bands(self, node, out, tier):
        assert hint(node, "m", {"prices": {"m": {"in": 0.1, "out": out}}}).startswith(f"{tier} · ")

    def test_a_small_price_keeps_its_figures(self, node):
        assert hint(node, "m", {"prices": {"m": {"in": 0.075, "out": 0.3}}}) == "$ · $0.075 in / $0.30 out per 1M tokens"

    def test_what_is_not_known_is_left_out(self, node):
        assert hint(node, "m", {"windows": {"m": 200_000}}) == "Standard context (200K)"
        assert hint(node, "m", {}) == ""
        assert hint(node, "other", {"windows": {"m": 200_000}}) == ""

    def test_with_hints_adds_the_line_and_keeps_a_hint_already_there(self, node):
        options = [{"value": "a", "label": "a"}, {"value": "b", "label": "b", "hint": "its own"},
                   {"value": "__default__", "label": "Default"}]
        got = node(f"return M.withHints({json.dumps(options)}, {json.dumps({'windows': {'a': 1_000_000, 'b': 1}})});")
        assert got == [{"value": "a", "label": "a", "hint": "Large context (1M)"},
                       {"value": "b", "label": "b", "hint": "its own"}, {"value": "__default__", "label": "Default"}]

    def test_hints_by_id_for_a_picker_of_plain_ids(self, node):
        got = node("return M.modelHints(['a', 'b'], {windows: {a: 1000000}});")
        assert got == {"a": "Large context (1M)"}


#: What `/api/models` says, with commit A's fields: a fit for some, the engine's order, windows and the floor.
SAID = {"windows": {"rec1": 1_000_000, "rec2": 200_000, "other": 400_000, "nfb": 128_000, "tiny": 32_000},
        "synapse_ai_min_window": 128_000,
        "builder_fit": {"rec1": "recommended", "rec2": "recommended", "nfb": "not_for_building"},
        "ranked": ["rec2", "other", "rec1", "nfb", "tiny"]}
OPTIONS = [{"value": v, "label": v, "group": "Provider"} for v in ("rec1", "nfb", "other", "unranked", "rec2", "tiny")]
TAG = {"text": "Recommended", "tone": "success"}


def synapse_ai(node, current: str | None = None, said: dict = SAID, options: list = OPTIONS) -> list[dict]:
    arg = json.dumps(current) if current else "undefined"
    return node(f"return M.synapseAIOnly({json.dumps(options)}, {json.dumps(said)}, {arg});")


class TestSynapseAIsPickers:
    def test_recommended_first_with_its_words_then_other_models_in_the_engines_order(self, node):
        got = synapse_ai(node)
        assert [(o["value"], o["group"]) for o in got] == [
            ("rec2", "Recommended for building"), ("rec1", "Recommended for building"),
            ("other", "Other models"), ("unranked", "Other models")]
        assert all(o.get("tag") == TAG for o in got[:2]) and not any("tag" in o for o in got[2:])

    def test_not_for_building_and_too_small_are_left_out(self, node):
        values = [o["value"] for o in synapse_ai(node)]
        assert "nfb" not in values and "tiny" not in values

    def test_a_saved_model_not_for_building_stays_as_the_one_row_saying_why(self, node):
        got = synapse_ai(node, "nfb")
        assert got[0]["value"] == "nfb" and got[0]["group"] == "Not for building", got[0]
        assert "Synapse AI" in got[0]["hint"] and "Recommended" in got[0]["hint"], got[0]
        assert [o["value"] for o in got].count("nfb") == 1

    def test_a_saved_model_too_small_is_kept_as_before(self, node):
        got = synapse_ai(node, "tiny")
        assert got[0] == {"value": "tiny", "label": "tiny", "group": "Too small for Synapse AI",
                          "hint": "reads up to 32K tokens; Synapse AI needs 128K"}

    def test_never_empty_with_nothing_rated(self, node):
        said = {**SAID, "builder_fit": {}}
        got = synapse_ai(node, said=said)
        # Nothing is marked, so nothing is left out but the too small: all of it, in the engine's order.
        assert [o["value"] for o in got] == ["rec2", "other", "rec1", "nfb", "unranked"]
        assert [o["group"] for o in got] == ["Other models"] * 5
        assert node(f"return M.synapseAINote({json.dumps(got)});").endswith(
            "None of these is rated for building yet.")

    def test_the_note_says_what_is_left_out(self, node):
        note = node(f"return M.synapseAINote({json.dumps(synapse_ai(node))});")
        assert note == "Some models aren't offered here, such as ones too small for what Synapse AI reads."

    def test_with_no_fit_said_it_groups_as_it_did(self, node):
        said = {k: v for k, v in SAID.items() if k not in ("builder_fit", "ranked")}
        got = synapse_ai(node, said=said)
        assert all(o["group"] == "Provider" and "tag" not in o for o in got)
        assert [o["value"] for o in got] == ["rec1", "nfb", "other", "unranked", "rec2"]


class TestADefaultRowSaysWhatItResolvesTo:
    """A "Default (…)" or "Same as default model (…)" row in Synapse AI's pickers runs Synapse AI on the model it
    names. Where that model is not for building, or too small, the row says so as the model's own row would, never
    offering quietly what the list leaves out."""

    def test_one_not_for_building_says_so(self, node):
        got = node(f"return M.synapseAIDefaultHint('nfb', {json.dumps(SAID)});")
        assert got == "Not offered for building with Synapse AI: pick a Recommended model"

    def test_one_too_small_says_what_it_reads(self, node):
        got = node(f"return M.synapseAIDefaultHint('tiny', {json.dumps(SAID)});")
        assert got == "reads up to 32K tokens; Synapse AI needs 128K"

    @pytest.mark.parametrize("model", ["rec1", "other", "unranked", ""])
    def test_one_that_builds_or_is_not_known_says_nothing(self, node, model):
        assert node(f"return M.synapseAIDefaultHint({json.dumps(model)}, {json.dumps(SAID)}) ?? null;") is None

    @pytest.mark.parametrize("path, pattern", [
        ("components/app/BuilderPanel.tsx", r"synapseAIDefaultHint\(builderModel \|\| workspaceModel"),
        ("components/settings/ModelsTab.tsx", r"synapseAIDefaultHint\(selectedModel"),
        ("components/onboarding/ModelSetup.tsx", r"synapseAIDefaultHint\(everyday"),
    ])
    def test_each_of_synapse_ais_default_rows_reads_it(self, path, pattern):
        assert re.search(pattern, (SRC / path).read_text()), path


def test_a_synapse_ai_turn_on_a_model_not_for_building_runs_on_it():
    """The person chose it, so it is theirs: Synapse AI runs on it, and the row says plainly that it is not offered for
    building. Only a window too small for its prompt is refused, as before (a model the person names is never
    replaced by a pick)."""
    import asyncio

    from core.native_builder import models_menu as MM

    assert asyncio.run(MM.synapse_ai_refusal("gemma-3-4b-it", source="builder_model")) is None
    assert asyncio.run(MM.synapse_ai_refusal("mistral.mistral-large-2402-v1:0", source="builder_model"))


class TestEveryPickerReadsIt:
    @pytest.mark.parametrize("path, count", [
        ("components/settings/ModelsTab.tsx", 3), ("components/app/BuilderPanel.tsx", 1),
        ("components/onboarding/ModelSetup.tsx", 1), ("components/settings/AgentsTab.tsx", 2),
        ("components/settings/EvalsScreen.tsx", 1), ("lib/knowledgeHost.tsx", 1),
        ("components/settings/KnowledgeModels.tsx", 1)])
    def test_each_chat_model_picker_adds_the_line(self, path, count):
        assert (SRC / path).read_text().count("withHints(") >= count, path

    def test_the_step_and_data_lab_pickers_get_hints_by_id(self):
        assert re.search(r"modelHints=\{", (SRC / "components/settings/OrchestrationTab.tsx").read_text())
        assert "modelHints(" in (SRC / "components/settings/OrchestrationTab.tsx").read_text()
        assert re.search(r"modelHints=\{modelHints\(", (SRC / "components/SettingsView.tsx").read_text())
        assert "modelHints?.[m]" in (SRC / "components/settings/DataLabTab.tsx").read_text()

    @pytest.mark.parametrize("path", ["components/settings/ModelsTab.tsx", "components/onboarding/ModelSetup.tsx",
                                      "components/app/BuilderPanel.tsx"])
    def test_synapse_ais_pickers_say_its_note(self, path):
        assert "synapseAINote(" in (SRC / path).read_text(), path

    def test_the_combobox_draws_the_tag_in_words(self):
        box = (SRC / "components/ui/combobox.tsx").read_text()
        assert "tag?: { text: string; tone: BadgeTone }" in box and "{option.tag.text}" in box

    def test_the_agents_screen_hands_the_facts_on(self):
        assert "modelFacts={models}" in (SRC / "components/settings/AgentsScreen.tsx").read_text()

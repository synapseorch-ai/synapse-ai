"""Self-hosted Synapse AI runs only on a model that can hold its planner's prompt, and says so (F1592; F1586's class).

F1586 (TRI3, prod 11-13) refused, in cloud, a Synapse AI turn on a model too small for its planner and kept such models
out of cloud's pickers. Self-hosted had the same door open: its route ran any model it was given (`_selected_model`) and
its three pickers (the panel's, Settings → Models' Builder model, onboarding's Synapse AI's model) offered every model,
so a model that cannot read the planner's prompt failed at its first step.

Self-hosted's floor is Naveen's (11-14, in-tab: "refuse only sure failures"): 32,768 tokens, the least that holds the
planner's first call (about 27,500 tokens, SHN1 11-13; the api_app test measures it) with room for its answer. A model
from 32K up to the engine's long-input floor (128K) runs and is marked "may run out of room on a long build", since local
models of 32K-40K reach the planner today (SHN1's qwen3 8B); cloud keeps 128K. A model whose window is not known is never
refused for it (a CLI, an OpenAI-compatible or local endpoint, Hugging Face, an Ollama that does not say), as the
engine's own pick never rules one out. An Ollama model's window is what Ollama says it reads (`/api/show`, F1569).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core import llm_providers as L
from core.native_builder import models_menu as MM

SMALL = "meta.llama3-8b-instruct-v1:0"  # the table reads Llama 3 8B as 8,000 tokens
AT_THE_FLOOR = "mistral.mistral-7b-instruct-v0:2"  # 32,768: exactly the floor, so it runs
LARGE = "claude-sonnet-4-6"


@pytest.fixture
def ollama(monkeypatch):
    """What a local Ollama says each model reads (`/api/show`'s context_length); a model it does not name, nothing."""
    said: dict[str, int] = {}

    async def context(_client, _base, model):
        return said.get(model), None

    monkeypatch.setattr(L, "_ollama_model_context", context)
    return said


class TestTheFloor:
    def test_it_is_naveens_32k_and_under_the_engines_long_input_floor(self):
        assert MM.SYNAPSE_AI_MIN_WINDOW == 32_768 < MM.LONG_WINDOW

    async def test_a_model_known_smaller_is_refused_in_words(self):
        said = await MM.synapse_ai_refusal(SMALL, source="pick")
        assert said is not None
        assert SMALL in said and "8K" in said and "32K" in said, said
        assert "Synapse AI's model menu" in said, "where to change a pick"

    async def test_a_model_at_the_floor_or_over_runs(self):
        assert await MM.synapse_ai_refusal(AT_THE_FLOOR, source="pick") is None
        assert await MM.synapse_ai_refusal(LARGE, source="pick") is None

    @pytest.mark.parametrize("source, words, never", [
        ("pick", "Synapse AI's model menu", "Settings"),
        ("builder_model", "Settings → Models (Builder model), or clear it", "model menu"),
        ("everyday", "no Builder model is set", "clear it"),
    ])
    async def test_it_says_where_to_change_what_chose_the_model(self, source, words, never):
        said = await MM.synapse_ai_refusal(SMALL, source=source)
        assert words in said and never not in said, said


class TestAModelWhoseWindowIsNotKnownIsNeverRefused:
    """One per family whose ids the engine's table cannot read (eb, 11-14)."""

    @pytest.mark.parametrize("model", [
        "cli.claude",                 # a model CLI, by its own name
        "oaic.my-gateway-model",      # an OpenAI-compatible endpoint the person runs
        "locv1.my-local-model",       # a local OpenAI-compatible server
        "hf.someorg/tiny-model",      # Hugging Face
        "ollama.qwen3:8b",            # an Ollama that says nothing of it
    ])
    async def test_never_refused(self, model, ollama):
        assert await MM.synapse_ai_window(model) is None
        assert await MM.synapse_ai_refusal(model, source="pick") is None


class TestAnOllamaModelReadsWhatOllamaSays:
    async def test_one_ollama_says_is_small_is_refused(self, ollama):
        ollama["llama3"] = 8_192
        assert await MM.synapse_ai_window("ollama.llama3") == 8_192
        said = await MM.synapse_ai_refusal("ollama.llama3", source="builder_model")
        assert said is not None and "8K" in said, said

    async def test_one_that_holds_the_planner_runs(self, ollama):
        ollama["qwen3:8b"] = 40_960  # SHN1's qwen3 8B: a local model that reaches the planner today
        assert await MM.synapse_ai_window("ollama.qwen3:8b") == 40_960
        assert await MM.synapse_ai_refusal("ollama.qwen3:8b", source="pick") is None


# ── the pickers (the frontend's own TypeScript, run in node) ──────────────────────────────────────────────────────

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
MODELS = SRC / "lib" / "models.ts"
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


#: What `/api/models` says (F1592): each known window, the floor and the engine's long-input floor.
WINDOWS = {"windows": {"llama3-8b": 8_000, "ollama.qwen3:8b": 40_960, "claude-sonnet-4-6": 1_000_000},
           "synapse_ai_min_window": 32_768, "synapse_ai_long_window": 128_000}
OPTIONS = [{"value": v, "label": v, "group": "G"} for v in ("claude-sonnet-4-6", "llama3-8b", "ollama.qwen3:8b",
                                                            "cli.claude")]


class TestSynapseAIsPickersOfferWhatCanHoldIt:
    def test_one_under_the_floor_is_left_out_and_one_short_of_long_input_is_marked(self, node):
        got = node(f"return M.synapseAIOnly({json.dumps(OPTIONS)}, {json.dumps(WINDOWS)});")
        assert [o["value"] for o in got] == ["claude-sonnet-4-6", "ollama.qwen3:8b", "cli.claude"]
        marked = {o["value"]: o.get("hint") for o in got}
        assert marked["ollama.qwen3:8b"] == "reads up to 40K tokens · may run out of room on a long build"
        assert marked["claude-sonnet-4-6"] is None and marked["cli.claude"] is None, "known large, or not known"

    def test_a_remembered_one_too_small_is_kept_visible_saying_why(self, node):
        got = node(f"return M.synapseAIOnly({json.dumps(OPTIONS)}, {json.dumps(WINDOWS)}, 'llama3-8b');")
        assert got[0] == {"value": "llama3-8b", "label": "llama3-8b", "group": "Too small for Synapse AI",
                          "hint": "reads up to 8K tokens"}

    def test_with_no_windows_said_nothing_changes(self, node):
        assert node(f"return M.synapseAIOnly({json.dumps(OPTIONS)}, {{}});") == OPTIONS

    @pytest.mark.parametrize("path, pattern", [
        ("components/app/BuilderPanel.tsx", r"synapseAIOnly\(modelOptions\(providers\), modelWindows, sessionModel"),
        ("components/settings/ModelsTab.tsx", r"synapseAIOnly\(buildModelOptions\(providers\), modelWindows, builderModel"),
        ("components/onboarding/ModelSetup.tsx", r"synapseAIOnly\(options, windows, assistant"),
    ])
    def test_each_picker_of_synapse_ais_model_uses_it(self, path, pattern):
        import re

        assert re.search(pattern, (SRC / path).read_text()), path

    def test_the_everyday_and_quick_step_pickers_still_list_every_model(self):
        setup = (SRC / "components/onboarding/ModelSetup.tsx").read_text()
        assert "onChange={setEveryday} options={options}" in setup
        tab = (SRC / "components/settings/ModelsTab.tsx").read_text()
        assert "...withUnreachable(buildModelOptions(providers), builderQuickModel)" in tab

"""Self-hosted Synapse AI runs only on a model that can hold its planner's prompt, and says so (F1592; F1586's class).

F1586 (TRI3, prod 11-13) refused, in cloud, a Synapse AI turn on a model too small for its planner and kept such models
out of cloud's pickers. Self-hosted had the same door open: its route ran any model it was given (`_selected_model`) and
its three pickers (the panel's, Settings → Models' Builder model, onboarding's Synapse AI's model) offered every model,
so a model that cannot read the planner's prompt failed at its first step.

Self-hosted's floor is Naveen's (11-14, in-tab, his second answer: "lets not have 32k floor actually... lets keep the
bigger model", then "128K, same as cloud"): the engine's long-input floor, `LONG_WINDOW`, 128K, as cloud's (F1586). The
planner's first call is about 30,000 tokens (the api_app test measures it), but a build reads more as it goes. Local
models of 32K-40K (SHN1's qwen3 8B, Mistral 7B) are refused for Synapse AI and still run agents and workflows. Since our
own first-run step told people to pull qwen3, it now names a local model that reads 128K, `ollama pull llama3.1` (8B,
131,072 tokens; Naveen: "128K + suggest a 128K local model"), and so does the refusal, with where to change the model.
A model whose window is not known is never refused for it (a CLI, an OpenAI-compatible or local endpoint, Hugging Face,
an Ollama that does not say), as the engine's own pick never rules one out. An Ollama model's window is what Ollama says
it reads (`/api/show`, F1569).
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
MISTRAL_7B = "mistral.mistral-7b-instruct-v0:2"  # 32,768: refused now
AT_THE_FLOOR = "meta.llama3-1-70b-instruct-v1:0"  # Llama 3.1 70B, 128,000: exactly the floor, so it runs
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
    def test_it_is_the_engines_long_input_floor_as_clouds(self):
        assert MM.SYNAPSE_AI_MIN_WINDOW == MM.LONG_WINDOW == 128_000

    async def test_a_model_known_smaller_is_refused_in_words_that_say_what_to_do(self):
        said = await MM.synapse_ai_refusal(SMALL, source="pick")
        assert said is not None
        assert SMALL in said and "8K" in said and "128K" in said, said
        assert "Synapse AI's model menu" in said, "where to change a pick"
        assert "ollama pull llama3.1" in said, "a local model that reads 128K, named"

    async def test_a_32k_model_is_refused_now(self):
        said = await MM.synapse_ai_refusal(MISTRAL_7B, source="pick")
        assert said is not None and "32K" in said, said

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

    async def test_qwen3_8b_is_refused_now(self, ollama):
        ollama["qwen3:8b"] = 40_960  # SHN1's qwen3 8B, which our first-run step suggested
        assert await MM.synapse_ai_window("ollama.qwen3:8b") == 40_960
        said = await MM.synapse_ai_refusal("ollama.qwen3:8b", source="pick")
        assert said is not None and "40K" in said and "ollama pull llama3.1" in said, said

    async def test_the_one_it_names_runs(self, ollama):
        ollama["llama3.1:8b"] = 131_072
        assert await MM.synapse_ai_refusal("ollama.llama3.1:8b", source="pick") is None


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


#: What `/api/models` says (F1592): each known window and the least Synapse AI runs on.
WINDOWS = {"windows": {"llama3-8b": 8_000, "ollama.qwen3:8b": 40_960, "claude-sonnet-4-6": 1_000_000},
           "synapse_ai_min_window": 128_000}
OPTIONS = [{"value": v, "label": v, "group": "G"} for v in ("claude-sonnet-4-6", "llama3-8b", "ollama.qwen3:8b",
                                                            "cli.claude")]


class TestSynapseAIsPickersOfferWhatCanHoldIt:
    def test_one_under_the_floor_is_left_out_and_the_rest_are_as_they_were(self, node):
        got = node(f"return M.synapseAIOnly({json.dumps(OPTIONS)}, {json.dumps(WINDOWS)});")
        assert got == [OPTIONS[0], OPTIONS[3]], "Llama 3 8B and qwen3 8B left out; a large one and an unknown kept"

    def test_a_remembered_one_too_small_is_kept_visible_saying_what_it_needs(self, node):
        got = node(f"return M.synapseAIOnly({json.dumps(OPTIONS)}, {json.dumps(WINDOWS)}, 'ollama.qwen3:8b');")
        assert got[0] == {"value": "ollama.qwen3:8b", "label": "ollama.qwen3:8b", "group": "Too small for Synapse AI",
                          "hint": "reads up to 40K tokens; Synapse AI needs 128K"}

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

    def test_the_first_run_steps_ollama_tip_names_a_model_that_reads_128k(self):
        setup = (SRC / "components/onboarding/ModelSetup.tsx").read_text()
        assert "ollama pull llama3.1" in setup and "ollama pull qwen3" not in setup

    def test_the_everyday_and_quick_step_pickers_still_list_every_model(self):
        setup = (SRC / "components/onboarding/ModelSetup.tsx").read_text()
        assert "onChange={setEveryday} options={options}" in setup
        tab = (SRC / "components/settings/ModelsTab.tsx").read_text()
        assert "...withUnreachable(buildModelOptions(providers), builderQuickModel)" in tab

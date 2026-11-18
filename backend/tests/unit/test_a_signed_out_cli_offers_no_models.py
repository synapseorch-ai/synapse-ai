"""A model CLI's models are offered only while that CLI is signed in (F1603).

Claude Code's list in the pickers is its own: four aliases (`fable`, `opus`, `sonnet`, `haiku`, each the newest model
of its tier the account can use) and the ids its binary carries, and every one of them runs when `claude` is signed in
(checked by hand). Signed out, none does, yet every picker but onboarding's offered them: `/api/models` calls an
installed CLI available whether or not it is signed in (it has to: the engine reads `available` to answer "sign in to
Claude Code" when someone asks for one, F1418), and the pickers read `available` alone. The rule: "Leave the list; hide
signed-out only".

So the one rule the pickers share (`lib/models.ts`) leaves out a provider whose own command says it is signed out
(`cli.signed_in === false`), for each of the four CLIs. One that cannot be told without a model call (`signed_in`
null) stays, as onboarding's own rule kept it (CLIM1). The step picker of a workflow and Data Lab, which took a flat
list of every available model, read the same rule. A model already saved stays visible where it is set (an agent's
model, a step's, Settings'), so nothing saved reads as blank. The engine is not touched.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
MODELS = SRC / "lib" / "models.ts"
SETUP = SRC / "lib" / "modelSetup.ts"
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
        pytest.skip("node is not on PATH: the pickers' helpers are TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(script: str):
        program = (f"import * as M from {json.dumps(MODELS.as_uri())};\n"
                   f"import * as S from {json.dumps(SETUP.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


#: The four model CLIs, as `/api/models` names each provider and two of the models it lists (`cli_status.MODEL_LISTS`,
#: then the bare `cli.<x>` that runs its default).
CLIS = {
    "anthropic_cli": ["cli.claude.sonnet", "cli.claude"],
    "gemini_cli": ["cli.gemini.pro", "cli.gemini"],
    "codex_cli": ["cli.codex.gpt-5-codex", "cli.codex"],
    "github_copilot_cli": ["cli.copilot.auto", "cli.copilot"],
}
KEYED = {"anthropic": {"available": True, "models": ["claude-sonnet-4-6"], "embedding_models": []},
         "openai": {"available": False, "models": ["gpt-4.1"], "embedding_models": []}}


def providers(cli_key: str, signed_in, *, has_cli: bool = True) -> dict:
    """`/api/models`' providers with one CLI installed (`available` true, as the route says of any installed CLI)."""
    info: dict = {"available": True, "models": CLIS[cli_key], "embedding_models": []}
    if has_cli:
        info["cli"] = {"version": "1.0.0", "signed_in": signed_in, "sign_in_hint": "Sign in.", "models_source": "list",
                       "models_note": None, "note": None}
    return {**KEYED, cli_key: info}


def values(options: list[dict]) -> list[str]:
    return [o["value"] for o in options]


class TestThePickersRuleOverACLIsModels:
    @pytest.mark.parametrize("cli", list(CLIS))
    def test_signed_out_its_models_are_not_offered_and_the_rest_are_as_they_were(self, node, cli):
        got = node(f"return M.modelOptions({json.dumps(providers(cli, False))});")
        assert values(got) == ["claude-sonnet-4-6"], cli

    @pytest.mark.parametrize("cli", list(CLIS))
    def test_one_that_cannot_be_told_is_offered(self, node, cli):
        got = node(f"return M.modelOptions({json.dumps(providers(cli, None))});")
        assert values(got) == ["claude-sonnet-4-6", *CLIS[cli]], cli

    @pytest.mark.parametrize("cli", list(CLIS))
    def test_signed_in_it_is_offered(self, node, cli):
        got = node(f"return M.modelOptions({json.dumps(providers(cli, True))});")
        assert values(got) == ["claude-sonnet-4-6", *CLIS[cli]], cli

    def test_a_provider_that_says_nothing_of_a_sign_in_is_offered_as_before(self, node):
        got = node(f"return M.modelOptions({json.dumps(providers('anthropic_cli', None, has_cli=False))});")
        assert values(got) == ["claude-sonnet-4-6", *CLIS["anthropic_cli"]]

    def test_a_saved_model_of_a_signed_out_cli_stays_visible_where_it_is_set(self, node):
        got = node(f"return M.withUnreachable(M.modelOptions({json.dumps(providers('anthropic_cli', False))}), "
                   "'cli.claude.sonnet');")
        assert got[-1] == {"value": "cli.claude.sonnet", "label": "cli.claude.sonnet", "group": "Not reachable"}


class TestTheFlatListsReadTheSameRule:
    """The step picker of a workflow and Data Lab take a plain list of model ids, not the grouped options."""

    @pytest.mark.parametrize("cli", list(CLIS))
    @pytest.mark.parametrize("signed_in", [False, None, True])
    def test_it_lists_what_the_grouped_pickers_offer(self, node, cli, signed_in):
        p = json.dumps(providers(cli, signed_in))
        flat, grouped = node(f"return [M.offeredModels({p}), M.modelOptions({p}).map(o => o.value)];")
        assert flat == grouped, (cli, signed_in)

    def test_a_saved_model_it_no_longer_lists_is_kept_last(self, node):
        got = node("return [M.withSaved(['a', 'b'], 'cli.claude.sonnet'), M.withSaved(['a', 'b'], 'b'), "
                   "M.withSaved(['a', 'b'], undefined), M.withSaved(['a', 'b'], null), M.withSaved(['a', 'b'], '')];")
        assert got == [["a", "b", "cli.claude.sonnet"], ["a", "b"], ["a", "b"], ["a", "b"], ["a", "b"]]


class TestOnboardingKeepsItsRule:
    @pytest.mark.parametrize("cli", list(CLIS))
    @pytest.mark.parametrize("signed_in", [False, None, True])
    def test_its_pickers_offer_what_every_other_picker_offers(self, node, cli, signed_in):
        p = json.dumps(providers(cli, signed_in))
        mine, theirs = node(f"return [S.pickable({p}), M.modelOptions({p})];")
        assert mine == theirs, (cli, signed_in)

    def test_its_found_here_list_still_says_a_signed_out_cli_is_there_and_how_to_sign_in(self, node):
        got = node(f"return S.foundHere({json.dumps(providers('anthropic_cli', False))});")
        assert got == [{"key": "anthropic_cli", "state": "signed-out", "hint": "Sign in.", "models": 2}]


#: Every picker that offers models to run, and how it builds its list. The node tests above hold the rule; these hold
#: each picker to it.
PICKERS = [
    ("components/app/BuilderPanel.tsx", r"synapseAIOnly\(modelOptions\(providers\), modelWindows, sessionModel"),
    ("components/settings/ModelsTab.tsx", r"withUnreachable\(buildModelOptions\(providers\), selectedModel\)"),
    ("components/settings/ModelsTab.tsx", r"synapseAIOnly\(buildModelOptions\(providers\), modelWindows, builderModel"),
    ("components/settings/ModelsTab.tsx", r"withUnreachable\(buildModelOptions\(providers\), builderQuickModel\)"),
    ("components/settings/KnowledgeModels.tsx", r"withUnreachable\(modelOptions\(providers\), value\.knowledge_contextual_model"),
    # Each model's line added around it (F1598's `withHints`) is the same list.
    ("lib/knowledgeHost.tsx", r"chatModels: (?:withHints\()?modelOptions\(providers\)"),
    ("components/settings/EvalsScreen.tsx", r"withUnreachable\(modelOptions\(models\.providers \?\? \{\}\), draft\.judge_model"),
    ("components/settings/AgentsTab.tsx", r"withUnreachable\(modelOptions\(providers \?\? \{\}\), draftAgent\.model \|\| undefined\)"),
    ("components/settings/AgentsTab.tsx", r"const models = modelOptions\(providers \?\? \{\}\);"),
    ("components/onboarding/ModelSetup.tsx", r"pickable\(providers\)"),
    ("components/settings/OrchestrationTab.tsx", r"setAvailableModels\(offeredModels\(data\.providers \?\? \{\}\)\)"),
    ("components/settings/OrchestrationTab.tsx", r"availableModels=\{withSaved\(availableModels, step\.model\)\}"),
    ("components/SettingsView.tsx", r"modelOptions=\{offeredModels\(providers\)\}"),
]


class TestEveryPickerReadsIt:
    @pytest.mark.parametrize("path, pattern", PICKERS, ids=[f"{p}:{i}" for i, (p, _) in enumerate(PICKERS)])
    def test_it_builds_its_list_with_the_shared_rule(self, path, pattern):
        assert re.search(pattern, (SRC / path).read_text()), path

    def test_settings_models_tab_builds_with_the_shared_helper(self):
        assert re.search(r"modelOptions as buildModelOptions", (SRC / "components/settings/ModelsTab.tsx").read_text())

    def test_no_picker_takes_every_available_model_unfiltered(self):
        assert "all_available" not in (SRC / "components/settings/OrchestrationTab.tsx").read_text()
        assert "p.available ? p.models" not in (SRC / "components/SettingsView.tsx").read_text()

    def test_onboarding_has_no_rule_of_its_own(self):
        setup = SETUP.read_text()
        body = setup[setup.index("export function pickable"):]
        body = body[:body.index("\n}\n")]
        assert "signed_in" not in body and "modelOptions(providers)" in body

    def test_the_shared_workflow_components_are_not_changed(self):
        """components/orchestration is synced into cloud byte for byte: the step picker keeps its own shape."""
        shared = (SRC / "components/orchestration/panel/shared.tsx").read_text()
        assert "...availableModels.map((m) => ({ value: m, label: m," in shared

"""A new tool's "Name agents use" follows its Name as it is typed, until the person sets it themselves (F1739).

The tool editor made "Name agents use" from the Name only while it was empty, so it was made from the first letter
and then left: typing "Send email" saved a tool called `s`. Now `lib/toolNameFromTitle.ts` `derivedName` makes it
again at each keystroke while the tool is new and the name is empty or still the one made from the Name before, and
leaves it once the person has typed their own. A saved tool's name never changes with its Name: agents and workflows
call the tool by it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_a_path_before_any_run_draws_a_step_by_what_it_does import HOOKS

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
HELPER = SRC / "lib/toolNameFromTitle.ts"
EDITOR = SRC / "components/settings/CustomToolsTab.tsx"


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the editor's naming is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(script: str):
        program = (f"import * as N from {json.dumps(HELPER.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


#: The editor's keystrokes, one at a time, from a draft: each sets the Name and what `derivedName` makes of it.
TYPE = """
const type = (draft, text, isNew) => {
  let d = { ...draft };
  for (let i = (d.generalName || '').length + 1; i <= text.length; i += 1) {
    const next = text.slice(0, i);
    d = { ...d, generalName: next, name: N.derivedName(d, next, isNew) };
  }
  return d;
};
"""


def test_typing_a_new_tools_name_makes_the_whole_name_agents_use(node):
    assert node(TYPE + "return type({ name: '', generalName: '' }, 'Send email', true).name;") == "send_email"


def test_a_name_the_person_typed_themselves_stays(node):
    script = TYPE + """
      const mid = { ...type({ name: '', generalName: '' }, 'Send', true), name: 'mailer' };
      return type(mid, 'Send email', true).name;"""
    assert node(script) == "mailer"


def test_a_saved_tools_name_never_changes_with_its_name(node):
    assert node(TYPE + "return type({ name: 'send_mail', generalName: 'Send' }, 'Send email', false).name;") == \
        "send_mail"


def test_a_name_cleared_by_hand_is_made_again(node):
    assert node(TYPE + "return type({ name: '', generalName: 'Send email' }, 'Send emails', true).name;") == \
        "send_emails"


def test_it_is_made_as_before_from_any_words(node):
    assert node("return N.slugName('  Process Orders!! (v2) ');") == "process_orders_v2"


def test_the_editor_names_a_new_tool_this_way_and_never_a_saved_one():
    text = EDITOR.read_text("utf-8")
    assert "derivedName(draftTool, val, openName.current === null)" in text
    assert "if (!draftTool.name)" not in text, "the first letter's name is never kept"

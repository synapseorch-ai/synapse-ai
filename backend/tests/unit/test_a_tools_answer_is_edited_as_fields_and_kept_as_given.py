"""A custom tool's output schema is edited as answer fields, and kept as it is given (F1620, part 3).

The custom tool editor showed no output schema at all: it loaded a tool's `outputSchema` into its draft and parsed it
back on Save, so a tool's declared answer could be set only through the API or an import. Now the editor has a "What it
answers" section with F1618 d's answer-field rows. `toolAnswer.ts` is the section's reading of the editor's text:
`toolAnswerOf` reads it into fields as the server reads a declaration (`output_fields.from_json_schema`), and
`toolAnswerText` writes fields back as the server compiles them (`json_schema`).

Kept as given: a schema inside the answer's subset round-trips byte for byte; one with a part outside it gives no fields
and names each part, so the editor shows it read-only and saves it unchanged; no schema gives no fields, and no fields
give no text, so no `outputSchema` is sent, as before.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.orchestration.output_fields import from_json_schema, json_schema

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
TOOL_ANSWER_TS = SRC / "components/settings/toolAnswer.ts"
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

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "email", "type": "string", "description": "Their work email"},
    {"name": "company", "type": "object", "fields": [{"name": "name", "type": "string"}]}]}},
    {"name": "total", "type": "number", "may_be_empty": True}]
OTHERWISE = {"type": "object", "title": "Lead", "properties": {"count": {"type": "integer"},
                                                               "email": {"type": "string", "format": "email"}},
             "required": ["count"]}
OUTSIDE = {"type": "object", "properties": {"id": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                                            "name": {"type": "string", "minLength": 2}},
           "required": ["id", "name"]}

TEXTS = {
    "inside the subset": json.dumps(json_schema(LEADS), indent=2),
    "written otherwise": json.dumps(OTHERWISE),
    "outside the subset": json.dumps(OUTSIDE, indent=2),
    "none": "",
    "blank": "  \n ",
    "not json": "{\"type\": \"object\",",
}


@pytest.fixture(scope="module")
def editor(tmp_path_factory) -> dict:
    """The editor's reading of every text, and its writing of LEADS, in one node run."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the editor's reader is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text("import { register } from 'node:module';\n"
                                        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")
    program = (f"import * as T from {json.dumps(TOOL_ANSWER_TS.as_uri())};\n"
               f"const texts = {json.dumps(TEXTS)};\n"
               "const read = {}; for (const [k, t] of Object.entries(texts)) read[k] = T.toolAnswerOf(t);\n"
               f"const written = T.toolAnswerText({json.dumps(LEADS)});\n"
               "const back = T.toolAnswerText(T.toolAnswerOf(texts['inside the subset']).fields);\n"
               "console.log(JSON.stringify({read, written, back, none: T.toolAnswerText([])}));")
    done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                           (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)


@pytest.mark.parametrize("case", ["inside the subset", "written otherwise"])
def test_a_schema_inside_the_subset_is_read_into_the_fields_the_server_reads(editor, case):
    fields, refused = from_json_schema(json.loads(TEXTS[case]))
    assert refused == [] and editor["read"][case] == {"fields": fields, "kept": []}, editor["read"][case]


def test_one_outside_the_subset_gives_no_fields_and_names_each_part(editor):
    _, refused = from_json_schema(OUTSIDE)
    assert editor["read"]["outside the subset"] == {"fields": [], "kept": refused}
    assert [r.split("`")[1] for r in refused] == ["id", "name"], refused


def test_fields_are_written_as_the_server_compiles_them(editor):
    assert json.loads(editor["written"]) == json_schema(LEADS)


def test_a_schema_inside_the_subset_round_trips_byte_for_byte(editor):
    assert editor["back"] == TEXTS["inside the subset"]


@pytest.mark.parametrize("case", ["none", "blank"])
def test_no_schema_gives_no_fields(editor, case):
    assert editor["read"][case] == {"fields": [], "kept": []}


def test_no_fields_give_no_text_so_no_output_schema_is_sent(editor):
    assert editor["none"] == ""


def test_text_that_is_not_json_is_kept_and_said(editor):
    said = editor["read"]["not json"]
    assert said["fields"] == [] and len(said["kept"]) == 1 and said["kept"][0].startswith("It is not JSON"), said

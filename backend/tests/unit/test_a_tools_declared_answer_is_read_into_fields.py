"""A tool's declared answer (its `outputSchema`) is read into answer fields, by the server as the editor reads it
(F1620, part 1a).

A tool can say what it answers: an MCP tool's `outputSchema`, or a custom tool's. To check a later step's reads of a
Tool step's answer with F1618's checks, the server reads that JSON Schema into `output_fields`, the one representation
of an answer's shape. `output_fields.from_json_schema` is the server's twin of the editor's `answerFieldsFromSchema`
(F1618 d): the same subset, the same fields, and each part outside it named the same way, with nothing taken. A test
runs both on the same schemas and holds them to the same bytes.

Near misses: a schema inside the subset comes back as the declaration it was compiled from; a provider's wrapper is
read through; a schema with one part outside the subset gives no fields at all, so a tool whose answer is not fully
known is never half-checked.
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
ANSWER_TS = SRC / "components/orchestration/answerFields.ts"
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
    {"name": "company", "type": "object", "fields": [
        {"name": "name", "type": "string"},
        {"name": "address", "type": "object", "fields": [{"name": "city", "type": "string", "may_be_empty": True}]}]},
]}}, {"name": "total", "type": "number"}]
FLAT = [{"name": "risky", "type": "boolean", "description": "Whether the change is risky"},
        {"name": "fit", "type": "choice", "options": ["good", "poor"]},
        {"name": "summary", "type": "string"}, {"name": "score", "type": "number"}, {"name": "tags", "type": "list"}]
EMPTY_ONES = [{"name": "s", "type": "number", "may_be_empty": True, "description": "Its score"},
              {"name": "g", "type": "object", "may_be_empty": True, "fields": [{"name": "x", "type": "string"}]},
              {"name": "l", "type": "list", "may_be_empty": True,
               "of": {"fields": [{"name": "y", "type": "boolean"}]}}]
LISTS = [{"name": "n", "type": "list", "of": "number"}, {"name": "b", "type": "list", "of": "boolean"},
         {"name": "t", "type": "list"}, {"name": "g", "type": "list", "of": {"fields": [{"name": "x", "type": "number"}]}}]
OTHERWISE = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Lead", "type": "object",
             "properties": {"count": {"type": "integer"}, "maybe": {"type": ["number", "null"]},
                            "email": {"type": "string", "format": "email"},
                            "when": {"type": "string", "format": "date", "title": "When"},
                            "later": {"type": "string"}},
             "required": ["count", "maybe", "email", "when"]}
REFUSED_PARTS = [{"type": "string", "minLength": 2}, {"type": "number", "maximum": 5}, {"$ref": "#/$defs/x"},
                 {"oneOf": [{"type": "string"}, {"type": "number"}]}, {"anyOf": [{"type": "string"}, {"type": "number"}]},
                 {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
                 {"type": "string", "format": "uri"}, {"type": "number", "enum": [1, 2]}, {"const": "a"},
                 {"type": "array"}, {"type": "array", "items": [{"type": "string"}]},
                 {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}, {"type": "object"},
                 {"type": "string", "contentEncoding": "base64"}, {"type": ["string", "number"]}]

SCHEMAS = {
    "flat": json_schema(FLAT), "may be empty": json_schema(EMPTY_ONES), "four levels": json_schema(LEADS),
    "lists": json_schema(LISTS), "written otherwise": OTHERWISE,
    "openai's wrapper": {"type": "json_schema", "json_schema": {"name": "x", "schema": json_schema(FLAT)}},
    "a schema wrapper": {"name": "x", "strict": True, "schema": json_schema(FLAT)},
    "not an object": {"type": "array", "items": {"type": "string"}}, "a string": "{}",
    "a bad name": {"type": "object", "properties": {"bad name": {"type": "string"}}},
    "every part named": {"type": "object", "properties": {
        "name": {"type": "string", "minLength": 2}, "tags": {"$ref": "#/$defs/tags"},
        "leads": {"type": "array", "items": {"type": "object", "properties": {
            "email": {"type": "string", "format": "uri"}}, "required": ["email"]}}},
        "required": ["name", "tags", "leads"], "$defs": {"tags": {"type": "array"}}},
    **{f"refused {n}": {"type": "object", "properties": {"ok": {"type": "string"}, "x": part}, "required": ["ok", "x"]}
       for n, part in enumerate(REFUSED_PARTS)},
}


def _bytes(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture(scope="module")
def editor(tmp_path_factory) -> dict[str, dict]:
    """The editor's reading of every schema, in one node run."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the editor's reader is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text("import { register } from 'node:module';\n"
                                        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")
    program = (f"import * as A from {json.dumps(ANSWER_TS.as_uri())};\n"
               f"const schemas = {json.dumps(SCHEMAS)};\n"
               "const out = {}; for (const [k, s] of Object.entries(schemas)) out[k] = A.answerFieldsFromSchema(s);\n"
               "console.log(JSON.stringify(out));")
    done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                           (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout)


@pytest.mark.parametrize("case", list(SCHEMAS))
def test_the_server_reads_a_schema_as_the_editor_does_byte_for_byte(editor, case):
    fields, refused = from_json_schema(SCHEMAS[case])
    assert _bytes({"fields": fields, "refused": refused}) == _bytes(editor[case])


@pytest.mark.parametrize("case", ["flat", "may be empty", "four levels", "lists"])
def test_a_schema_inside_the_subset_comes_back_as_itself(case):
    fields, refused = from_json_schema(SCHEMAS[case])
    assert refused == [] and _bytes(json_schema(fields)) == _bytes(SCHEMAS[case])


def test_one_part_outside_the_subset_gives_no_fields_at_all():
    fields, refused = from_json_schema(SCHEMAS["every part named"])
    assert fields == [] and [r.split("`")[1] for r in refused] == ["name", "tags", "leads[].email"], refused

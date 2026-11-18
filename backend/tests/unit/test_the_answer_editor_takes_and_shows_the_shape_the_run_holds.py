"""The step editor's answer fields take a nested shape, from rows, a pasted example or a pasted JSON Schema, and show
the schema the run asks the model for, the very one the server compiles (F1618, commit d: the editor).

A model step's answer can hold groups and lists of groups (commit a), but the editor offered five flat types: a person
who knew the shape they wanted could not give it, paste an example of it, or paste the schema they already had. Now:

* the editor's twin, `answerFields.ts`, compiles a declaration to the server's `output_fields.json_schema` byte for byte,
  and finds the server's `declaration_problems` word for word, so "What the model is asked for" is what is sent;
* "Paste an example" turns a JSON value into fields, their types taken from the values;
* "Paste a JSON Schema" turns a schema inside the answer's subset into fields, and names each part outside it, with
  nothing half-taken: a schema with one refused part gives no fields at all.

Near misses: a flat declaration compiles to what it compiled to before; an optional property, `integer` and a `null`
type are inside the subset; annotations (`title`, `$schema`) are read past; a list's items that disagree are refused,
not guessed at.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.orchestration.output_fields import declaration_problems, json_schema

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
ANSWER_TS = SRC / "components/orchestration/answerFields.ts"
STEPS_TSX = SRC / "components/orchestration/panel/steps.tsx"
#: The answer-field editor a model step's section is (and a custom tool's "What it answers", F1620).
ANSWER_EDITOR_TSX = SRC / "components/orchestration/panel/answerFieldsEditor.tsx"

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
def ts(tmp_path_factory):
    """`answerFields.ts`'s named function on each argument list, in one node run."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the editor's twin is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(name: str, *calls):
        program = (f"import * as A from {json.dumps(ANSWER_TS.as_uri())};\n"
                   f"const calls = {json.dumps([list(c) for c in calls])};\n"
                   f"console.log(JSON.stringify(calls.map((args) => A[{json.dumps(name)}](...args))));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


def _bytes(value) -> str:
    """JSON as `JSON.stringify` writes it: no spaces, its own order, characters as they are."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "email", "type": "string", "description": "Their work email"},
    {"name": "company", "type": "object", "fields": [
        {"name": "name", "type": "string"},
        {"name": "address", "type": "object", "fields": [{"name": "city", "type": "string", "may_be_empty": True}]}]},
]}}, {"name": "total", "type": "number"}]

#: Every shape a declaration can take, the spellings written before the editor's own, and what the server reads past.
DECLARATIONS = {
    "flat, as 2.0.13 wrote it": [
        {"name": "risky", "type": "boolean", "description": "Whether the change is risky"},
        {"name": "fit", "type": "choice", "options": ["good", "poor"]},
        {"name": "summary", "type": "string"}, {"name": "score", "type": "number"}, {"name": "tags", "type": "list"}],
    "older spellings": [
        {"name": "a", "type": "text"}, {"name": "b", "type": "bool"}, {"name": "c", "type": "integer"},
        {"name": "d", "type": "select", "options": ["x", " ", "y "]}, {"name": "e", "type": "array"},
        {"name": "f", "type": "strings"}, {"name": "g", "type": "Group", "fields": [{"name": "h"}]},
        {"name": "i", "type": "long_text"}, {"name": "j", "type": "email"}, {"name": "k", "type": "date"}],
    "read past": [
        {"name": " total ", "type": "number", "description": "   "}, {"type": "string"}, {"name": "  "},
        {"name": "odd", "type": "weird"}, {"name": "pick", "type": "choice"}],
    "lists of each kind": [
        {"name": "n", "type": "list", "of": "Number"}, {"name": "b", "type": "list", "of": "boolean"},
        {"name": "t", "type": "list", "of": "text"}, {"name": "u", "type": "list", "of": "dates"},
        {"name": "g", "type": "list", "of": {"fields": [{"name": "x", "type": "number"}]}},
        {"name": "none", "type": "list", "of": {}}],
    "may be empty, described": [
        {"name": "s", "type": "number", "may_be_empty": True, "description": "Its score"},
        {"name": "g", "type": "object", "may_be_empty": True, "fields": [{"name": "x"}]},
        {"name": "l", "type": "list", "may_be_empty": True, "of": {"fields": [{"name": "y", "type": "boolean"}]}}],
    "nested to four levels": LEADS,
    "past the limits": [
        {"name": "a", "type": "object", "fields": [{"name": "b", "type": "object", "fields": [
            {"name": "c", "type": "object", "fields": [{"name": "d", "type": "object", "fields": [{"name": "e"}]}]}]}]},
        {"name": "bad name", "type": "string"}, {"name": "a", "type": "string"},
        {"name": "g", "type": "object", "fields": []}, {"name": "l", "type": "list", "of": {"fields": []}},
        {"name": "w", "type": "list", "of": "dates"}],
    "too many": [{"name": f"f{i}", "type": "string", "may_be_empty": True} for i in range(62)],
}


class TestTheEditorCompilesWhatTheServerSends:
    @pytest.mark.parametrize("case", list(DECLARATIONS))
    def test_answer_fields_compile_one_schema_in_both(self, ts, case):
        [editor] = ts("answerJsonSchema", [DECLARATIONS[case]])
        assert _bytes(editor) == _bytes(json_schema(DECLARATIONS[case]))

    @pytest.mark.parametrize("case", list(DECLARATIONS))
    def test_the_same_problems_word_for_word(self, ts, case):
        [editor] = ts("answerDeclarationProblems", [DECLARATIONS[case]])
        assert editor == declaration_problems(DECLARATIONS[case])

    def test_the_editor_offers_the_five_and_two_groups(self, ts):
        assert ts("answerFieldTypeLabels", []) == [["Yes / No", "One of", "Text", "Number", "List", "Group",
                                                      "List of groups"]]

    @pytest.mark.parametrize("field, kind", [
        ({"name": "a", "type": "boolean"}, "boolean"), ({"name": "a", "type": "select"}, "choice"),
        ({"name": "a"}, "string"), ({"name": "a", "type": "long_text"}, "string"), ({"name": "a", "type": "int"}, "number"),
        ({"name": "a", "type": "array"}, "list"), ({"name": "a", "type": "list", "of": "number"}, "list"),
        ({"name": "a", "type": "group", "fields": []}, "object"),
        ({"name": "a", "type": "list", "of": {"fields": []}}, "groups"),
        ({"name": "a", "type": "email"}, "email"), ({"name": "a", "type": "date"}, "date"),
        ({"name": "a", "type": "weird"}, None)])
    def test_each_stored_shape_is_shown_as_itself(self, ts, field, kind):
        assert ts("answerFieldKind", [field]) == [kind]


EXAMPLE = {"risky": True, "score": 3, "summary": "Ships Friday", "tags": ["urgent"],
           "leads": [{"email": "ana@example.com", "score": 0.9}, {"email": "ben@example.com", "score": None},
                     {"email": "cy@example.com"}],
           "company": {"name": "Harbor & Pine", "city": "Leeds"}, "note": None, "counts": [1, 2],
           "flags": [True], "nothing_yet": []}


class TestPasteAnExample:
    def test_each_value_gives_its_type(self, ts):
        [got] = ts("answerFieldsFromExample", [EXAMPLE])
        assert got["refused"] == []
        by = {f["name"]: f for f in got["fields"]}
        assert [f["name"] for f in got["fields"]] == list(EXAMPLE)
        assert by["risky"]["type"] == "boolean" and by["score"]["type"] == "number"
        assert by["summary"]["type"] == "string" and by["tags"] == {"name": "tags", "type": "list"}
        assert by["counts"]["of"] == "number" and by["flags"]["of"] == "boolean"
        assert by["nothing_yet"] == {"name": "nothing_yet", "type": "list"}
        assert by["note"] == {"name": "note", "type": "string", "may_be_empty": True}
        assert by["company"]["type"] == "object" and [f["name"] for f in by["company"]["fields"]] == ["name", "city"]
        leads = {f["name"]: f for f in by["leads"]["of"]["fields"]}
        assert by["leads"]["type"] == "list" and leads["email"] == {"name": "email", "type": "string"}
        # Empty in one item and missing from another: it may be empty.
        assert leads["score"] == {"name": "score", "type": "number", "may_be_empty": True}

    def test_what_it_gives_is_a_declaration_the_server_holds(self, ts):
        [got] = ts("answerFieldsFromExample", [EXAMPLE])
        assert declaration_problems(got["fields"]) == []
        schema = json_schema(got["fields"])
        assert schema["properties"]["leads"]["items"]["required"] == ["email", "score"]

    @pytest.mark.parametrize("value, said", [
        ([{"a": 1}], "one object"), ("text", "one object"), ({}, "no fields"),
        ({"first name": "Ana"}, "`first name`"),
        ({"rows": [{"a": 1}, "b"]}, "`rows`"), ({"rows": [1, "b"]}, "`rows`"),
        ({"rows": [{"a": 1}, {"a": "x"}]}, "`rows[].a`"), ({"rows": [1, None]}, "`rows`"),
        ({"grid": [[1, 2]]}, "`grid`"),
        ({"a": {"b": {"c": {"d": {"e": 1}}}}}, "four levels"), ({"g": {}}, "`g`")])
    def test_what_it_cannot_take_is_named_and_nothing_is_taken(self, ts, value, said):
        [got] = ts("answerFieldsFromExample", [value])
        assert got["fields"] == [] and got["refused"], got
        assert any(said in r for r in got["refused"]), got["refused"]

    def test_a_good_example_at_four_levels_is_taken(self, ts):
        [got] = ts("answerFieldsFromExample", [{"a": {"b": {"c": {"d": 1}}}}])
        assert got["refused"] == [] and declaration_problems(got["fields"]) == []


class TestPasteAJsonSchema:
    @pytest.mark.parametrize("case", ["flat, as 2.0.13 wrote it", "may be empty, described", "nested to four levels",
                                      "lists of each kind"])
    def test_a_schema_inside_the_subset_comes_back_as_itself(self, ts, case):
        schema = json_schema(DECLARATIONS[case])
        if case == "lists of each kind":
            # The server sends a list of groups with no fields as a group of nothing; pasted, that is refused.
            schema["properties"].pop("none")
            schema["required"].remove("none")
        [got] = ts("answerFieldsFromSchema", [schema])
        assert got["refused"] == [], got["refused"]
        assert _bytes(json_schema(got["fields"])) == _bytes(schema)

    def test_what_is_inside_the_subset_but_written_otherwise(self, ts):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Lead", "type": "object",
                  "properties": {"count": {"type": "integer"}, "maybe": {"type": ["number", "null"]},
                                 "email": {"type": "string", "format": "email"},
                                 "when": {"type": "string", "format": "date", "title": "When"},
                                 "later": {"type": "string"}},
                  "required": ["count", "maybe", "email", "when"]}
        [got] = ts("answerFieldsFromSchema", [schema])
        assert got["refused"] == [], got["refused"]
        by = {f["name"]: f for f in got["fields"]}
        assert by["count"]["type"] == "number"
        assert by["maybe"] == {"name": "maybe", "type": "number", "may_be_empty": True}
        assert by["email"]["type"] == "email" and by["when"]["type"] == "date"
        assert by["later"] == {"name": "later", "type": "string", "may_be_empty": True}

    def test_a_provider_wrapper_is_read_through(self, ts):
        inner = json_schema(DECLARATIONS["flat, as 2.0.13 wrote it"])
        [a, b] = ts("answerFieldsFromSchema", [{"type": "json_schema", "json_schema": {"name": "x", "schema": inner}}],
                    [{"name": "x", "strict": True, "schema": inner}])
        assert a["refused"] == [] and b["refused"] == []
        assert _bytes(json_schema(a["fields"])) == _bytes(inner) == _bytes(json_schema(b["fields"]))

    @pytest.mark.parametrize("part, said", [
        ({"type": "string", "minLength": 2}, "minLength"), ({"type": "number", "maximum": 5}, "maximum"),
        ({"type": "string", "pattern": "^a"}, "pattern"), ({"type": "array", "items": {"type": "string"}, "minItems": 1},
                                                          "minItems"),
        ({"$ref": "#/$defs/x"}, "$ref"), ({"oneOf": [{"type": "string"}, {"type": "number"}]}, "oneOf"),
        ({"anyOf": [{"type": "string"}, {"type": "number"}]}, "anyOf"),
        ({"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
         "additionalProperties"),
        ({"type": "string", "format": "uri"}, "uri"), ({"type": "number", "enum": [1, 2]}, "enum"),
        ({"const": "a"}, "const"), ({"type": "array"}, "items"), ({"type": "array", "items": [{"type": "string"}]},
                                                                   "items"),
        ({"type": "array", "items": {"type": "array", "items": {"type": "string"}}}, "list of lists"),
        ({"type": "object"}, "no fields"), ({"type": "string", "contentEncoding": "base64"}, "contentEncoding")])
    def test_each_part_outside_it_is_named_and_nothing_is_taken(self, ts, part, said):
        schema = {"type": "object", "properties": {"ok": {"type": "string"}, "x": part}, "required": ["ok", "x"]}
        [got] = ts("answerFieldsFromSchema", [schema])
        assert got["fields"] == [], got
        assert len(got["refused"]) == 1 and "`x`" in got["refused"][0] and said in got["refused"][0], got["refused"]

    def test_every_refused_part_is_named_not_only_the_first(self, ts):
        schema = {"type": "object", "properties": {
            "name": {"type": "string", "minLength": 2}, "tags": {"$ref": "#/$defs/tags"},
            "leads": {"type": "array", "items": {"type": "object", "properties": {
                "email": {"type": "string", "format": "uri"}}, "required": ["email"]}}},
            "required": ["name", "tags", "leads"], "$defs": {"tags": {"type": "array"}}}
        [got] = ts("answerFieldsFromSchema", [schema])
        assert got["fields"] == []
        assert [r.split("`")[1] for r in got["refused"]] == ["name", "tags", "leads[].email"], got["refused"]

    @pytest.mark.parametrize("schema, said", [
        ({"type": "array", "items": {"type": "string"}}, "one object"), ("{}", "one object"),
        ({"type": "object", "properties": {"bad name": {"type": "string"}}}, "`bad name`")])
    def test_a_root_that_is_not_an_answer_is_named(self, ts, schema, said):
        [got] = ts("answerFieldsFromSchema", [schema])
        assert got["fields"] == [] and any(said in r for r in got["refused"]), got


class TestTheStepEditor:
    def test_the_editor_offers_the_ways_in_and_shows_what_is_asked(self):
        """The step's section is the shared answer-field editor, in the step's words (F1620)."""
        steps = STEPS_TSX.read_text(encoding="utf-8")
        editor = ANSWER_EDITOR_TSX.read_text(encoding="utf-8")
        assert "<AnswerFieldsEditor" in steps and "What the model is asked for" in steps
        for words in ("Paste an example", "Paste a JSON Schema",
                      "answerFieldsFromExample(", "answerFieldsFromSchema(", "answerJsonSchema("):
            assert words in editor, words

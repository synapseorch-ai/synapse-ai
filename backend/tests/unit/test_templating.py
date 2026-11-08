"""`{{ … }}` rendering: namespaces, bare names, the untouched unknown, and the
three body modes an HTTP tool can be given."""
import json

import pytest

from core import templating as t

CTX = {"global": {"api_base": "https://api.acme.test", "tone": "dry", "nested": {"k": "v"}},
       "state": {"topic": "gold", "items": [1, 2]}}


class TestRender:
    def test_namespaced_references_resolve(self):
        assert t.render("{{global.api_base}}/x?q={{ state.topic }}", CTX) == "https://api.acme.test/x?q=gold"

    def test_unknown_references_are_left_as_written(self):
        assert t.render("hello {{global.missing}} and {{nothing}}", CTX) == "hello {{global.missing}} and {{nothing}}"

    def test_objects_render_as_json_and_paths_walk_into_them(self):
        assert t.render("{{global.nested}}", CTX) == '{"k": "v"}'
        assert t.render("{{global.nested.k}}", CTX) == "v"
        assert t.render("{{state.items.1}}", CTX) == "2"

    def test_bare_names_resolve_only_when_a_bare_namespace_is_given(self):
        ctx = CTX | {"args": {"body": {"a": 1}, "id": 7}}
        assert t.render("/things/{{id}}", ctx) == "/things/{{id}}"
        assert t.render("/things/{{id}}", ctx, bare="args") == "/things/7"
        assert t.render("{{body.a}}", ctx, bare="args") == "1"

    def test_single_braces_are_not_this_modules_business(self):
        assert t.render("/things/{id} {state.topic}", CTX, bare="args") == "/things/{id} {state.topic}"

    def test_references_lists_each_once(self):
        assert t.references("{{a}} {{ b.c }} {{a}}") == ["a", "b.c"]

    def test_mapping_values_render(self):
        out = t.render_mapping({"Authorization": "Bearer {{global.token}}", "X": 1}, {"global": {"token": "t"}})
        assert out == {"Authorization": "Bearer t", "X": 1}


class TestRequestBody:
    ARGS = {"body": {"data": {"name": "Ada"}, "meta": 1}, "id": 9, "note": "hi"}

    def test_empty_template_means_the_arguments(self):
        body, consumed = t.request_body("", self.ARGS, CTX)
        assert body == self.ARGS and consumed == set()

    def test_one_reference_means_that_argument_is_the_body(self):
        body, consumed = t.request_body("{{body}}", self.ARGS, CTX)
        assert body == self.ARGS["body"] and consumed == {"body"}
        body, consumed = t.request_body("{{ body.data }}", self.ARGS, CTX)
        assert body == {"name": "Ada"} and consumed == {"body"}

    def test_a_reference_to_a_global_is_the_body_and_consumes_nothing(self):
        body, consumed = t.request_body("{{global.nested}}", self.ARGS, CTX)
        assert body == {"k": "v"} and consumed == set()

    def test_a_json_template_splices_values_and_consumes_what_it_used(self):
        template = '{"customer": {{body.data}}, "ref": "{{id}}", "note": "{{note}}", "base": "{{global.api_base}}"}'
        body, consumed = t.request_body(template, self.ARGS, CTX)
        assert body == {"customer": {"name": "Ada"}, "ref": 9, "note": "hi", "base": "https://api.acme.test"}
        assert consumed == {"body", "id", "note"}

    def test_an_unknown_reference_in_a_template_stays_and_the_text_is_sent_as_is(self):
        body, consumed = t.request_body('{"x": "{{ghost}}"}', self.ARGS, CTX)
        assert body == {"x": "{{ghost}}"} and consumed == set()

    def test_a_non_json_template_is_sent_as_text(self):
        body, _ = t.request_body("id={{id}}&note={{note}}", self.ARGS, CTX)
        assert body == "id=9&note=hi"

    @pytest.mark.parametrize("template", ["{{missing}}", "{{ body.nope }}"])
    def test_a_missing_single_reference_is_none(self, template):
        body, consumed = t.request_body(template, self.ARGS, CTX)
        assert body is None and consumed == set()

    def test_json_roundtrip_of_a_string_value_is_not_double_quoted(self):
        body, _ = t.request_body('{"n": "{{note}}"}', self.ARGS, CTX)
        assert json.dumps(body) == '{"n": "hi"}'


class TestABareNamespace:
    """`{{args}}` is the whole argument payload — the one thing the single-brace
    syntax could say (`{data}`) and this one could not.

    Added when the URL field was moved onto the double-brace syntax the rest of
    the product already used: "one syntax everywhere" is only true if everything
    the old spelling could express has a new spelling.
    """

    def test_args_with_no_path_is_the_whole_payload(self):
        rendered = t.render("{{args}}", {"args": {"a": 1, "b": 2}}, bare="args")
        assert json.loads(rendered) == {"a": 1, "b": 2}

    def test_a_path_into_args_still_works(self):
        assert t.render("{{args.b.c}}", {"args": {"b": {"c": 7}}}, bare="args") == "7"

    def test_a_bare_global_is_NOT_expanded(self):
        """It would render every variable an organisation has — including the
        ones backed by secrets on a hosted deployment — into whatever URL or
        prompt it appeared in. Left unresolved, visibly."""
        out = t.render("{{global}}", {"global": {"k": "v", "token": "S3CRET"}})
        assert out == "{{global}}"
        assert "S3CRET" not in out

    def test_a_bare_state_is_not_expanded_either(self):
        assert t.render("{{state}}", {"state": {"k": "v"}}) == "{{state}}"

    def test_args_is_absent_when_there_is_no_args_namespace(self):
        """A prompt template has no arguments, so `{{args}}` there is not a
        reference to anything and must stay visible rather than render empty."""
        assert t.render("{{args}}", {"global": {}, "state": {}}) == "{{args}}"

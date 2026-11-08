"""Turning an MCP server's JSON Schema into something Gemini will accept.

The tool list is declared up front and validated as a whole, so **one property
on one tool of one server takes down every turn that agent makes**. Vercel's
`update_project_deployment_protection` declares `passwordProtection` as `oneOf`,
and what the customer saw as their agent's answer was:

    3 validation errors for FunctionDeclaration
    parameters.properties.passwordProtection.oneOf
      Extra inputs are not permitted

The cleaner was a list of keys to strip and it was rewritten three times in one
sitting — `oneOf`, then non-string `enum` values, then `exclusiveMinimum` — each
time because a real server declared something nobody had listed. It filters to
what `Schema` accepts now, which cannot be surprised by a keyword.
"""
import pytest

from core.llm_providers import _clean_schema_for_gemini, _convert_tools_for_gemini


def declare(parameters: dict):
    """Build a real FunctionDeclaration, which is where the validation lives."""
    return _convert_tools_for_gemini(
        [{"type": "function", "function": {"name": "t", "description": "", "parameters": parameters}}]
    )[0].function_declarations[0]


class TestComposition:
    def test_one_of_becomes_any_of(self):
        """The shape that broke it. `Schema` has `anyOf` and no `oneOf`."""
        d = declare({
            "type": "object",
            "properties": {
                "passwordProtection": {
                    "oneOf": [
                        {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
                        {"type": "null"},
                    ]
                }
            },
        })
        assert len(d.parameters.properties["passwordProtection"].any_of) == 2

    def test_all_of_is_merged_into_the_parent(self):
        """Members are object fragments contributing properties, which is what a
        merge produces. A repeated `required` name is not repeated after."""
        d = declare({
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "number"}}, "required": ["a"]},
            ]
        })
        assert sorted(d.parameters.properties) == ["a", "b"]
        assert d.parameters.required == ["a"]

    def test_not_is_dropped_without_leaving_a_typeless_node(self):
        """Dropping a composition keyword is only safe if a type is put back:
        Gemini rejects a property with no type, with a different error."""
        d = declare({"type": "object", "properties": {"x": {"not": {"type": "string"}}}})
        assert d.parameters.properties["x"].type is not None


class TestLiterals:
    def test_a_string_const_becomes_a_one_value_enum(self):
        d = declare({"type": "object", "properties": {"kind": {"const": "fixed"}}})
        assert list(d.parameters.properties["kind"].enum) == ["fixed"]

    def test_a_boolean_const_keeps_its_type(self):
        """`Schema.enum` is `list[str]`. Coercing `true` to `"true"` would have
        the model send a string where the tool wants a boolean — a worse failure
        than a loose schema, because it happens inside the tool at call time."""
        d = declare({"type": "object", "properties": {"on": {"const": True}}})
        prop = d.parameters.properties["on"]
        assert prop.type == "BOOLEAN"
        assert not prop.enum
        assert "true" in (prop.description or "")

    def test_a_numeric_enum_keeps_its_type_and_states_the_values(self):
        d = declare({"type": "object", "properties": {"n": {"enum": [1, 2, 3]}}})
        prop = d.parameters.properties["n"]
        assert prop.type == "NUMBER"
        assert "1, 2, 3" in (prop.description or "")

    def test_a_string_enum_is_left_alone(self):
        d = declare({"type": "object", "properties": {"s": {"enum": ["a", "b"]}}})
        assert list(d.parameters.properties["s"].enum) == ["a", "b"]


class TestTypeUnions:
    """`Schema.type` is one name. JSON Schema's is a name *or a list of names*.

    zod's `z.union([z.boolean(), z.string()])` serialises to the list form, and
    the official sequential-thinking server declares three fields through one,
    so what every agent with it attached answered was:

        3 validation errors for FunctionDeclaration
        parameters.properties.nextThoughtNeeded.type
          Input should be 'TYPE_UNSPECIFIED', 'STRING', ...
    """

    def test_the_sequentialthinking_schema_that_broke_it(self):
        """Verbatim, including the three fields that made it three errors."""
        d = declare({
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "nextThoughtNeeded": {"type": ["boolean", "string"],
                                      "description": "Whether another thought step is needed"},
                "thoughtNumber": {"type": "integer", "minimum": 1},
                "isRevision": {"type": ["boolean", "string"]},
                "needsMoreThoughts": {"type": ["boolean", "string"]},
            },
            "required": ["thought", "nextThoughtNeeded", "thoughtNumber"],
        })
        assert sorted(d.parameters.properties) == [
            "isRevision", "needsMoreThoughts", "nextThoughtNeeded",
            "thought", "thoughtNumber",
        ]

    def test_a_two_type_union_becomes_any_of(self):
        """The same translation `oneOf` gets — `Schema` has `anyOf` and no way
        to hold two type names in one field."""
        d = declare({"type": "object",
                     "properties": {"x": {"type": ["boolean", "string"]}}})
        prop = d.parameters.properties["x"]
        assert [b.type for b in prop.any_of] == ["BOOLEAN", "STRING"]
        assert prop.type is None

    def test_null_in_a_union_becomes_nullable(self):
        """`["string", "null"]` is how JSON Schema spells optional. Turning it
        into two `anyOf` branches would lose that it is *a string*, which is
        what the model needs to know to fill the argument in."""
        d = declare({"type": "object",
                     "properties": {"s": {"type": ["string", "null"]}}})
        prop = d.parameters.properties["s"]
        assert prop.type == "STRING"
        assert prop.nullable is True
        assert not prop.any_of

    def test_a_union_of_only_null_still_has_a_type(self):
        d = declare({"type": "object", "properties": {"n": {"type": ["null"]}}})
        assert d.parameters.properties["n"].type == "NULL"

    def test_a_union_does_not_overrule_a_real_one_of(self):
        """The branches already say what the node accepts; a type list beside
        them can only contradict it."""
        d = declare({
            "type": "object",
            "properties": {
                "x": {"type": ["string", "number"],
                      "oneOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}
            },
        })
        prop = d.parameters.properties["x"]
        assert len(prop.any_of) == 1
        assert prop.any_of[0].type == "OBJECT"

    def test_an_unrecognised_type_name_is_dropped_not_passed_through(self):
        """The quiet half of this bug. `types.Type` *accepts* an unknown name
        with only a `UserWarning`, then ships `{"type": "bool"}` — a 400 on
        every call the agent makes, with nothing raised locally to catch."""
        d = declare({"type": "object", "properties": {"b": {"type": "bool"}}})
        assert d.parameters.properties["b"].type == "STRING"

    def test_an_unrecognised_name_inside_a_union_is_ignored(self):
        d = declare({"type": "object", "properties": {"x": {"type": ["bool", "string"]}}})
        assert d.parameters.properties["x"].type == "STRING"


class TestTheWhitelist:
    def test_an_unknown_keyword_cannot_reach_the_declaration(self):
        """The property this whole approach has: a keyword nobody anticipated is
        filtered by not being on the list, rather than by being on another one."""
        d = declare({
            "type": "object",
            "properties": {"x": {"type": "string", "somethingNewIn2027": {"a": 1}}},
        })
        assert d.parameters.properties["x"].type == "STRING"

    def test_exclusive_bounds_survive_as_prose(self):
        """Gemini has no exclusive form. Dropping it silently would leave the
        model unable to honour a bound it was never told about."""
        cleaned = _clean_schema_for_gemini({"type": "number", "exclusiveMinimum": 0})
        assert "exclusiveMinimum" not in cleaned
        assert "greater than 0" in cleaned["description"]


class TestContainers:
    def test_properties_is_a_map_of_schemas_not_a_schema(self):
        """Recursing into it as one gave `properties` a `type` of its own, and
        Gemini answered `properties.type: Input should be a valid dictionary`."""
        cleaned = _clean_schema_for_gemini(
            {"type": "object", "properties": {"a": {"type": "string"}}}
        )
        assert "type" not in cleaned["properties"]
        assert cleaned["properties"]["a"]["type"] == "string"

    def test_a_node_with_properties_and_no_type_becomes_an_object(self):
        cleaned = _clean_schema_for_gemini({"properties": {"a": {"type": "string"}}})
        assert cleaned["type"] == "object"

    def test_nested_items_are_cleaned(self):
        d = declare({
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object",
                                                    "properties": {"k": {"const": "v"}}}}
            },
        })
        item = d.parameters.properties["rows"].items
        assert list(item.properties["k"].enum) == ["v"]


class TestNothingIsLost:
    def test_every_tool_still_becomes_a_declaration(self):
        """A tool dropped for a bad schema is a capability the customer
        configured and cannot use, with nothing saying why."""
        tools = [
            {"type": "function", "function": {"name": f"t{i}", "description": "",
                                              "parameters": params}}
            for i, params in enumerate([
                {"type": "object", "properties": {"a": {"oneOf": [{"type": "string"}]}}},
                {"allOf": [{"type": "object", "properties": {"b": {"type": "number"}}}]},
                {"type": "object", "properties": {"c": {"const": 7}}},
                {},
            ])
        ]
        built = _convert_tools_for_gemini(tools)
        assert len(built[0].function_declarations) == len(tools)

    def test_one_unconvertible_tool_does_not_cost_the_agent_the_others(self):
        """The cleaner has been rewritten four times by real MCP servers, so
        there will be a fifth keyword. Until then, a tool that still fails to
        build is dropped on its own — it used to raise, and the agent lost
        every tool it had on every turn.

        `object` is not a valid `description`, so the SDK refuses this one for a
        reason the cleaner does not police.
        """
        tools = [
            {"type": "function", "function": {"name": "good", "description": "",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "bad", "description": object(),
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "also_good", "description": "",
                                              "parameters": {"type": "object"}}},
        ]
        built = _convert_tools_for_gemini(tools)
        assert [d.name for d in built[0].function_declarations] == ["good", "also_good"]


class TestWhatReachesTheApi:
    """Local validation is not the bar — the API's is.

    `types.Schema` is the SDK's general schema type, used for structured output
    too, and `FunctionDeclaration.parameters` is validated server-side against
    something narrower. So a key can pass pydantic here and come back as

        Unknown name "additional_properties" at
        'tools[0].function_declarations[0].parameters.properties[3].value'

    which is a 400 on every call that agent makes. Deriving the whitelist from
    `Schema.model_fields` alone let exactly that through, so this asserts the
    *serialised payload* rather than that the object constructed.
    """

    REFUSED = {
        "additional_properties", "additionalProperties", "default",
        "min_properties", "minProperties", "max_properties", "maxProperties",
        "defs", "$defs", "ref", "$ref",
        "oneOf", "allOf", "const", "not", "exclusiveMinimum", "exclusiveMaximum",
    }

    def _keys(self, node, path="", found=None):
        found = found if found is not None else []
        if isinstance(node, dict):
            for k, v in node.items():
                if k in self.REFUSED:
                    found.append(f"{path}.{k}")
                self._keys(v, f"{path}.{k}", found)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._keys(v, f"{path}[{i}]", found)
        return found

    def test_nothing_the_api_refuses_survives_serialisation(self):
        import json as jsonlib

        gnarly = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "a": {"type": "string", "default": "x"},
                "b": {"type": "number", "exclusiveMinimum": 0},
                "c": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                "d": {"const": True},
                "e": {"$ref": "#/$defs/thing"},
                "f": {"type": "object", "minProperties": 1, "maxProperties": 3},
                "g": {"type": ["boolean", "string"]},
                "h": {"type": ["string", "null"]},
                "i": {"type": "bool"},
            },
            "$defs": {"thing": {"type": "string"}},
        }
        decl = declare(gnarly)
        payload = jsonlib.loads(decl.model_dump_json(exclude_none=True, by_alias=True))
        assert self._keys(payload) == []

    def test_no_type_in_the_payload_is_ever_a_list_or_an_unknown_name(self):
        """`type` is the one whitelisted keyword whose *value* is constrained
        too, and both ways it goes wrong end at the API: a list is refused
        locally and takes the whole tool list with it, an unknown name is
        accepted locally and 400s on every call."""
        import json as jsonlib

        decl = declare({
            "type": "object",
            "properties": {
                "a": {"type": ["boolean", "string"]},
                "b": {"type": ["string", "null"]},
                "c": {"type": "bool"},
                "d": {"type": "array", "items": {"type": ["number", "null"]}},
            },
        })
        payload = jsonlib.loads(decl.model_dump_json(exclude_none=True, by_alias=True))

        seen = []

        def walk(node):
            if isinstance(node, dict):
                if "type" in node:
                    seen.append(node["type"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(payload)
        assert seen, "nothing was checked"
        assert all(isinstance(t, str) for t in seen), seen
        assert all(t in {"TYPE_UNSPECIFIED", "STRING", "NUMBER", "INTEGER",
                         "BOOLEAN", "ARRAY", "OBJECT", "NULL"} for t in seen), seen

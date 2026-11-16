"""A model step's answer holds a typed shape: groups and lists of groups, each part named by its path (F1618).

A model step's answer fields were flat: text, a number, a yes or no, a choice or a list of text. A step whose answer is
"a list of leads, each with a name, an email and a score" could not be declared, so a loop over it, and every read of
`item.email`, was held to nothing. Now a field can be a group (`"type": "object"` with its own `fields`) or a list of
groups (`"type": "list"` with `"of": {"fields": […]}`), to four levels. The run asks for that shape and reads it,
naming each problem by its path (`leads[2].email is missing`), and the one repair it asks for names those paths too.
A field that may be empty says so (`"may_be_empty": true`) and is sent as itself or nothing. A flat declaration is
sent exactly as before, so no saved workflow changes. A declaration deeper than four levels, with more than 60 fields
in all, or with more than 16 that may be empty (a provider's own limit), is refused when it is made.
"""
from __future__ import annotations

from core.orchestration import output_fields as O

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "name"}, {"name": "email", "type": "email"}, {"name": "score", "type": "number"}]}},
    {"name": "summary", "type": "text"}]
LEAD = [{"name": "lead", "type": "object", "fields": [
    {"name": "name"}, {"name": "company", "type": "object", "fields": [{"name": "size", "type": "number"}]}]}]


class TestAFlatDeclarationIsSentAsBefore:
    def test_the_schema_is_the_same_object(self):
        flat = [{"name": "risky", "type": "boolean", "description": "Whether it is risky"},
                {"name": "category", "type": "choice", "options": ["billing", "tech"]}, {"name": "tags", "type": "list"},
                {"name": "count", "type": "number"}, {"name": "note"}]
        assert O.json_schema(flat) == {
            "additionalProperties": False,
            "properties": {"category": {"enum": ["billing", "tech"], "type": "string"}, "count": {"type": "number"},
                           "note": {"type": "string"},
                           "risky": {"description": "Whether it is risky", "type": "boolean"},
                           "tags": {"items": {"type": "string"}, "type": "array"}},
            "required": ["risky", "category", "tags", "count", "note"], "type": "object"}


class TestTheShapeIsAsked:
    def test_a_list_of_groups(self):
        leads = O.json_schema(LEADS)["properties"]["leads"]
        assert leads["type"] == "array"
        item = leads["items"]
        assert item["type"] == "object" and item["additionalProperties"] is False
        assert item["required"] == ["name", "email", "score"]
        assert item["properties"]["score"] == {"type": "number"}

    def test_a_group_in_a_group(self):
        lead = O.json_schema(LEAD)["properties"]["lead"]
        assert lead["type"] == "object" and lead["required"] == ["name", "company"]
        assert lead["properties"]["company"]["properties"]["size"] == {"type": "number"}

    def test_a_field_that_may_be_empty_is_itself_or_nothing(self):
        schema = O.json_schema([{"name": "phone", "type": "text", "may_be_empty": True}])
        assert schema["properties"]["phone"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
        assert schema["required"] == ["phone"]

    def test_the_prompt_says_the_shape(self):
        said = O.instruction(LEADS)
        assert "leads" in said and "email" in said and "score" in said


class TestTheAnswerIsReadByItsShape:
    def test_a_list_of_groups_is_typed_part_by_part(self):
        answer, problems = O.read(LEADS, '{"leads": [{"name": "Ada", "email": "ada@example.com", "score": "7"}], '
                                         '"summary": "One lead."}')
        assert problems == [], problems
        assert answer["leads"][0]["score"] == 7

    def test_each_problem_is_named_by_its_path(self):
        text = ('{"leads": [{"name": "Ada", "email": "ada@example.com", "score": "high"},'
                ' {"name": "Bo", "email": "bo@example.com", "score": 3}, {"name": "Cy", "score": 5}],'
                ' "summary": "Three leads."}')
        answer, problems = O.read(LEADS, text)
        assert answer is None
        said = " ".join(problems)
        assert "`leads[0].score`" in said and "not a number" in said, problems
        assert "`leads[2].email` is missing" in said, problems

    def test_a_missing_group_is_named(self):
        _, problems = O.read(LEAD, '{"lead": {"name": "Ada"}}')
        assert "`lead.company` is missing" in " ".join(problems), problems

    def test_an_empty_value_where_it_may_be_empty(self):
        answer, problems = O.read([{"name": "phone", "type": "text", "may_be_empty": True}], '{"phone": null}')
        assert problems == [] and answer == {"phone": None}, (answer, problems)


class TestTheRepairNamesThePaths:
    def test_at_most_five_then_how_many_more(self):
        problems = [f"`leads[{i}].email` is missing" for i in range(8)]
        said = O.repair_prompt("Find leads.", '{"leads": []}', problems, LEADS)
        assert "`leads[4].email`" in said and "`leads[5].email`" not in said and "3 more" in said, said


class TestADeclarationThatCannotBeHeldIsRefused:
    def test_deeper_than_four_levels(self):
        deep = {"name": "e", "type": "text"}
        for level in "dcba":
            deep = {"name": level, "type": "object", "fields": [deep]}
        assert any("four levels" in p for p in O.declaration_problems([deep])), O.declaration_problems([deep])

    def test_more_than_sixty_fields(self):
        many = [{"name": f"f{i}"} for i in range(61)]
        assert any("60" in p for p in O.declaration_problems(many))

    def test_more_than_sixteen_that_may_be_empty(self):
        many = [{"name": f"f{i}", "may_be_empty": True} for i in range(17)]
        assert any("16" in p for p in O.declaration_problems(many))

    def test_a_group_with_no_fields(self):
        assert any("lead" in p and "fields" in p for p in O.declaration_problems([{"name": "lead", "type": "object"}]))

    def test_a_list_of_an_unknown_kind(self):
        bad = [{"name": "leads", "type": "list", "of": "people"}]
        assert O.declaration_problems(bad), "a list of an unknown kind is refused"

    def test_a_good_nested_declaration_is_held(self):
        assert O.declaration_problems(LEADS) == [] and O.declaration_problems(LEAD) == []

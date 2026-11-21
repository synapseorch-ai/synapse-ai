"""A practice answer is made from its declared shape (F1619).

Before the builder shows a plan, it runs the plan once on made-up answers. Each answer is made from the shape its
step declares: every field there, each a value of its type, a list with two items, a group with its own fields to its
depth. The run reads it as it reads a model's answer, so `output_fields.read` takes a made-up answer as it is.
"""
from __future__ import annotations

from core.orchestration import output_fields as of

_EVERY_TYPE = [
    {"name": "title", "type": "text"},
    {"name": "notes", "type": "textarea"},
    {"name": "price", "type": "number"},
    {"name": "in_stock", "type": "boolean"},
    {"name": "contact", "type": "email"},
    {"name": "when", "type": "date"},
    {"name": "phone", "type": "phone"},
    {"name": "size", "type": "choice", "options": ["small", "large"]},
    {"name": "tags", "type": "list"},
    {"name": "scores", "type": "list", "of": "number"},
    {"name": "flags", "type": "list", "of": "boolean"},
    {"name": "maybe", "type": "text", "may_be_empty": True},
]

_NESTED = [
    {"name": "data", "type": "list", "of": {"fields": [
        {"name": "name", "type": "text"},
        {"name": "address", "type": "object", "fields": [
            {"name": "city", "type": "text"},
            {"name": "geo", "type": "object", "fields": [{"name": "lat", "type": "number"}]},
        ]},
    ]}},
    {"name": "total", "type": "number"},
]


def test_every_field_is_there_as_a_value_of_its_type():
    answer = of.sample(_EVERY_TYPE)
    assert set(answer) == {f["name"] for f in _EVERY_TYPE}
    assert isinstance(answer["title"], str) and answer["title"]
    assert isinstance(answer["price"], (int, float)) and not isinstance(answer["price"], bool)
    assert answer["in_stock"] is True
    assert answer["size"] == "small"
    assert len(answer["tags"]) == 2 and all(isinstance(t, str) for t in answer["tags"])
    assert all(isinstance(n, (int, float)) for n in answer["scores"])
    # A field that may be empty is there: the practice tests the shape, and the read checks judge the empties.
    assert answer["maybe"] is not None


def test_the_run_reads_a_made_up_answer_as_it_is():
    for fields in (_EVERY_TYPE, _NESTED):
        answer = of.sample(fields)
        read, problems = of.read(fields, answer)
        assert problems == [] and read == answer, problems


def test_a_list_of_groups_holds_two_items_each_with_its_fields_to_its_depth():
    answer = of.sample(_NESTED)
    assert len(answer["data"]) == 2
    for item in answer["data"]:
        assert set(item) == {"name", "address"}
        assert isinstance(item["address"]["geo"]["lat"], (int, float))
    # Every path the shape declares reads something, so the practice's misses are the read's, never the sample's.
    assert of.path_problem(_NESTED, ["data", "1", "address", "geo", "lat"]) is None


def test_two_items_of_a_list_differ_so_code_that_keys_on_them_sees_two():
    answer = of.sample(_NESTED)
    assert answer["data"][0] != answer["data"][1]


def test_a_shape_with_no_fields_makes_an_empty_object_and_an_unknown_type_makes_text():
    assert of.sample([]) == {}
    assert isinstance(of.sample([{"name": "x", "type": "nonsense"}])["x"], str)

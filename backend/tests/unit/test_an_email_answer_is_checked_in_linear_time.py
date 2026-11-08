"""F1472 (CodeQL 9877): a Human step's email answer is checked in time that grows with its length, not its square.

The check read `^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$`. The domain's first run also takes dots, so a long dotted answer
that fails at its end was tried at every dot. Every answer door reaches it (the run page, v1/v2, MCP, a channel).
Now a domain is labels without dots joined by dots, one way to read it, and an answer longer than an address can
be (254 characters, RFC 5321) is refused before the pattern runs."""
from __future__ import annotations

import time

import pytest

from core.orchestration import human_fields as H
from core.orchestration.human_fields import BadAnswer, check_value

EMAIL = {"name": "email", "type": "email", "label": "Work email"}
#: A dotted domain that fails at its very end: CodeQL's shape ('!@!.' then many '!.').
SLOW = "a@a." + "a." * 40_000 + " "


def test_the_pattern_reads_a_long_dotted_answer_in_linear_time():
    start = time.perf_counter()
    assert H._EMAIL.match(SLOW) is None
    assert time.perf_counter() - start < 0.5


def test_a_long_answer_is_refused_quickly_and_in_words():
    start = time.perf_counter()
    with pytest.raises(BadAnswer) as said:
        check_value(EMAIL, SLOW.strip() + "@")
    assert time.perf_counter() - start < 0.5
    assert "Work email" in str(said.value) and "not an email address" in str(said.value)


def test_an_address_as_long_as_one_may_be_is_taken_and_one_longer_is_not():
    local = "a" * 64
    longest = f"{local}@{'b' * 63}.{'c' * 63}.{'d' * (254 - len(local) - len('@') - 63 - len('.') - 63 - len('.'))}"
    assert len(longest) == 254
    assert check_value(EMAIL, longest) == longest
    with pytest.raises(BadAnswer):
        check_value(EMAIL, longest + "d")


@pytest.mark.parametrize("said", [
    "ana@example.com", " ana@example.com ", "first.last+tag@sub.example.org", "x@y.z", "o'neil@example.ie",
    "ünï@exämple.de", "a@b.c.d.e",
])
def test_an_address_is_taken_as_before(said):
    assert check_value(EMAIL, said) == said.strip()


@pytest.mark.parametrize("said", [
    "ana-at-example", "a@b", "a b@c.d", "a@@b.c", "@b.c", "a@", "a@b.", "a@.b", "a@b c.d", "a@b.c@d.e",
    # The one answer read differently: an empty label between two dots was taken; no mail server takes it.
    "a@b..c",
])
def test_what_is_not_an_address_is_refused(said):
    with pytest.raises(BadAnswer):
        check_value(EMAIL, said)

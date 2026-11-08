"""A Tool step does not retry a refusal no argument can change — F851 (BF11, D311; QA1b).

Found on cloud's prod by QA1b (D314): a Tool step whose HTTP tool's URL was built from a variable
(`https://{{ global.carrier_host }}/…`, the variable set to 10.0.0.1) was refused at call time by the
egress guard ("10.0.0.1 resolves to a blocked range: private network"), correctly, and then asked the
model for new arguments and tried again, to its Max turns: 15 attempts, 78% of the run's model cost.
The URL's host is the tool's own (a host the model would fill is refused when the tool is saved), so
no argument the model writes can change the answer. The guard's refusals are final: a blocked range,
no host, not a URL, a scheme that is not http. A host that could not be resolved is retried as before:
DNS can fail for a moment.
"""
from __future__ import annotations

import pytest

from core import egress_guard
from tests.unit.test_a_tool_step_does_not_repeat_a_call import _run, _tool, sent  # noqa: F401


@pytest.mark.parametrize("refusal", [
    egress_guard.BlockedAddress("10.0.0.1 resolves to a blocked range: private network", "10.0.0.1"),
    egress_guard.BlockedEgress("that URL has no host"),
    egress_guard.MalformedUrl("that is not a URL; it has no http:// or https:// scheme"),
])
async def test_the_guards_refusal_is_tried_once(fake_llm, sent, refusal):  # noqa: F811
    from _fakes.fake_llm import tool_call

    await _tool("carrier_host_probe", "GET")
    sent.outcome = refusal
    fake_llm.script([tool_call("carrier_host_probe")] * 5)
    status, said = await _run("carrier_host_probe")
    assert len(sent.calls) == 1, "no argument can change where the tool's own URL points"
    assert status == "failed" and str(refusal) in said


async def test_a_host_that_did_not_resolve_is_tried_again(fake_llm, sent):  # noqa: F811
    from _fakes.fake_llm import tool_call

    await _tool("flaky_dns", "GET")
    sent.outcome = egress_guard.UnresolvedHost("could not resolve api.example.com")
    fake_llm.script([tool_call("flaky_dns")] * 3)
    status, _ = await _run("flaky_dns")
    assert len(sent.calls) == 3 and status == "failed"

"""A team's call is projected at what its calls write, not at its whole response cap (F865).

QA1c on prod (rc3, 2026-10-22, S5 on Naveen's key): a workflow with a Team step, run cap $0.20, lead and members on
Claude Haiku 4.5 with Max response length 16,384 (raised after F863's error told QA1c to). The writer's first call
was refused "Stopped at this run's cap of $0.20 in model costs." when the run had spent $0.089 in all: `allowance`
priced 16,384 tokens out (about $0.082) as if every call wrote its whole cap, and a member's check adds one more of
the lead's — about $0.17 of "need" for one call.

Now a call is projected at the team's own calls' average output (never under CE1's 1,600 prior, never over the cap
it is sent with), and a stop on a projection says it is one, with what was spent.
"""
import pytest

from core import team as team_mod
from core.team import StepLimits, TeamConfig

HAIKU = "claude-haiku-4-5-20251001"


@pytest.fixture(autouse=True)
def haiku_priced(monkeypatch):
    """Anthropic's price for Haiku 4.5 ($1 in, $5 out per million): the tests' rate card prices nothing."""
    from core import usage_tracker

    def price(model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
        return (input_tokens * 1.0 + output_tokens * 5.0) / 1_000_000

    monkeypatch.setattr(usage_tracker, "calculate_cost", price)


def _team(limits=None, spent=0.0):
    t = team_mod.Team(lead={"id": "agent_lead"}, members={}, config=TeamConfig(3, 6, None),
                      session_id="s_f865", source="orchestration", run_id="run_f865", server_module=None,
                      lead_model=HAIKU, should_stop=None, can_park=False, state={"spent_usd": spent})
    t.limits = limits
    return t


def _calls(t, *outputs, tokens_in=4_000):
    token = team_mod.current.set(t)
    try:
        for out in outputs:
            team_mod.note_cost(HAIKU, tokens_in, out)
    finally:
        team_mod.current.reset(token)


class TestTheProjection:
    def test_qa1cs_member_call_fits_the_cap_it_was_refused_at(self):
        t = _team(StepLimits(usd_left=0.20, cap_usd=0.20))
        _calls(t, *[1_000] * 6, tokens_in=6_000)    # what the team's calls wrote so far: $0.066
        assert t.spent_usd == pytest.approx(0.066)
        assert t.may_call(HAIKU, 20_000, 16_384, lead=True)
        assert t.may_call(HAIKU, 20_000, 16_384, lead=False), "a $0.20 cap with under half spent has room"

    def test_a_call_is_projected_at_what_the_teams_calls_write(self):
        t = _team()
        _calls(t, 6_000, 8_000)
        assert t.allowance(HAIKU, 0, 16_384) == pytest.approx(t.allowance(HAIKU, 0, 7_000))

    def test_never_under_the_prior_before_anything_is_seen_or_when_calls_write_little(self):
        t = _team()
        assert t.allowance(HAIKU, 0, 16_384) == pytest.approx(t.allowance(HAIKU, 0, team_mod.OUTPUT_ALLOWANCE))
        _calls(t, 50, 70)
        assert t.allowance(HAIKU, 0, 16_384) == pytest.approx(t.allowance(HAIKU, 0, team_mod.OUTPUT_ALLOWANCE))

    def test_never_over_the_cap_the_call_is_sent_with(self):
        t = _team()
        _calls(t, 9_000)
        assert t.allowance(HAIKU, 0, 2_000) == pytest.approx(_out_only(2_000))

    def test_a_spend_that_does_reach_the_cap_still_stops(self):
        t = _team(StepLimits(usd_left=0.20, cap_usd=0.20))
        _calls(t, *[16_000] * 3, tokens_in=40_000)   # about $0.36
        assert not t.may_call(HAIKU, 20_000, 16_384, lead=True)


def _out_only(tokens_out):
    from core.usage_tracker import calculate_cost
    return calculate_cost(HAIKU, 0, tokens_out)


class TestTheWords:
    def test_a_stop_on_a_projection_says_so_with_what_was_spent(self):
        t = _team(StepLimits(usd_left=0.20, cap_usd=0.20))
        _calls(t, 15_000, tokens_in=10_000)          # about $0.085
        t._stopped_by = "cap"
        line, no_answer = t.spend_stop()
        assert line == "Stopped: another call could pass this run's cap of $0.20 in model costs (spent so far $0.085)."
        assert no_answer["why"] == "run_cap"

    def test_a_stop_at_the_cap_keeps_its_words(self):
        t = _team(StepLimits(usd_left=0.0, cap_usd=0.20))
        _calls(t, 1_000, tokens_in=1_000)
        t._stopped_by = "cap"
        line, _ = t.spend_stop()
        assert line == "Stopped at this run's cap of $0.20 in model costs."

"""`synapse schedules create|update --missed` takes what the API takes: skip or run_immediately (F1730).

The flag offered `skip` and `run_once`, and sent its value as it stood. The API's `missed_run_policy` is
`run_immediately` or `skip`, so `--missed run_once` was answered 422 and the one value that changes anything could not
be set from the CLI at all. The flag now offers the API's two values, and `run_once` still works, as the
`run_immediately` it always meant, so a script written against the old help keeps running. Its help names only the
two.
"""
from __future__ import annotations

import json

import pytest


async def _made(synapse, *extra):
    code, out, err = await synapse("schedules", "create", "--orchestration", "weekly report", "--cron", "0 9 * * 1",
                                   "--prompt", "this week", "--paused", "--json", *extra)
    assert code == 0, err
    return json.loads(out)


class TestMissed:
    @pytest.mark.parametrize("value", ["skip", "run_immediately"])
    async def test_create_takes_the_apis_two_values(self, value, synapse, seed_orchestration, schedule_manager):
        await seed_orchestration(name="Weekly report")
        assert (await _made(synapse, "--missed", value))["missed_run_policy"] == value

    async def test_run_once_still_works_as_run_immediately(self, synapse, seed_orchestration, schedule_manager):
        await seed_orchestration(name="Weekly report")
        assert (await _made(synapse, "--missed", "run_once"))["missed_run_policy"] == "run_immediately"

    async def test_update_takes_it_too(self, synapse, seed_orchestration, schedule_manager):
        await seed_orchestration(name="Weekly report")
        made = await _made(synapse)
        code, out, err = await synapse("schedules", "update", made["id"], "--missed", "run_immediately", "--json")
        assert code == 0, err
        assert json.loads(out)["missed_run_policy"] == "run_immediately"

    async def test_anything_else_is_refused_naming_the_two(self, synapse, seed_orchestration, schedule_manager):
        await seed_orchestration(name="Weekly report")
        code, _, err = await synapse("schedules", "create", "--orchestration", "weekly report", "--cron", "0 9 * * 1",
                                     "--missed", "sometimes")
        assert code == 2
        assert "skip" in err and "run_immediately" in err and "run_once" not in err

    def test_the_help_names_the_two_and_not_the_old_word(self):
        import argparse

        from synapse.client import commands

        parser = argparse.ArgumentParser(prog="synapse")
        commands.register(parser.add_subparsers(dest="command"))
        for verb in ("create", "update"):
            sub = _sub(_sub(parser, "schedules"), verb)
            text = sub.format_help()
            assert "{skip,run_immediately}" in text and "run_once" not in text, text


def _sub(parser, name):
    import argparse

    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices[name]

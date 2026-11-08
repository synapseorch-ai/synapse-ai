"""F1473 (CodeQL 9913/9914): a Data Lab dataset's file is named from a slug of its topic.

The name was `dataset_{topic with spaces as _}_{time}.jsonl`, joined to the scratch directory as it came. A topic
with a `/` ("HR/payroll") named a directory that is not there, so every record failed and the job ended "No records
were generated". On Windows a `..\\` in the topic is a step up, so a topic could put the file outside the datasets
directory. Now everything but letters, digits, `_` and `-` becomes `_`."""
from __future__ import annotations

import json
import re

import pytest

from services import synthetic_data as dl

PAIR = json.dumps({"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]})


@pytest.fixture(autouse=True)
def _idle_job(monkeypatch):
    import core.llm_providers as llm

    async def generate_response(**kwargs):
        return PAIR

    monkeypatch.setattr(llm, "generate_response", generate_response)
    dl.current_job.update(status="idle", completed=0, total=0, file=None, error=None)
    yield
    dl.current_job.update(status="idle", completed=0, total=0, file=None, error=None)


def _datasets():
    from core.storage.scratch import scratch_dir
    return scratch_dir("datasets")


@pytest.mark.parametrize("topic,starts", [
    ("HR/payroll", "dataset_HR_payroll_"),
    ("Customer support", "dataset_Customer_support_"),
    ("Tâches d'été", "dataset_Tâches_d_été_"),
])
async def test_a_topic_names_its_dataset(topic, starts):
    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic=topic, count=1))

    assert dl.current_job["status"] == "completed", dl.current_job
    name = dl.current_job["file"]
    assert name.startswith(starts) and name.endswith(".jsonl"), name
    assert (_datasets() / name).is_file()


@pytest.mark.parametrize("topic", ["../../escape", "..\\..\\..\\escape", "/etc/escape", "C:\\escape", "a/./b"])
async def test_a_topic_that_names_a_path_stays_in_the_datasets_directory(topic):
    before = {p.name for p in _datasets().parent.iterdir()}

    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic=topic, count=1))

    name = dl.current_job["file"]
    assert not set(name) & {"/", "\\", ":"} and ".." not in name, name
    assert dl.current_job["status"] == "completed", dl.current_job
    assert (_datasets() / name).is_file()
    assert {p.name for p in _datasets().parent.iterdir()} == before


async def test_a_topic_of_only_punctuation_still_names_a_dataset():
    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic="../", count=1))

    assert re.fullmatch(r"dataset_\d{8}_\d{6}\.jsonl", dl.current_job["file"]), dl.current_job["file"]
    assert dl.current_job["status"] == "completed", dl.current_job


async def test_a_long_topic_is_cut():
    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic="word " * 100, count=1))

    assert dl.current_job["status"] == "completed", dl.current_job
    assert len(dl.current_job["file"]) <= len("dataset__20261108_000000.jsonl") + 60

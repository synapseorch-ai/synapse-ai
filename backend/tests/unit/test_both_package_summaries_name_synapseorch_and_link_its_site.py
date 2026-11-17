"""Both package summaries name who makes Synapse and link its site (F1652).

PyPI shows `pyproject.toml`'s `description` under the package's name, and npm shows `package.json`'s. Each said
"Synapse - A Multi Agent Orchestrator", naming neither who makes it nor where it lives, so a reader of either index had
no way to the site. Each now says the same line: Synapse by SynapseOrch, what it does, and the site's address in the
form every other place gives it, with its slash.

Near misses: the line is the same in both; a link to the site without its slash, or to another of its pages, is not the
site's address.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SITE = "https://synapseorch.com/"


def _pypi() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["description"]


def _npm() -> str:
    return json.loads((ROOT / "package.json").read_text("utf-8"))["description"]


def _links(text: str) -> list[str]:
    return re.findall(r"https?://(?:www\.)?synapseorch\.com\S*", text)


def test_pypis_summary_names_synapseorch_and_links_the_site():
    said = _pypi()
    assert "Synapse by SynapseOrch" in said and _links(said) == [SITE], said


def test_npms_summary_names_synapseorch_and_links_the_site():
    said = _npm()
    assert "Synapse by SynapseOrch" in said and _links(said) == [SITE], said


def test_both_say_the_same():
    assert _pypi() == _npm()


def test_an_address_without_its_slash_or_of_another_page_is_not_the_sites():
    assert _links("Synapse by SynapseOrch. https://synapseorch.com") != [SITE]
    assert _links("Synapse by SynapseOrch. https://synapseorch.com/docs/") != [SITE]
    assert _links("Synapse by SynapseOrch. https://synapseorch.com/") == [SITE]

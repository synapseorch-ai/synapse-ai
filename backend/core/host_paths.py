"""Text a person reads, without the host's filesystem paths (F1627).

A tool's error is carried into a failed step's reason and its last answer, which the run page shows. The Vault tools
said a missing file by its absolute path, so a run showed the host's user name and folder layout. `scrub` rewrites what
a person is shown: a path in the Vault becomes its Vault path (`reports/q1.md`, what every Vault tool reads back), the
run's scratch folder is named, and the home folder is `~`. Text with none of them is returned as it was.

Only person-facing text goes through it: a step's error and a failed step's answer. A tool's successful answer in run
state keeps another tool's absolute path, since a later step may need it; the Vault tools answer with Vault paths at
the source.
"""
from __future__ import annotations

import os
from pathlib import Path


def _forms(path: str | os.PathLike | None) -> list[str]:
    """A folder as written, and resolved (a symlinked home or temp folder is said either way), longest first."""
    if not path:
        return []
    forms = {str(path).rstrip("/")}
    try:
        forms.add(str(Path(path).resolve()).rstrip("/"))
    except OSError:
        pass
    return sorted((f for f in forms if f and f != "/"), key=len, reverse=True)


def _roots() -> list[tuple[str, str]]:
    """Each host folder with what it is said as, the most specific first: a path inside one is said by the first that
    holds it."""
    said: list[tuple[str, str]] = []
    try:
        from core.vault import _vault_root

        said += [(form + "/", "") for form in _forms(_vault_root())]
        said += [(form, "the Vault") for form in _forms(_vault_root())]
    except Exception:  # noqa: BLE001 — no Vault here is no Vault path to say
        pass
    scratch = os.environ.get("SYNAPSE_SCRATCH_DIR")
    said += [(form, "the run's scratch folder") for form in _forms(scratch)]
    synapse_home = os.environ.get("SYNAPSE_HOME")
    said += [(form, "~/.synapse") for form in _forms(synapse_home)]
    said += [(form, "~") for form in _forms(Path.home())]
    return sorted(said, key=lambda pair: len(pair[0]), reverse=True)


def scrub(text: str) -> str:
    """`text` with each host folder said as a person reads it; unchanged when it holds none."""
    if not isinstance(text, str) or "/" not in text and "\\" not in text:
        return text
    for root, as_said in _roots():
        if root in text:
            text = text.replace(root, as_said)
    return text

"""
Synapse's version numbers, as PyPI and npm each spell them (F826, F827).

PyPI orders versions by PEP 440: a dev release, then alpha, beta and rc, then
the release, then its post releases. npm spells the same version in semver
(`2.0.0rc1` on PyPI is `2.0.0-rc1` on npm). `synapse upgrade` compares with
`key`; the release workflow checks the tag, pyproject.toml and package.json
agree, and whether this is a pre-release, with `scripts/release_version.py`.
A release's tag is npm's spelling with a `v` (`v2.0.0-rc1`), and `install_command`
is the line that installs a release again (F1443).

Standard library only, and in the installer's package: the release script
imports this without the app, and synapse-install.pyz, which carries only
synapse/installer/, prints `install_command` when an install fails.
"""
from __future__ import annotations

import re

_VERSION = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>alpha|beta|rc|a|b|c)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?post[-_.]?(?P<post>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?$",
    re.I,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, "c": 2}
_PRE_PEP440 = {0: "a", 1: "b", 2: "rc"}
_PRE_NPM = {0: "alpha", 1: "beta", 2: "rc"}


def _parts(v: str):
    m = _VERSION.match((v or "").strip())
    if not m:
        return None
    pre = (_PRE_RANK[m["pre"].lower()], int(m["pre_n"] or 0)) if m["pre"] else None
    post = int(m["post"] or 0) if m["post"] is not None else None
    dev = int(m["dev"] or 0) if m["dev"] is not None else None
    return m["release"], pre, post, dev


def key(v: str) -> tuple:
    """A sort key that orders versions as PyPI does. `2.0.0rc1`, `2.0.0-rc.1`
    and `v2.0.0rc1` are one version; anything unreadable sorts below every real one."""
    parts = _parts(v)
    if parts is None:
        return ((), (-1,), -1, -1)
    release_text, pre, post, dev = parts
    release = [int(x) for x in release_text.split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if pre is not None:
        pre_key = (0, *pre)
    elif dev is not None and post is None:
        pre_key = (-1,)          # 2.0.0.dev1 comes before 2.0.0a1
    else:
        pre_key = (1,)
    return (
        tuple(release),
        pre_key,
        post if post is not None else -1,
        dev if dev is not None else float("inf"),
    )


def pep440(v: str) -> str:
    """The version as PyPI normalises it: `v2.0.0-rc.1` → `2.0.0rc1`."""
    parts = _parts(v)
    if parts is None:
        return v
    release, pre, post, dev = parts
    out = release
    if pre is not None:
        out += f"{_PRE_PEP440[pre[0]]}{pre[1]}"
    if post is not None:
        out += f".post{post}"
    if dev is not None:
        out += f".dev{dev}"
    return out


def npm(v: str) -> str:
    """The same version as npm spells it (semver): `2.0.0rc1` → `2.0.0-rc1`."""
    parts = _parts(v)
    if parts is None:
        return v
    release, pre, post, dev = parts
    out = release
    if pre is not None:
        out += f"-{_PRE_NPM[pre[0]]}{pre[1]}"
    if post is not None:
        out += f"-post{post}"
    if dev is not None:
        out += f"-dev{dev}"
    return out


def is_prerelease(v: str) -> bool:
    """An alpha, beta, rc or dev release — what pip and npm users get only by asking."""
    parts = _parts(v)
    return parts is not None and (parts[1] is not None or parts[3] is not None)


#: Where one release's setup scripts are: the site's short address, which redirects to that release's files.
_RELEASE_SETUP = "https://synapseorch.com/install"
#: The newest final release's setup scripts.
_LATEST_SETUP = "https://github.com/synapseorch-ai/synapse-ai/releases/latest/download"


def tag(v: str) -> str:
    """The release's git tag: npm's spelling with a `v`, as v2.0.0-rc1 was published and as the
    site's short install address takes it (`2.0.0rc2` → `v2.0.0-rc2`, `2.0.1` → `v2.0.1`)."""
    return "v" + npm(v)


def install_command(v: str, windows: bool = False) -> str:
    """The one line that installs release `v` again. A pre-release from its own release:
    releases/latest is the newest final, which has no setup files of an rc's (F1443).
    A final, or no version, from the latest."""
    if v and is_prerelease(v):
        name = tag(v)
        return (f"irm {_RELEASE_SETUP}/{name}.ps1 | iex" if windows
                else f"curl -LsSf {_RELEASE_SETUP}/{name}.sh | bash")
    return f"irm {_LATEST_SETUP}/setup.ps1 | iex" if windows else f"curl -LsSf {_LATEST_SETUP}/setup.sh | bash"

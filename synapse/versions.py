"""
Synapse's version numbers, as PyPI and npm each spell them (F826, F827).

PyPI orders versions by PEP 440: a dev release, then alpha, beta and rc, then
the release, then its post releases. npm spells the same version in semver
(`2.0.0rc1` on PyPI is `2.0.0-rc1` on npm). `synapse upgrade` compares with
`key`; the release workflow checks the tag, pyproject.toml and package.json
agree, and whether this is a pre-release, with `scripts/release_version.py`.

Standard library only: the release script imports this without the app.
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

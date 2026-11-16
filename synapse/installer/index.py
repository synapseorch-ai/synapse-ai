"""
Whether the package index an install reads serves a version yet (F1638).

`synapse upgrade` took its target from PyPI's JSON page, while uv and pip
install from PyPI's simple index. The two are cached apart, and for some minutes
after a release they disagree: an upgrade announced the new version, stopped
Synapse, backed up the database, then failed with the resolver's words. So the
target is read from the index the install itself reads (its PEP 691 JSON,
`versions`), and before anything is stopped an index that answered without the
version is said plainly instead.

The index is the one uv or pip would use, read from their environment variables
(`UV_DEFAULT_INDEX` or `UV_INDEX_URL`; `PIP_INDEX_URL`), else PyPI. Where the
install could find the package somewhere this cannot see (find-links, an extra
index, or an index named in a uv.toml, a pyproject's [tool.uv] or a pip
configuration file), `simple_index` says None and nothing here stands in the
way: the upgrade goes on as it did. An index that does not answer in PEP 691
JSON (HTML only, a 404, a timeout) is the same: only an index that answered,
without the version, is ever said to be still catching up.

Standard library only, like the rest of synapse/installer/: the release's
installer (synapse-install.pyz) carries this too.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from . import net, versions

PACKAGE = "synapse-orch-ai"
PYPI_SIMPLE = "https://pypi.org/simple"
NPM_REGISTRY = f"https://registry.npmjs.org/{PACKAGE}"
#: PEP 691's JSON form of a simple index page.
_PEP691 = "application/vnd.pypi.simple.v1+json"

#: Where the install could find the package beside the index: the check cannot see there, so it stands aside.
_ELSEWHERE = {"uv": ("UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS"),
              "pip": ("PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS")}


def _read(url: str, headers: dict | None = None, timeout: float = 15):
    """The JSON at `url` (a seam: tests answer for the index)."""
    return net.get_json(url, headers=headers, timeout=timeout)


def _read_npm():
    """The npm registry's document for the package, or None when it does not answer."""
    try:
        return net.get_json(NPM_REGISTRY, timeout=15)
    except net.HttpError:
        return None


def _names_an_index(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "index" in text.lower()


def _uv_config_files(env: Mapping[str, str], cwd: Path | None) -> list[Path]:
    """The configuration files uv reads for a command run in `cwd`: its own file, the user's and the system's, and a
    uv.toml or pyproject.toml in `cwd` or any folder above it."""
    found: list[Path] = []
    if env.get("UV_CONFIG_FILE"):
        found.append(Path(env["UV_CONFIG_FILE"]))
    if sys.platform == "win32":
        base = env.get("APPDATA")
        if base:
            found.append(Path(base) / "uv" / "uv.toml")
        program_data = env.get("PROGRAMDATA")
        if program_data:
            found.append(Path(program_data) / "uv" / "uv.toml")
    else:
        config = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
        found.append(Path(config) / "uv" / "uv.toml")
        found += [Path(d) / "uv" / "uv.toml" for d in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
        found.append(Path("/etc/uv/uv.toml"))
    if cwd is not None:
        for folder in (Path(cwd), *Path(cwd).parents):
            found += [folder / "uv.toml", folder / "pyproject.toml"]
    return found


def _pip_config_files(env: Mapping[str, str]) -> list[Path]:
    found: list[Path] = []
    if env.get("PIP_CONFIG_FILE"):
        found.append(Path(env["PIP_CONFIG_FILE"]))
    home = Path(env.get("HOME") or Path.home())
    if sys.platform == "win32":
        base = env.get("APPDATA")
        if base:
            found.append(Path(base) / "pip" / "pip.ini")
        found.append(home / "pip" / "pip.ini")
    else:
        config = env.get("XDG_CONFIG_HOME") or str(home / ".config")
        found += [Path(config) / "pip" / "pip.conf", home / ".pip" / "pip.conf", Path("/etc/pip.conf")]
        found += [Path(d) / "pip" / "pip.conf" for d in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
    venv = env.get("VIRTUAL_ENV") or sys.prefix
    found.append(Path(venv) / ("pip.ini" if sys.platform == "win32" else "pip.conf"))
    return found


def simple_index(method: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str | None:
    """The simple index the install will read (no trailing slash), or None where it could find the package somewhere
    this cannot see. `method` is "uv" (the installer, and an npm install's) or "pip"."""
    env = os.environ if env is None else env
    tool = "pip" if method == "pip" else "uv"
    if any(env.get(var) for var in _ELSEWHERE[tool]):
        return None
    if tool == "uv":
        if any(f.is_file() and _names_an_index(f) for f in _uv_config_files(env, cwd)):
            return None
        chosen = env.get("UV_DEFAULT_INDEX") or env.get("UV_INDEX_URL")
    else:
        if any(f.is_file() and _names_an_index(f) for f in _pip_config_files(env)):
            return None
        chosen = env.get("PIP_INDEX_URL")
    return (chosen or PYPI_SIMPLE).strip().rstrip("/")


def listed(index_url: str) -> list[str] | None:
    """The versions `index_url` lists for the package, or None when it does not answer in PEP 691 JSON."""
    try:
        page = _read(f"{index_url.rstrip('/')}/{PACKAGE}/", headers={"Accept": _PEP691}, timeout=15)
    except (net.HttpError, OSError, ValueError):
        return None
    found = page.get("versions") if isinstance(page, dict) else None
    if not isinstance(found, list):
        return None
    return [str(v) for v in found]


def newest_final(found: list[str]) -> str | None:
    """The newest final release in `found`, compared as versions (2.0.16 is newer than 2.0.9)."""
    finals = [v for v in found if not versions.is_prerelease(v) and versions.key(v)[0]]
    return max(finals, key=versions.key) if finals else None


def serves(version: str, method: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> bool | None:
    """Whether the install's index lists `version` (any spelling of it); None when that cannot be told."""
    index = simple_index(method, env, cwd)
    if index is None:
        return None
    found = listed(index)
    if found is None:
        return None
    wanted = versions.key(version)
    return any(versions.key(v) == wanted for v in found)


def where(method: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """The index as a person says it: PyPI, or their own."""
    index = simple_index(method, env, cwd)
    return "PyPI" if index in (None, PYPI_SIMPLE) else "your package index"


def npm_has(version: str) -> bool | None:
    """Whether the npm registry has `version` (npm's spelling); None when it does not answer."""
    doc = _read_npm()
    found = doc.get("versions") if isinstance(doc, dict) else None
    if not isinstance(found, dict):
        return None
    wanted = versions.key(version)
    return any(versions.key(v) == wanted for v in found)


def still_reaching(version: str, place: str) -> str:
    """What is said when `place` has not caught up with `version` yet: plain, and that nothing changed."""
    return f"Synapse {version} is still reaching {place}. Nothing was changed; try again in a few minutes."

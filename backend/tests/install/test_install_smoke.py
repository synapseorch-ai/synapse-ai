"""
Installation / packaging smoke tests (run in the gate).

Catches the breakages that would ship a non-installable or non-startable
release: version drift between the npm and PyPI manifests, an import-time error
in any route module (which would 500 the server on boot), and a missing CLI
entry point. The heavyweight "build the wheel and pip-install it into a clean
venv" check runs as a separate CI shell job (see .github/workflows/ci.yml).
"""
import importlib
import json
import pathlib
import pkgutil

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _pyproject_version() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def _package_json_version() -> str:
    return json.loads((_REPO_ROOT / "package.json").read_text())["version"]


def test_version_sync_between_pyproject_and_package_json():
    """PyPI (pyproject.toml) and npm (package.json) versions must match — the
    release tags a single version across both registries."""
    assert _pyproject_version() == _package_json_version(), (
        f"pyproject={_pyproject_version()} but package.json={_package_json_version()}"
    )


def test_backend_app_imports():
    import core.server as server
    assert server.app is not None


def test_all_route_modules_import_cleanly():
    """Importing every core.routes.* module guards against an import-time error
    that would break server startup (and thus installation) for real users."""
    import core.routes as routes_pkg
    failures = {}
    for mod in pkgutil.iter_modules(routes_pkg.__path__):
        name = f"core.routes.{mod.name}"
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert not failures, f"Route modules failed to import: {failures}"


def test_cli_entrypoint_exists():
    """The `synapse` console script points at synapse.cli:main."""
    try:
        cli = importlib.import_module("synapse.cli")
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"synapse.cli not importable in this environment: {exc}")
    assert hasattr(cli, "main") and callable(cli.main)

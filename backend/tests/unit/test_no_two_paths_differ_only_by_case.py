"""No two paths in the repo differ only by case, and no two web modules in one folder do (F1528).

macOS and Windows match file names regardless of case. frontend/src/components/run/ held `WorkflowList.tsx` (the
list) beside `workflowList.ts` (its words and order). On Linux `./WorkflowList` can only be the .tsx, but on a disk
that ignores case `workflowList.ts` answers to it too, and TypeScript tries `.ts` before `.tsx`. So the import read the
words file and the type check failed ("'./WorkflowList' has no exported member named 'WorkflowList'"): the nightly's
install-e2e on macOS and Windows failed there once 2.0 reached master, while Linux, where every release is built,
never saw it. ScheduleList, LogList and ScheduleSwitch had the same twin; each words file now has a name of its own.
"""
from __future__ import annotations

import subprocess
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
#: What an import may leave off a web module's file name: `./WorkflowList` is WorkflowList.ts, .tsx, .js and the rest.
#: `.d.ts` comes before `.ts`, so `types.d.ts` is the module `types`.
_MODULE_SUFFIXES = (".d.ts", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _module(name: str) -> str | None:
    return next((name[: -len(suffix)] for suffix in _MODULE_SUFFIXES if name.endswith(suffix)), None)


def twins(paths: Iterable[str]) -> list[str]:
    """Each set of names one folder holds that a disk which ignores case takes for one: files or folders equal but
    for case, and web modules an import names alike but for case (a file without its extension, or a folder by its
    index file). A module in two extensions (`a.ts` and `a.tsx`) is no case twin and is left alone."""
    seen: dict[tuple[str, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in paths:
        parts = path.split("/")
        for depth, name in enumerate(parts):
            seen["path", "/".join(parts[:depth]), name.lower()][name].add(name)
        folder, name = "/".join(parts[:-1]), parts[-1]
        module = _module(name)
        if module is None:
            continue
        seen["module", folder, module.lower()][module].add(name)
        if module == "index" and folder:
            above, _, own = folder.rpartition("/")
            seen["module", above, own.lower()][own].add(own + "/")
    return sorted({f"{folder or '.'}: " + " | ".join(sorted(n for names in by_name.values() for n in names))
                   for (_kind, folder, _lowered), by_name in seen.items() if len(by_name) > 1})


# Folders a walk of an unpacked tree skips: what .gitignore keeps out of the repo, so a walk sees what git would.
_NOT_SOURCE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "var", "data", "coverage", "out"}


def _files() -> list[str]:
    """The repo's files as git holds them, with the new ones it does not ignore yet. A tree with no git (an
    archive, an unpacked sdist, the private CI's checkout of one) is walked instead, minus what git ignores, so the
    guard holds wherever the source is (F1532)."""
    if (ROOT / ".git").exists():
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard"], capture_output=True, check=True)
        names = listed.stdout.decode("utf-8", "surrogateescape").split("\0")
        return [path for path in names if path and (ROOT / path).exists()]
    found: list[str] = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if any(part in _NOT_SOURCE for part in parts) or not path.is_file():
            continue
        found.append("/".join(parts))
    return found


def test_the_guard_reads_a_tree_with_no_git(tmp_path, monkeypatch):
    """F1532: the full suite runs on an archive of the source, where `git ls-files` has no repo to read."""
    (tmp_path / "frontend" / "run").mkdir(parents=True)
    (tmp_path / "frontend" / "run" / "LogList.tsx").write_text("")
    (tmp_path / "frontend" / "run" / "logList.ts").write_text("")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "A.js").write_text("")
    (tmp_path / "node_modules" / "x" / "a.js").write_text("")
    monkeypatch.setattr(__import__(__name__), "ROOT", tmp_path)
    files = _files()
    assert "frontend/run/LogList.tsx" in files and "frontend/run/logList.ts" in files
    assert not any(f.startswith("node_modules/") for f in files)
    assert twins(files) == ["frontend/run: LogList.tsx | logList.ts"]


def test_no_two_names_in_one_folder_differ_only_by_case():
    files = _files()
    assert any(path.startswith("frontend/src/components/run/") for path in files)
    assert not twins(files), "\n".join(twins(files))


def test_the_check_sees_the_twins_that_broke_the_mac_build():
    run = "frontend/src/components/run"
    assert twins([f"{run}/WorkflowList.tsx", f"{run}/workflowList.ts", f"{run}/index.ts"]) == [
        f"{run}: WorkflowList.tsx | workflowList.ts"]


def test_the_check_sees_files_folders_and_a_folder_module_alike_but_for_case():
    assert twins(["docs/README.md", "docs/Readme.md"]) == ["docs: README.md | Readme.md"]
    assert twins(["a/Models/x.py", "a/models/y.py"]) == ["a: Models | models"]
    assert twins(["ui/Button/index.tsx", "ui/button.ts"]) == ["ui: Button/ | button.ts"]
    assert twins(["types.d.ts", "Types.ts"]) == [".: Types.ts | types.d.ts"]


def test_names_that_differ_by_more_than_case_are_left_alone():
    run = "frontend/src/components/run"
    assert twins([f"{run}/WorkflowList.tsx", f"{run}/workflowRows.ts"]) == []
    assert twins(["a/plan.ts", "a/plan.tsx"]) == []          # one module in two extensions: not a case twin
    assert twins(["a/Run.ts", "a/run/step.ts"]) == []        # a folder with no index is not the module `run`
    assert twins(["a/Foo.py", "a/foo.ts"]) == []             # a Python file is not a web module

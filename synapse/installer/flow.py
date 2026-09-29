"""
The install, as the user sees it: named steps, a few questions, then one link.

Three shapes, one screen:

* **fresh** — Getting Python → Installing Synapse → Node.js → Browser →
  Setting up → Starting. The questions are asked while the first four run.
* **from 1.x** — the same, after "Stopping Synapse 1.x" and "Backing up", with
  "Moving your data" before setting up and "Removing the old install" last.
  Nothing is asked that the old install already answered.
* **2.x upgrade** — Stopping → Backing up the database → Installing →
  Updating the database → Starting (only if it was running).

Everything a command prints goes to `<home>/logs/install.log`. The screen shows
step names, bars and one line at the end.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from . import autostart, backup, home as home_mod, legacy, ports, providers, runtime
from .progress import Downloads, UvInstall
from .runner import CommandFailed, Log, kill_children, run
from .screen import make_screen
from .tui import Term
from .widgets import Asker, Cancelled

PACKAGE = "synapse-orch-ai"
PYTHON = "3.12"
IS_WIN = sys.platform == "win32"
RETRY = {
    "posix": "curl -LsSf https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.sh | bash",
    "nt": "irm https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.ps1 | iex",
}


@dataclass
class Options:
    yes: bool = False             # ask nothing; take the defaults
    no_start: bool = False
    no_browser: bool = False
    version: str = ""             # install exactly this version
    package: str = ""             # a wheel path or requirement (SYNAPSE_PACKAGE), for testing
    method: str = "uv"            # "uv", or "npm" when the npm wrapper runs us
    uv: str = ""
    legacy_root: str = ""         # a v1.x script install handing over (see handoff.py)
    carrier: str = ""             # a source tree that only brought this installer (setup.py stub)
    upgrade: bool = False         # `synapse upgrade`: restart only if it was running
    home: Path | None = None


class Failed(Exception):
    """A step failed; `message` is the plain sentence for the user."""

    def __init__(self, step: str, message: str, tail: list[str] | None = None):
        super().__init__(message)
        self.step, self.message, self.tail = step, message, tail or []


@dataclass
class State:
    answers: dict = field(default_factory=dict)
    autostart: bool = False
    tool_python: Path | None = None
    tool_bin: Path | None = None
    node: tuple | None = None
    backend_port: int = 8765
    frontend_port: int = 3000
    backup: Path | None = None
    migrated: bool = False
    was_running: bool = False
    link: str = ""
    path_changed: bool = False


class Installer:
    def __init__(self, options: Options, term: Term | None = None):
        self.o = options
        self.home = options.home or home_mod.home_dir()
        self.term = term or Term()
        self.log = Log(self.home / "logs" / "install.log")
        self.s = State()
        self.legacy = legacy.Migration()
        self.existing = home_mod.read_install(self.home)
        self.uv: Path | None = None
        self.target = ""               # the requirement uv installs
        self.version = ""
        self._errors: list[Failed] = []
        self._node_ready = threading.Event()
        self._failed = threading.Event()     # ends any open question
        self.screen = None
        self.asker: Asker | None = None

    # ── entry ──────────────────────────────────────────────────────────────
    def run(self) -> int:
        try:
            with home_mod.InstallLock(self.home):
                return self._run()
        except home_mod.Busy as busy:
            self.term.write(f"Another Synapse installer is running (pid {busy.args[0]}). "
                            "Let it finish, then try again.\n")
            return 1
        except KeyboardInterrupt:
            kill_children()
            self._close(["", "  Install stopped. " + self._safe_line(), ""])
            return 130
        finally:
            self.term.close()
            self.log.close()

    def _run(self) -> int:
        self._resolve_target()
        tool_root = self._tool_root_guess()
        self.legacy = legacy.find(self.home, tool_root)
        if self.o.legacy_root:
            self._include_legacy_root(Path(self.o.legacy_root))
        mode = "legacy" if self.legacy.data_dirs or self.legacy.installs else (
            "upgrade" if self.existing.get("version") else "fresh")
        self.log.write(f"mode={mode} target={self.target} home={self.home}")

        title = f"Synapse {self.version}".strip()
        if mode == "legacy":
            title = f"Synapse {self.legacy.version} {self.term.g('arrow')} {self.version}"
        elif mode == "upgrade" and self.existing.get("version") != self.version:
            title = f"Synapse {self.existing.get('version')} {self.term.g('arrow')} {self.version}"

        self.screen = make_screen(self.term, title)
        self.asker = Asker(self.screen, None if self.o.yes else self.term.keys(), self._failed)
        self.screen.open()
        if mode == "legacy" and not self._confirm_legacy():
            self._close(["", "  Nothing was changed.", ""])
            return 0

        self._plan(mode)
        try:
            if mode == "legacy":
                self._step("stop", self._stop_legacy)
                self._step("backup", self._backup_legacy)
            elif mode == "upgrade":
                self._step("stop", self._stop_current)
                self._step("backup", self._backup_database)
            self._pick_ports()

            workers = self._start_background()
            try:
                self._ask(mode)
            except Cancelled:
                pass     # a step failed; its error is raised below
            for w in workers:
                w.join()
            if self._errors:
                raise self._errors[0]

            if mode == "legacy":
                self._step("migrate", self._migrate_legacy)
            if mode == "upgrade":
                self._step("setup", self._update_database)
            else:
                self._step("setup", self._apply_settings)
            start = not self.o.no_start and (mode != "upgrade" or not self.o.upgrade or self.s.was_running)
            if start:
                self._step("start", self._start)
            else:
                self.screen.skip("start")
            if mode == "legacy":
                self._step("cleanup", self._cleanup_legacy)
            self._record(mode)
            self._remove_carrier()
        except Failed as failure:
            kill_children()
            self._close(self._failure_lines(failure))
            return 1
        self._close(self._final_lines(start))
        return 0

    # ── what runs, in what order ───────────────────────────────────────────
    def _plan(self, mode: str) -> None:
        add = self.screen.add
        if mode == "legacy":
            add("stop", f"Stopping Synapse {self.legacy.version}")
            add("backup", "Backing up")
        elif mode == "upgrade":
            add("stop", "Stopping Synapse")
            add("backup", "Backing up the database")
        add("python", "Getting Python")
        add("install", "Installing Synapse")
        add("node", "Node.js")
        add("browser", "Browser")
        if mode == "legacy":
            add("migrate", "Moving your data")
        add("setup", "Updating the database" if mode == "upgrade" else "Setting up")
        add("start", "Starting")
        if mode == "legacy":
            add("cleanup", "Removing the old install")

    def _step(self, key: str, fn) -> None:
        self.screen.start(key)
        try:
            detail = fn()
        except Failed:
            self.screen.fail(key)
            raise
        except CommandFailed as exc:
            self.screen.fail(key)
            raise Failed(key, _plain(key, str(exc)), exc.tail) from exc
        except (runtime.RuntimeError_, backup.BackupError) as exc:
            self.screen.fail(key)
            raise Failed(key, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — the screen must say something
            self.log.write(f"{key} failed: {type(exc).__name__}: {exc}")
            self.screen.fail(key)
            raise Failed(key, f"{type(exc).__name__}: {exc}") from exc
        if detail == "skip":
            self.screen.skip(key)
        else:
            self.screen.done(key, detail or "")

    def _start_background(self) -> list[threading.Thread]:
        def guarded(fn):
            def body():
                try:
                    fn()
                except Failed as failure:
                    self._errors.append(failure)
                    self._failed.set()
            return body

        def python_install_browser():
            self._step("python", self._python)
            self._step("install", self._install)
            self._node_ready.wait()
            if not self._errors:
                self._browser_step()

        def node():
            try:
                self._step("node", self._node)
            finally:
                self._node_ready.set()

        workers = [threading.Thread(target=guarded(python_install_browser), daemon=True),
                   threading.Thread(target=guarded(node), daemon=True)]
        for w in workers:
            w.start()
        return workers

    # ── questions ──────────────────────────────────────────────────────────
    def _confirm_legacy(self) -> bool:
        found = legacy.summary(self.legacy.counts)
        dot = f" {self.term.g('dot')} "
        self.log.write(f"found Synapse {self.legacy.version} at {self.legacy.where}: {', '.join(found)}")
        note = "\n".join([
            dot.join(found) if found else "No agents or workflows in it yet.",
            f"A backup is made first. 2.0 keeps everything in {_tilde(self.home)},",
            "and the old install is removed once 2.0 is running.",
        ])
        return self.asker.confirm(f"Found Synapse {self.legacy.version} in {_tilde(self.legacy.where)}",
                                  note=note, yes="Upgrade", no="Not now")

    def _ask(self, mode: str) -> None:
        if mode == "upgrade":
            return
        if mode == "legacy" and self.legacy.has_model():
            self.asker.say(["Your model and keys come across with your data — nothing to ask."])
            nothing_asked = True
        else:
            self.s.answers = providers.ask(self.asker)
            nothing_asked = False
        if mode == "legacy":
            # v1.x asked this already; its answer is whether it left an entry.
            self.s.autostart = legacy.had_autostart()
        elif self.o.method != "npm" and autostart.supported():
            self.s.autostart = self.asker.confirm("Start Synapse when you log in?", default=False)
        background = ("python", "install", "node", "browser")
        if self.asker.interactive and not nothing_asked and any(
                s.key in background and s.state in ("pending", "running") for s in self.screen.steps):
            self.asker.say(["All set — finishing the install…"])

    # ── the steps ──────────────────────────────────────────────────────────
    def _resolve_target(self) -> None:
        package = self.o.package or os.environ.get("SYNAPSE_PACKAGE", "")
        if package:
            self.target = package
            self.version = runtime.wheel_version(package) or self.o.version or ""
        else:
            from . import BUILT_FOR
            self.version = self.o.version or BUILT_FOR or _checkout_version()
            self.target = f"{PACKAGE}=={self.version}" if self.version else PACKAGE

    def _tool_root_guess(self) -> Path | None:
        python = self.existing.get("tool_python")
        return Path(python).parent.parent if python else None

    def _include_legacy_root(self, root: Path) -> None:
        """The v1.x install handing over (`handoff.py`), wherever it lives."""
        install = legacy._script_install(root)
        if install is None or any(i.root == root for i in self.legacy.installs):
            return
        self.legacy.installs.insert(0, install)
        self.legacy.leftovers.append(root)
        self.legacy.env_files.insert(0, root / ".env")
        data, logs = root / "backend" / "data", root / "backend" / "logs"
        if legacy.has_data(data):
            self.legacy.data_dirs.insert(0, data)
            self.legacy.pid_files += [data / "backend.pid", data / "frontend.pid"]
        if legacy.has_logs(logs):
            self.legacy.logs_dirs.insert(0, logs)
        self.legacy.counts = legacy.count(self.legacy.data_dirs, self.legacy.logs_dirs)

    def _stop_legacy(self) -> str:
        stopped = legacy.stop_services(self.legacy, self.log)
        return f"stopped {len(stopped)} process{'es' if len(stopped) != 1 else ''}" if stopped else "not running"

    def _backup_legacy(self) -> str:
        sources: dict[str, Path] = {}
        for i, d in enumerate(self.legacy.data_dirs):
            sources[f"data-{i + 1}"] = d
        for i, d in enumerate(self.legacy.logs_dirs):
            sources[f"logs-{i + 1}"] = d
        for i, f in enumerate(self.legacy.env_files):
            sources[f"env-{i + 1}/.env"] = f
        self.s.backup = backup.backup_folders(
            sources, self.home / "backups", self.legacy.version,
            lambda f: self.screen.progress("backup", f))
        return _tilde(self.s.backup)

    def _stop_current(self) -> str:
        status = self._cli_json(["status", "--json"], old=True)
        self.s.was_running = bool(status and (status.get("backend") or {}).get("running"))
        if self.s.was_running:
            self._cli(["stop"], old=True)
            return "stopped"
        return "not running"

    def _backup_database(self) -> str:
        db = self.home / "var" / "synapse.db"
        if not db.is_file():
            return "skip"
        self.s.backup = backup.backup_sqlite(db, self.home / "backups", self.existing.get("version") or "2")
        return _tilde(self.s.backup)

    def _pick_ports(self) -> None:
        wanted_b = _int(self.legacy.settings.get("backend_port")) or 8765
        wanted_f = _int(self.legacy.settings.get("frontend_port")) or 3000
        if self.existing.get("version") and not self.legacy.found:
            return   # a 2.x install keeps the ports it has in its settings
        self.s.backend_port, self.s.frontend_port = ports.pick(wanted_b, wanted_f)

    def _python(self) -> str:
        self.uv = self._find_uv()
        _, out = run([self.uv, "python", "install", PYTHON], self.log, env=self._uv_env())
        found = re.search(r"Installed Python (\S+)", out)
        return found.group(1) if found else PYTHON

    def _install(self) -> str:
        parser = UvInstall()
        cmd = [self.uv, "tool", "install", "--force", "--managed-python", "--python", PYTHON]
        constraints = _constraints_file(self.home)
        if constraints:
            cmd += ["-c", constraints]
        cmd.append(self.target)
        run(cmd, self.log, env=self._uv_env(),
            on_line=lambda line: self.screen.progress("install", parser.feed(line)))
        tool_dir = Path(run([self.uv, "tool", "dir"], self.log, env=self._uv_env())[1].strip().splitlines()[-1])
        bin_dir = Path(run([self.uv, "tool", "dir", "--bin"], self.log, env=self._uv_env())[1].strip().splitlines()[-1])
        venv = tool_dir / PACKAGE
        self.s.tool_python = venv / ("Scripts/python.exe" if IS_WIN else "bin/python")
        self.s.tool_bin = bin_dir
        installed = self._cli(["--version"]).strip().split()[-1:] or [self.version]
        self.version = self.version or installed[0]
        return installed[0]

    def _node(self) -> str:
        found = runtime.find_node(self.home)
        if found:
            node, version, source = found
            self.s.node = (node, source)
            return runtime.version_text(version) + (" (already here)" if source == "system" else "")
        node, version = runtime.install_node(self.home, lambda f: self.screen.progress("node", f))
        self.s.node = (node, "private")
        return runtime.version_text(version)

    def _browser_step(self) -> None:
        # Not needed to start Synapse — only its browser tools use it — so a
        # failure is noted and left for `synapse start` to retry.
        self.screen.start("browser")
        try:
            detail = self._browser()
            self.screen.done("browser", detail)
        except (CommandFailed, OSError) as exc:
            self.log.write(f"browser: {exc}")
            self.screen.skip("browser", "not now — Synapse retries when it starts")

    def _browser(self) -> str:
        path = _playwright_dir()
        present = {p.name.split("-")[0] for p in path.iterdir()} if path.is_dir() else set()
        env = self._runtime_env()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(path)
        parts = Downloads(parts=3)
        if "chromium" not in present:
            run([self.s.tool_python, "-m", "playwright", "install", "chromium"], self.log, env=env,
                on_line=lambda line: self.screen.progress("browser", parts.feed(line) * 0.6))
        if not any(p.startswith("mcp") for p in present):
            npx = shutil.which("npx.cmd" if IS_WIN else "npx", path=env["PATH"]) or "npx"
            later = Downloads(parts=1)
            run([npx, "-y", "@playwright/mcp", "install-browser", "chrome-for-testing"], self.log, env=env,
                on_line=lambda line: self.screen.progress("browser", 0.6 + later.feed(line) * 0.4))
        self.s.answers.setdefault("playwright_browsers_path", str(path))
        return "Chromium"

    def _migrate_legacy(self) -> str:
        # The secrets first, so existing sessions and links keep working.
        merged = legacy.merged_env(self.legacy.env_files)
        if merged:
            _merge_env_file(self.home / ".env", merged)
        args = ["migrate", "--json"]
        for d in self.legacy.data_dirs:
            args += ["--from", str(d)]
        for d in self.legacy.logs_dirs:
            args += ["--logs", str(d)]
        report = self._cli_json(args)
        if not report or report.get("database") != "ok":
            raise Failed("migrate", "the database could not be opened: "
                         + str((report or {}).get("error", "no report")))
        failed = [r for r in report["sources"] + report["logs"] if "error" in r]
        if failed:
            raise Failed("migrate", f"{failed[0]['path']} could not be imported: {failed[0]['error']}")
        self.s.migrated = True
        totals = report.get("totals", {})
        moved = [f"{totals[k]} {name}" for k, name in (("user_agents", "agents"), ("orchestrations", "orchestrations"),
                                                         ("chat_sessions", "chats")) if totals.get(k)]
        return ", ".join(moved) or "done"

    def _apply_settings(self) -> str:
        settings = dict(self.s.answers)
        settings.update(backend_port=self.s.backend_port, frontend_port=self.s.frontend_port,
                        start_on_boot=self.s.autostart)
        if not self.existing.get("installed_at") and not self.legacy.settings.get("installed_at"):
            settings["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = json.dumps({"settings": settings, "default_agent": True})
        self._cli(["setup", "--apply", "-"], stdin_text=payload)
        return providers.describe(self.s.answers) or "choose an AI later in Settings"

    def _update_database(self) -> str:
        report = self._cli_json(["migrate", "--json"])
        if not report or report.get("database") != "ok":
            raise Failed("setup", "the database could not be updated: "
                         + str((report or {}).get("error", "no report")))
        return "up to date"

    def _start(self) -> str:
        if self.s.autostart:
            path_dirs = [d for d in (self._runtime_bin(),) if d]
            autostart.enable(self.s.tool_python, self.home, path_dirs)
        self._cli(["start", "--detach", "--no-browser"])
        status = self._cli_json(["status", "--json"]) or {}
        if not status.get("ready"):
            raise Failed("start", "Synapse started but does not answer yet. "
                         "See what it says with: synapse logs")
        self.s.link = status.get("link") or status.get("url", "")
        if not self.o.no_browser:
            _open(self.s.link)
        return ""

    def _cleanup_legacy(self) -> str:
        bins = [i.bin_dir for i in self.legacy.installs if i.bin_dir]
        if bins:
            self.s.path_changed = bool(legacy.remove_path_lines(bins)) or self.s.path_changed
        pending = []
        for path in self.legacy.leftovers:
            if not path.exists():
                continue
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError as exc:
                self.log.write(f"could not remove {path} now: {exc}")
                pending.append(str(path))
        for install in self.legacy.installs:
            if install.kind == "pip":
                self._remove_pip_copy(install)
        if pending:
            home_mod.write_install(self.home, pending_cleanup=pending)
        return "done" if not pending else "finishes at the next start"

    def _remove_carrier(self) -> None:
        """The source tree an old setup.sh unpacked only to run setup.py (the stub).

        Everything it held is now installed properly; left behind, its bin/
        (which that old setup.sh puts on PATH) would offer a `synapse` with no
        venv behind it.
        """
        carrier = Path(self.o.carrier) if self.o.carrier else None
        if not carrier or not carrier.is_dir() or (carrier / ".git").exists():
            return
        if not (carrier / "synapse" / "installer").is_dir():
            return   # not a Synapse source tree: not ours to remove
        shutil.rmtree(carrier, ignore_errors=True)
        self.log.write(f"removed the source tree the installer came in: {carrier}")

    def _remove_pip_copy(self, install) -> None:
        """A 1.x pip copy whose `synapse` sits where uv's shim now is.

        pip lists that file in its RECORD, so a later `pip uninstall` would
        delete uv's shim; uninstalling now, and putting the shim back, avoids
        it. A copy elsewhere (some venv) is left alone — it is off PATH until
        that venv is activated, and it is not ours to remove.
        """
        if not (install.python and install.script and self.s.tool_bin):
            return
        if os.path.realpath(Path(install.script).parent) != os.path.realpath(self.s.tool_bin):
            return
        code, _ = run([install.python, "-m", "pip", "uninstall", "-y", PACKAGE], self.log, check=False)
        if code == 0:
            run([self.uv, "tool", "install", "--force", "--managed-python", "--python", PYTHON,
                 self.target], self.log, env=self._uv_env(), check=False)

    def _record(self, mode: str) -> None:
        fields = {
            "method": self.o.method, "version": self.version,
            "uv": str(self.uv) if self.uv else None,
            "tool_python": str(self.s.tool_python) if self.s.tool_python else None,
            "tool_bin": str(self.s.tool_bin) if self.s.tool_bin else None,
            "node": self.s.node[1] if self.s.node else None,
        }
        if not self.existing.get("installed_at"):
            fields["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if mode == "legacy":
            fields["migrated_from"] = {
                "version": self.legacy.version,
                "path": str(self.legacy.where) if self.legacy.where else "",
                "backup": str(self.s.backup) if self.s.backup else "",
            }
        home_mod.write_install(self.home, **fields)
        if self.o.method != "npm" and self.s.tool_bin and not _on_path(self.s.tool_bin):
            run([self.uv, "tool", "update-shell"], self.log, env=self._uv_env(), check=False)
            self.s.path_changed = True
        if any(i.bin_dir and _on_path(i.bin_dir) for i in self.legacy.installs):
            self.s.path_changed = True

    # ── running things ─────────────────────────────────────────────────────
    def _find_uv(self) -> Path:
        if self.o.uv and Path(self.o.uv).is_file():
            return Path(self.o.uv)
        found = runtime.find_uv(self.home)
        if found:
            return found
        return runtime.install_uv(self.home, lambda f: self.screen.progress("python", f * 0.5))

    def _uv_env(self) -> dict:
        env = self._runtime_env()
        env.setdefault("UV_NO_PROGRESS", "1")
        if self.o.method == "npm":
            env["UV_TOOL_BIN_DIR"] = str(self.home / "runtime" / "bin")
        return env

    def _runtime_bin(self) -> Path | None:
        node = self.s.node[0] if self.s.node else None
        return Path(node).parent if node else None

    def _runtime_env(self) -> dict:
        env = dict(os.environ)
        for var in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "SYNAPSE_DATA_DIR",
                    "SYNAPSE_UPGRADE_SKIP_DOWNLOAD"):
            env.pop(var, None)
        env["SYNAPSE_HOME"] = str(self.home)
        extra = [str(d) for d in (self._runtime_bin(), self.home / "runtime" / "uv") if d and Path(d).is_dir()]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        return env

    def _cli_cmd(self, old: bool = False) -> list:
        if old:
            python = self.existing.get("tool_python")
            if not python or not Path(python).is_file():
                return []
            return [python, "-m", "synapse"]
        return [self.s.tool_python, "-m", "synapse"]

    def _cli(self, args: list, stdin_text: str | None = None, old: bool = False) -> str:
        cmd = self._cli_cmd(old)
        if not cmd:
            return ""
        return run(cmd + args, self.log, env=self._runtime_env(), stdin_text=stdin_text)[1]

    def _cli_json(self, args: list, old: bool = False):
        cmd = self._cli_cmd(old)
        if not cmd:
            return None
        env = self._runtime_env()
        out = subprocess.run([str(c) for c in cmd + args], capture_output=True, text=True,
                             env=env, timeout=900)
        self.log.write("$ " + " ".join(str(c) for c in cmd + args))
        if out.stderr.strip():
            for line in out.stderr.strip().splitlines():
                self.log.write(line)
        for line in reversed(out.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except ValueError:
                continue
        self.log.write(out.stdout)
        return None

    # ── the end ────────────────────────────────────────────────────────────
    def _safe_line(self) -> str:
        if self.s.migrated and self.s.backup:
            return f"Your data is in 2.0; the 1.x backup is {_tilde(self.s.backup)}."
        if self.legacy.found:
            where = f" A backup is in {_tilde(self.s.backup)}." if self.s.backup else ""
            return f"Your {self.legacy.version} install and its data are untouched.{where}"
        return "Running the installer again picks up where it stopped."

    def _failure_lines(self, failure: Failed) -> list[str]:
        t = self.term
        step = next((s for s in self.screen.steps if s.key == failure.step), None)
        lines = self.screen.lines() if hasattr(self.screen, "lines") else []
        lines = lines[:-1] if lines and not lines[-1] else lines
        what = step.title if step else "The install"
        lines += ["", "  " + t.style(f"{what} failed: {failure.message}", "red")]
        for line in failure.tail[-6:]:
            lines.append("    " + t.style(line, "dim"))
        lines += ["", "  " + self._safe_line(),
                  "  " + t.style(f"Full log: {_tilde(self.log.path)}", "dim")]
        if self.o.method != "npm" and not self.o.upgrade:
            lines.append("  " + t.style(f"Try again: {RETRY[os.name if os.name == 'nt' else 'posix']}", "dim"))
        lines.append("")
        return lines

    def _final_lines(self, started: bool) -> list[str]:
        t = self.term
        if started:
            name = f"Synapse {self.version}" if self.o.upgrade else "Synapse"
            lines = ["", f"  {name} is running {t.g('arrow')} " + t.style(self.s.link, "cyan", "bold")]
        else:
            lines = ["", f"  Synapse {self.version} is installed. Start it with: synapse start"]
        if self.s.path_changed and self.o.method != "npm":
            lines.append("  " + t.style("Open a new terminal to use the synapse command.", "dim"))
        lines.append("")
        return lines

    def _close(self, final: list[str]) -> None:
        if self.screen is not None:
            self.screen.close(final)
        else:
            self.term.write("\n".join(final) + "\n")


# ── helpers ──────────────────────────────────────────────────────────────────

def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _tilde(path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _plain(step: str, detail: str) -> str:
    return {
        "python": "uv could not get Python", "install": "uv could not install Synapse",
        "node": "Node.js could not be set up", "setup": "the settings could not be saved",
        "start": "Synapse did not start", "migrate": "your data could not be moved",
    }.get(step, detail) + f" ({detail})"


def _checkout_version() -> str:
    try:
        import re
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _constraints_file(home: Path) -> str:
    """The pinned dependency set shipped in the installer, written out for uv."""
    try:
        from importlib import resources
        text = resources.files(__package__).joinpath("constraints.txt").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return ""
    path = home / "runtime" / "constraints.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _playwright_dir() -> Path:
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _merge_env_file(path: Path, values: dict) -> None:
    existing = legacy._read_env(path) if path.exists() else {}
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    added = [f"{k}={v}" for k, v in values.items() if k not in existing]
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            path.write_text("\n".join(lines + ["# Carried over from Synapse 1.x"] + added) + "\n",
                            encoding="utf-8")
        finally:
            os.umask(old_umask)


def _on_path(directory: Path) -> bool:
    target = os.path.normcase(os.path.realpath(directory))
    return any(os.path.normcase(os.path.realpath(p)) == target
               for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _open(url: str) -> None:
    """Open the link in the user's browser; quietly nothing when there is none."""
    if not url:
        return
    try:
        if _is_wsl():
            subprocess.run(["cmd.exe", "/c", "start", "", url.replace("&", "^&")],
                           capture_output=True, timeout=15)
            return
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return
        webbrowser.open(url)
    except Exception:
        pass

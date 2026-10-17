"""Which model CLIs are installed, whether each is signed in, and its models (CLIM1, D299).

Asked of each CLI without a model call, cached for ten minutes per binary
(a new or updated binary is asked again), and served by Settings → Models and
by onboarding (`cli_statuses`):

- Claude Code: `claude --version`, `claude auth status --json`; it lists no
  models, so its aliases (each the newest model the account can use) and this
  version's model ids come from `MODEL_LISTS`.
- Gemini CLI: `gemini --version`; signed in when its Google sign-in file, a
  GEMINI_API_KEY or Vertex settings are there (`gemini_sign_in`, the same
  check its calls use); models from `MODEL_LISTS`, its own constants.
- Codex: `codex --version`, `codex login status`, and `codex debug models` —
  its own catalog for this account, or the cache file it keeps.
- Copilot CLI: `copilot --version`; signed in with a GitHub token it can use;
  models from the choices its `--help` names, else `MODEL_LISTS`.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.providers.cli_invocation import CLI_BINARIES, gemini_sign_in

#: Settings → Models' provider ids and the CLI each one is.
PROVIDERS = {"anthropic_cli": "cli.claude", "gemini_cli": "cli.gemini", "codex_cli": "cli.codex",
             "github_copilot_cli": "cli.copilot"}

LABELS = {"cli.claude": "Claude Code", "cli.gemini": "Gemini CLI", "cli.codex": "Codex", "cli.copilot": "Copilot CLI"}

SIGN_IN_HINTS = {
    "cli.claude": "Run `claude` in a terminal and sign in.",
    "cli.gemini": "Run `gemini` in a terminal and choose Sign in with Google, or set GEMINI_API_KEY.",
    "cli.codex": "Run `codex login` in a terminal.",
    "cli.copilot": "Run `copilot` in a terminal and `/login`, or sign in with `gh auth login`.",
}

#: The models a CLI that cannot list its own takes, per CLI version (major.minor →
#: names), newest last. Aliases first: each is the CLI's own name for the newest
#: model of its kind the account can use, so it never goes stale.
MODEL_LISTS: dict[str, list[tuple[tuple[int, int], list[str]]]] = {
    # The ids claude 2.1.291 carries (its binary); `fable`/`opus`/`sonnet`/`haiku` are its aliases.
    "cli.claude": [((2, 1), ["fable", "opus", "sonnet", "haiku",
                             "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"])],
    # gemini 0.38.0's VALID_GEMINI_MODELS and its aliases (resolveModel).
    "cli.gemini": [((0, 38), ["auto", "pro", "flash", "flash-lite",
                              "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview",
                              "gemini-3.1-flash-lite-preview", "gemini-2.5-pro", "gemini-2.5-flash",
                              "gemini-2.5-flash-lite"]),
                   # gemini 0.64.0's VALID_GEMINI_MODELS (its customtools variant left out).
                   ((0, 64), ["auto", "pro", "flash", "flash-lite",
                              "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview",
                              "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash", "gemini-3.5-flash-lite",
                              "gemini-3.1-flash-lite", "gemini-2.5-pro", "gemma-4-31b-it", "gemma-4-26b-a4b-it"])],
    # When its --help names no choices; `auto` is its own pick.
    "cli.copilot": [((1, 0), ["auto"])],
}

_TTL = 600.0
_PROBE_TIMEOUT = 8.0
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class CliStatus:
    provider: str
    base: str
    label: str
    installed: bool
    version: str | None = None
    #: None when it cannot be told without a model call.
    signed_in: bool | None = None
    sign_in_hint: str | None = None
    #: The CLI's own model names (the part after `cli.<x>.`).
    models: list[str] = field(default_factory=list)
    #: "cli" when the CLI said so itself, "list" when from `MODEL_LISTS`.
    models_source: str | None = None
    models_note: str | None = None
    #: Something it reads that Synapse cannot switch off.
    note: str | None = None
    _catalog: dict | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("_catalog", None)
        return d


_cache: dict[str, tuple[float, tuple, CliStatus]] = {}
_token_cache: tuple[float, str | None] | None = None


def clear_cache() -> None:
    global _token_cache
    _cache.clear()
    _token_cache = None


def _binary_key(path: str) -> tuple:
    try:
        return (path, os.stat(path).st_mtime)
    except OSError:
        return (path, None)


async def _run(*argv: str, timeout: float = _PROBE_TIMEOUT, stderr: bool = False) -> tuple[int | None, str]:
    """Run a probe in the temp dir (no project around it); its exit code and
    stdout, with stderr after it when `stderr` (Codex prints its login there)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if stderr else asyncio.subprocess.DEVNULL, cwd=tempfile.gettempdir())
    except (OSError, ValueError):
        return None, ""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return None, ""
    text = (out or b"").decode("utf-8", errors="replace")
    if stderr and err:
        text += "\n" + err.decode("utf-8", errors="replace")
    return proc.returncode, text


def _version(text: str) -> str | None:
    m = _VERSION.search(text or "")
    return m.group(0) if m else None


def _listed(base: str, version: str | None) -> tuple[list[str], str]:
    """The kept list for this version: the newest one not newer than it."""
    lists = MODEL_LISTS[base]
    pick = lists[0]
    if version:
        major, minor = (int(x) for x in version.split(".")[:2])
        for entry in lists:
            if entry[0] <= (major, minor):
                pick = entry
    return list(pick[1]), f"Synapse's list for {LABELS[base]} {pick[0][0]}.{pick[0][1]}"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_models(catalog: dict | None) -> list[str]:
    """The models Codex lets this account pick, in its own order."""
    entries = []
    for m in (catalog or {}).get("models") or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        if m.get("visibility") != "list" or not m.get("supported_in_api", True):
            continue
        entries.append((m.get("priority", 999), m["slug"]))
    return [slug for _, slug in sorted(entries, key=lambda e: e[0])]


async def _codex_catalog_from(binary: str) -> dict | None:
    code, out = await _run(binary, "debug", "models")
    if code == 0:
        try:
            catalog = json.loads(out)
            if isinstance(catalog, dict) and isinstance(catalog.get("models"), list):
                return catalog
        except ValueError:
            pass
    try:
        catalog = json.loads((_codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        return catalog if isinstance(catalog, dict) else None
    except (OSError, ValueError):
        return None


def _setting_choices(help_text: str, key: str) -> list[str]:
    """The choices `copilot help config` lists under a setting:
    "  `model`: …" then "    - \"claude-sonnet-5.5\"" lines, up to the next setting."""
    out, inside = [], False
    for line in (help_text or "").splitlines():
        if re.match(r"^\s{0,4}`[\w.]+`:", line):
            if inside:
                break
            inside = line.strip().startswith(f"`{key}`:")
            continue
        if inside:
            m = re.match(r'^\s+-\s+"([^"]+)"\s*$', line)
            if m:
                out.append(m.group(1))
    return out


def _help_choices(help_text: str, flag: str) -> list[str]:
    """The choices a CLI's --help names for `flag`, e.g. `(choices: "a", "b")`."""
    for line in (help_text or "").splitlines():
        if flag in line and "choices:" in line:
            inner = line.split("choices:", 1)[1]
            return re.findall(r'"([^"]+)"', inner.split(")", 1)[0])
    return []


async def _read_github_token() -> str | None:
    """A GitHub token from `gh` or Copilot's own config files (never printed)."""
    if shutil.which("gh"):
        code, out = await _run("gh", "auth", "token", timeout=5.0)
        if code == 0 and out.strip():
            return out.strip()
    system = platform.system()
    if system == "Windows":
        config_dirs = [Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "github-copilot"]
    elif system == "Darwin":
        config_dirs = [Path.home() / "Library" / "Application Support" / "github-copilot",
                       Path.home() / ".config" / "github-copilot"]
    else:
        config_dirs = [Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "github-copilot"]
    for config_dir in config_dirs:
        try:
            data = json.loads((config_dir / "hosts.json").read_text(encoding="utf-8"))
            token = data.get("github.com", {}).get("oauth_token") or data.get("github.com", {}).get("token")
            if token:
                return token
        except (OSError, ValueError, AttributeError):
            pass
    return _copilot_config_token()


def _copilot_config_token() -> str | None:
    """The token Copilot CLI saved for its signed-in user, where no keychain
    holds it (WSL, a server): `$COPILOT_HOME/config.json`, which 1.0.98 writes
    with `//` comment lines, its tokens under `authTokens["https://<host>:<login>"]`."""
    home = Path(os.getenv("COPILOT_HOME") or Path.home() / ".copilot")
    try:
        text = (home / "config.json").read_text(encoding="utf-8")
        data = json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("authTokens") if isinstance(data.get("authTokens"), dict) else {}
    last = data.get("lastLoggedInUser") if isinstance(data.get("lastLoggedInUser"), dict) else {}
    if last.get("login"):
        host = last.get("host") or "https://github.com"
        host = host if "://" in host else f"https://{host}"
        entry = tokens.get(f"{host}:{last['login']}")
        if isinstance(entry, dict) and entry.get("token"):
            return entry["token"]
    for entry in tokens.values():
        if isinstance(entry, dict) and entry.get("token"):
            return entry["token"]
    # Older builds kept it flat.
    return data.get("oauth_token") or data.get("token") or None


async def github_token(refresh: bool = False) -> str | None:
    """The GitHub token a Copilot CLI call signs in with: COPILOT_GITHUB_TOKEN,
    GH_TOKEN or GITHUB_TOKEN, else `gh auth token`, else Copilot's config."""
    global _token_cache
    token = os.getenv("COPILOT_GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token
    now = time.monotonic()
    if not refresh and _token_cache and _token_cache[0] > now:
        return _token_cache[1]
    token = await _read_github_token()
    _token_cache = (now + _TTL, token)
    return token


async def _probe(provider: str, base: str) -> CliStatus:
    binary = shutil.which(CLI_BINARIES[base])
    label = LABELS[base]
    if not binary:
        return CliStatus(provider, base, label, installed=False)
    st = CliStatus(provider, base, label, installed=True)
    _, out = await _run(binary, "--version")
    st.version = _version(out)

    if base == "cli.claude":
        code, out = await _run(binary, "auth", "status", "--json")
        try:
            logged = json.loads(out).get("loggedIn")
            st.signed_in = logged if isinstance(logged, bool) else None
        except (ValueError, AttributeError):
            st.signed_in = None
        st.models, st.models_note = _listed(base, st.version)
        st.models_source = "list"

    elif base == "cli.gemini":
        kind, _ = gemini_sign_in(os.environ)
        st.signed_in = kind is not None
        st.models, st.models_note = _listed(base, st.version)
        st.models_source = "list"

    elif base == "cli.codex":
        code, out = await _run(binary, "login", "status", stderr=True)
        if code == 0 and "logged in" in out.lower():
            st.signed_in = True
        elif code not in (0, None):
            st.signed_in = False
        st._catalog = await _codex_catalog_from(binary)
        st.models = _codex_models(st._catalog)
        if st.models:
            st.models_source, st.models_note = "cli", "From Codex"
        # Measured 2026-10-17: run with none of its own tools, newer Codex models
        # emit an agent's tool calls less often (gpt-6-luna 0/5, gpt-5.6-luna 5/8).
        notes = ["Newer Codex models call an agent's tools less reliably than Claude, Gemini or Copilot here."]
        if (_codex_home() / "AGENTS.md").is_file():
            notes.append(f"Codex also reads {_codex_home() / 'AGENTS.md'} on every call; "
                         "it has no switch to leave that file out.")
        st.note = " ".join(notes)

    elif base == "cli.copilot":
        st.signed_in = (await github_token()) is not None
        _, config_help = await _run(binary, "help", "config")
        choices = _setting_choices(config_help, "model")
        if choices:
            # `auto` is its own pick, not one of the setting's choices.
            st.models, st.models_source, st.models_note = ["auto", *choices], "cli", "From Copilot CLI"
        elif choices := _help_choices((await _run(binary, "--help"))[1], "--model"):
            st.models, st.models_source, st.models_note = choices, "cli", "From Copilot CLI"
        else:
            st.models, st.models_note = _listed(base, st.version)
            st.models_source = "list"

    if st.signed_in is False:
        st.sign_in_hint = SIGN_IN_HINTS[base]
    return st


async def cli_status(provider: str, refresh: bool = False) -> CliStatus:
    """One CLI's status, from the cache unless `refresh` or its binary changed."""
    base = PROVIDERS[provider]
    binary = shutil.which(CLI_BINARIES[base])
    key = _binary_key(binary) if binary else (None, None)
    now = time.monotonic()
    hit = _cache.get(provider)
    if not refresh and hit and hit[0] > now and hit[1] == key:
        return hit[2]
    st = await _probe(provider, base)
    _cache[provider] = (now + _TTL, key, st)
    return st


async def cli_statuses(refresh: bool = False) -> dict[str, CliStatus]:
    """Every model CLI's status, keyed by Settings → Models' provider id."""
    got = await asyncio.gather(*(cli_status(p, refresh) for p in PROVIDERS))
    return dict(zip(PROVIDERS, got))


async def codex_catalog(refresh: bool = False) -> dict | None:
    """Codex's own model catalog (`codex debug models`), as last read."""
    return (await cli_status("codex_cli", refresh))._catalog

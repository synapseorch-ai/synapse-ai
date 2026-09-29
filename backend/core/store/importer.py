"""
One-time import of a `backend/data/*.json` install into the store.

Existing installs keep their orchestrations, agents, custom tools, MCP servers
and settings as JSON files under `DATA_DIR`. Those files are the thing being
removed, so upgrading has to bring them across — and it has to do so without
asking the user to run anything, because an upgrade that needs a command
becomes a support thread for every self-hoster who missed it.

Runs at startup. Does nothing at all when the store already holds data, so it
is safe to call on every boot and cannot overwrite work done since the import.
On success the folder is renamed to `data.migrated/` rather than deleted: if
something did not come across, the original is still there, and the rename is
also what stops the import running twice.

Everything lands on the single tenant a self-hosted install has. There is no
tenant dimension in the source files to preserve — that is precisely what made
them a single-machine design.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from core.store.models import DEFAULT_TENANT

#: Source file → the model it populates and the columns to build from each item.
#: Order matters only for readability; the tables are independent.
_SOURCES = (
    "orchestrations", "user_agents", "custom_tools", "mcp_servers", "schedules",
    "usage_logs", "model_pricing", "settings",
)


def _legacy_data_dirs() -> list[Path]:
    """Every folder where a pre-database install kept JSON, highest priority first.

    This module is the only one that knows about these folders, deliberately: a
    shared `DATA_DIR` constant is what every other module used to derive paths
    from, and re-introducing one would invite the same thing again. Here it is
    not configuration — it is the location of the thing being migrated away
    from, and it stops mattering the moment the import has run.

    Three candidates, because the three installers put it in three places:

    1. ``SYNAPSE_DATA_DIR`` — a deployment that still sets it (the container
       images do, for exactly this). Read for detection only; nothing else in
       the engine looks at it any more.
    2. ``backend/data`` — the git-clone layout, and where every v1.x install
       wrote chat sessions, ``schedules.json`` and part of the vault whatever
       ``SYNAPSE_DATA_DIR`` said.
    3. ``~/.synapse/data`` — the npm and pip layouts.

    All of them, not the first: a v1.9 pip install kept its settings and
    agents in (3) and its schedules and chats in (2), so taking the first
    folder with any JSON imported the schedules and stranded everything else.
    """
    configured = (os.getenv("SYNAPSE_DATA_DIR") or "").strip()
    candidates = []
    if configured:
        path = Path(configured)
        candidates.append(
            path if path.is_absolute()
            else Path(__file__).resolve().parent.parent.parent.parent / path
        )
    candidates.append(_code_data_dir())
    candidates.append(Path.home() / ".synapse" / "data")

    found: list[Path] = []
    for candidate in candidates:
        if not _has_legacy_content(candidate):
            continue
        if any(candidate.resolve() == f.resolve() for f in found):
            continue
        found.append(candidate)
    return found


def _code_data_dir() -> Path:
    """`backend/data` beside this code: where every v1.x wrote its chats."""
    return Path(__file__).resolve().parent.parent.parent / "data"


#: Folders of per-item files that are content on their own, without any of the
#: root JSON files beside them — a pip install's `backend/data` can hold only
#: chats and vault files.
_CONTENT_DIRS = ("chat_sessions", "vault", "datasets")


def _has_legacy_content(root: Path) -> bool:
    if not root.is_dir():
        return False
    if any((root / f"{name}.json").is_file() for name in _SOURCES):
        return True
    return any(
        (root / name).is_dir() and any((root / name).iterdir()) for name in _CONTENT_DIRS
    )


def _read(path: Path):
    # A source file that simply is not there is the normal case — an install
    # only has the files for features it used — so it is not worth a line of
    # output. A file that exists and will not parse is.
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[import] could not read {path.name}: {exc}", flush=True)
        return None


def _parse_stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seeded_ids() -> dict[str, set[str]]:
    """Rows the product writes itself, by table: they are not the user's work.

    The builder is seeded on every boot and the installer creates the default
    agent, so counting either made the store "not empty" for good after the
    first start — and an import that missed that start (a forgotten Docker
    mount, a failed first boot) could then never run.
    """
    from core.default_agent import DEFAULT_AGENT_ID
    from core.native_builder.seed import LEGACY_BUILDER_AGENT_IDS, bundled_definitions

    bundled = bundled_definitions()
    return {
        "agents": {a["id"] for a in bundled["agents"]}
        | set(LEGACY_BUILDER_AGENT_IDS) | {DEFAULT_AGENT_ID},
        "orchestrations": {bundled["orchestration"]["id"]},
    }


async def store_is_empty() -> bool:
    """True when no orchestration, agent, tool or MCP server of the user's exists.

    Settings are deliberately excluded from the check: a fresh install can
    legitimately have settings (written by the setup screen) and no content,
    and skipping the import in that case would strand the user's workflows.
    Rows the product seeds itself are excluded for the same reason.
    """
    from core.store import session
    from core.store.models import AgentDB, MCPServerDB, OrchestrationDB, ToolDB

    seeded = _seeded_ids()
    checks = (
        (OrchestrationDB, OrchestrationDB.id.notin_(seeded["orchestrations"])),
        (AgentDB, AgentDB.id.notin_(seeded["agents"])),
        (ToolDB, None),
        (MCPServerDB, None),
    )
    async with session() as s:
        for model, user_rows in checks:
            query = select(func.count()).select_from(model)
            if user_rows is not None:
                query = query.where(user_rows)
            if (await s.execute(query)).scalar():
                return False
    return True


async def import_data_dir(data_dir: str | Path, tenant_id: str = DEFAULT_TENANT) -> dict:
    """Import a DATA_DIR into the store. Returns a per-source count."""
    from core.store import session, upsert
    from core.store.models import (
        AgentDB,
        ApiKeyDB,
        ChatSessionDB,
        CollectionDB,
        MCPServerDB,
        ModelPricingDB,
        OrchestrationDB,
        ScheduleDB,
        SettingDB,
        ToolDB,
        UsageLogDB,
    )
    from core.store.resources import (
        agent_values,
        mcp_server_values,
        orchestration_values,
        tool_values,
    )
    from core.store.schedules import schedule_values
    from core.store.usage import record_values

    root = Path(data_dir)
    counts = dict.fromkeys(_SOURCES, 0)
    # Not in _SOURCES: chat history is a directory of per-session files rather
    # than one file at the root, so it does not take part in the "is there
    # anything here to import" check.
    counts["chat_sessions"] = 0
    counts["google"] = 0
    counts["mcp_tokens"] = 0
    # Also not in _SOURCES: these five are peripheral, so an install having only
    # them is not an install worth importing — but when there *is* something to
    # import, they come across with it.
    for _name in ("repos", "db_configs", "messaging_channels",
                  "personal_details", "api_keys"):
        counts[_name] = 0

    #: source file → (count key, model, conflict target, row builder, "is this
    #: item importable"). The builders are the same ones the live CRUD path
    #: uses, so an imported install and a natively-created one produce identical
    #: rows — when these drifted, the importer became a second schema.
    collections = (
        ("orchestrations.json", "orchestrations", OrchestrationDB, ["id"],
         orchestration_values, lambda i: bool(i.get("id"))),
        ("user_agents.json", "user_agents", AgentDB, ["id"],
         agent_values, lambda i: bool(i.get("id"))),
        ("custom_tools.json", "custom_tools", ToolDB, ["id"],
         tool_values, lambda i: bool(i.get("id") or i.get("name"))),
        ("mcp_servers.json", "mcp_servers", MCPServerDB, ["tenant_id", "name"],
         mcp_server_values, lambda i: bool(i.get("name"))),
        ("schedules.json", "schedules", ScheduleDB, ["id"],
         schedule_values, lambda i: bool(i.get("id"))),
    )

    async with session() as s:
        for filename, count_key, model, index_elements, build, importable in collections:
            items = _read(root / filename) or []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or not importable(item):
                    continue
                await upsert(
                    s, model,
                    values=build(item, tenant_id),
                    index_elements=index_elements,
                )
                counts[count_key] += 1

        # ── the peripheral collections ──────────────────────────────────────
        # Repos, database configs and messaging channels are lists keyed by id;
        # personal details is a single document under the empty key. Keyed
        # exactly as `core/store/collections.py` keys them, including its
        # fall back to the item's position, or an imported install would read
        # back differently from a natively-created one.
        for filename, name in (
            ("repos.json", "repos"),
            ("db_configs.json", "db_configs"),
            ("messaging_channels.json", "messaging_channels"),
        ):
            items = _read(root / filename) or []
            for index, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                await upsert(
                    s, CollectionDB,
                    values={"tenant_id": tenant_id, "collection": name,
                            "key": str(item.get("id") or index), "value": item},
                    index_elements=["tenant_id", "collection", "key"],
                )
                counts[name] += 1

        details = _read(root / "personal_details.json")
        if isinstance(details, dict) and details:
            await upsert(
                s, CollectionDB,
                values={"tenant_id": tenant_id, "collection": "personal_details",
                        "key": "", "value": details},
                index_elements=["tenant_id", "collection", "key"],
            )
            counts["personal_details"] += 1

        # ── api keys ────────────────────────────────────────────────────────
        # A real table, not a collection: every authenticated request looks a
        # key up by hash. The hash is what is stored, so these import losslessly
        # — the keys a user already issued keep working, which is the whole
        # point. Losing them means every script and integration they have built
        # starts returning 401 with nothing in the logs to explain it.
        for item in _read(root / "api_keys.json") or []:
            if not isinstance(item, dict) or not item.get("key_hash"):
                continue
            await upsert(
                s, ApiKeyDB,
                values={
                    "id": str(item.get("id") or item["key_hash"][:32]),
                    "tenant_id": tenant_id,
                    "name": item.get("name") or "",
                    "key_hash": item["key_hash"],
                    "key_prefix": item.get("key_prefix") or "",
                    "is_active": bool(item.get("is_active", True)),
                    "created_at": _parse_stamp(item.get("created_at")),
                    "last_used_at": _parse_stamp(item.get("last_used_at")),
                },
                index_elements=["id"],
            )
            counts["api_keys"] += 1

        # ── chat sessions ───────────────────────────────────────────────────
        # One JSON file per conversation, named `{agent}_{session}.json` — not
        # reversibly parseable, since both ids may contain the separator, so
        # the ids come from the body as they always did.
        for path in sorted((root / "chat_sessions").glob("*.json")):
            data = _read(path)
            if not isinstance(data, dict) or not data.get("session_id"):
                continue
            messages = []
            for turn in data.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                messages.append({"role": "user", "content": turn.get("user", "")})
                messages.append({
                    "role": "assistant",
                    "content": turn.get("assistant", ""),
                    "tools": turn.get("tools") or [],
                    "timestamp": turn.get("timestamp"),
                })
            # Upserted on the table's key: two legacy folders can both hold a
            # conversation (a pip install's two), and a plain insert of the
            # second raised and lost the whole import.
            await upsert(
                s, ChatSessionDB,
                values={
                    "tenant_id": tenant_id,
                    "session_id": data["session_id"],
                    "agent_id": data.get("agent_id") or "default",
                    "messages": messages,
                    "cli_session_ids": data.get("cli_session_ids") or {},
                    "last_message_at": _parse_stamp(data.get("last_updated")),
                },
                index_elements=["tenant_id", "session_id", "agent_id"],
            )
            counts["chat_sessions"] += 1

        # ── mcp oauth tokens ────────────────────────────────────────────────
        # `mcp_tokens/<server>.json` plus `<server>_client.json` become one
        # document per server, matching what StoreTokenStorage writes.
        tokens: dict[str, dict] = {}
        for path in sorted((root / "mcp_tokens").glob("*.json")):
            document = _read(path)
            if not isinstance(document, dict):
                continue
            stem = path.stem
            field = "client" if stem.endswith("_client") else "tokens"
            server = stem[: -len("_client")] if field == "client" else stem
            tokens.setdefault(server, {"id": server})[field] = document
        if tokens:
            for server, value in tokens.items():
                await upsert(
                    s, CollectionDB,
                    values={"tenant_id": tenant_id, "collection": "mcp_tokens",
                            "key": server, "value": value},
                    index_elements=["tenant_id", "collection", "key"],
                )
                counts["mcp_tokens"] += 1

        # ── usage log ───────────────────────────────────────────────────────
        # Append-only history rather than a keyed collection, so these are
        # plain inserts. The store is empty when the import runs, so there is
        # nothing to conflict with.
        records = _read(root / "usage_logs.json") or []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            s.add(UsageLogDB(tenant_id=tenant_id, **record_values(record)))
            counts["usage_logs"] += 1

        # ── model pricing ───────────────────────────────────────────────────
        # Not tenant data — one rate card for the install. Imported so an
        # edited price survives, then topped up from the shipped defaults at
        # startup for any model the file predates.
        pricing = _read(root / "model_pricing.json") or {}
        for model, entry in (pricing.items() if isinstance(pricing, dict) else []):
            if not isinstance(entry, dict):
                continue
            await upsert(
                s, ModelPricingDB,
                values={"model": model, "entry": entry},
                index_elements=["model"],
            )
            counts["model_pricing"] += 1

        # ── google oauth ────────────────────────────────────────────────────
        # Stored as documents rather than settings rows: load_settings() returns
        # one dict read on every turn, and a refresh token has no business in it.
        for filename, key in (("credentials.json", "google_client"),
                              ("token.json", "google_token")):
            document = _read(root / filename)
            if isinstance(document, dict) and document:
                await upsert(
                    s, CollectionDB,
                    values={"tenant_id": tenant_id, "collection": key,
                            "key": "", "value": document},
                    index_elements=["tenant_id", "collection", "key"],
                )
                counts["google"] += 1

        # ── settings ────────────────────────────────────────────────────────
        settings = _read(root / "settings.json") or {}
        for key, value in (settings.items() if isinstance(settings, dict) else []):
            await upsert(
                s, SettingDB,
                values={"tenant_id": tenant_id, "key": key, "value": json.dumps(value)},
                index_elements=["tenant_id", "key"],
            )
            counts["settings"] += 1

    # These rows went in through upsert() directly rather than through the CRUD
    # helpers, so nothing has invalidated the store's read-through cache.
    from core.store import cache
    cache.invalidate_all()

    return counts


def _holds_live_state(root: Path) -> bool:
    """Whether `root` is also where this install keeps its current state.

    True for the container images, whose one persistent volume holds both the
    legacy JSON and the database that replaced it. Renaming such a folder would
    take `synapse.db` with it — and the data has just been imported *into* that
    file, so the rename would destroy exactly what the import produced.
    """
    return any((root / name).exists() for name in ("synapse.db", "blobs", "state"))


def _retire_inside(root: Path) -> str:
    """Move the consumed files into `<root>/migrated/`, leaving `root` itself."""
    destination = root / "migrated"
    destination.mkdir(exist_ok=True)
    consumed = (
        *_SOURCES, "credentials", "token",
        "repos", "db_configs", "messaging_channels", "personal_details",
        "api_keys",
    )
    for name in consumed:
        source = root / f"{name}.json"
        if source.is_file():
            source.rename(destination / source.name)
    for name in ("chat_sessions", "mcp_tokens", "vault", "datasets", "whatsapp_sessions"):
        source = root / name
        if source.is_dir():
            source.rename(destination / name)
    return str(destination)


def _retire(root: Path) -> str:
    """Put the imported files beyond the reach of a second import.

    Normally the whole folder is renamed, which is legible from a file manager
    and obviously reversible. When the folder also holds live state, or is a
    mount point that cannot be renamed, only the files that were consumed move
    into `<root>/migrated/`, which achieves the same thing without touching
    anything else in there.
    """
    if _holds_live_state(root):
        return _retire_inside(root)

    migrated = root.parent / f"{root.name}.migrated"
    if migrated.exists():
        # A previous import already claimed the name. Keep both rather than
        # overwriting whatever is in there.
        migrated = root.parent / f"{root.name}.migrated.{int(root.stat().st_mtime)}"
    try:
        root.rename(migrated)
    except OSError:
        # A mount point cannot be renamed (compose binds 1.x's ./backend/data
        # at /legacy-data): move what was consumed inside it instead.
        return _retire_inside(root)
    return migrated.name


async def import_sources(roots, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """Import each legacy folder in `roots` and retire it. One report per folder.

    `roots` is highest priority first — the order `_legacy_data_dirs()` finds
    them in, and the order `synapse migrate --from` is given them — and they
    are imported in reverse, so where two folders hold the same item the
    higher-priority folder's copy is written last and wins.

    Each report is ``{"path", "counts", "files", "retired_to"}``, or
    ``{"path", "error"}`` for a folder whose import failed. A failed folder's
    transaction is rolled back and the folder left where it was; the others
    still come across, because one corrupt folder must not cost the user the
    rest of their work.
    """
    from core.store.legacy_files import import_legacy_files

    reports: list[dict] = []
    for root in reversed([Path(r) for r in roots]):
        try:
            counts = await import_data_dir(root, tenant_id)
            files = import_legacy_files(root)
        except Exception as exc:
            reports.append({"path": str(root), "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            where = _retire(root)
        except OSError as exc:
            # The data is in the store either way; retiring the files is only
            # what stops a second import, and `store_is_empty()` does that too.
            print(f"[import] imported, but could not retire {root}: {exc}", flush=True)
            where = str(root)
        reports.append({"path": str(root), "counts": counts, "files": files,
                        "retired_to": where})
    reports.reverse()
    return reports


def merged_counts(reports: list[dict]) -> dict:
    """The per-source counts of several folders' reports, summed."""
    total: dict = {}
    for report in reports:
        for source in ("counts", "files"):
            for key, value in (report.get(source) or {}).items():
                total[key] = total.get(key, 0) + value
    return total


async def import_legacy_data_if_present() -> dict | None:
    """Import a pre-database install's JSON once, on first boot after upgrade.

    Returns the per-source counts when an import ran, None when there was
    nothing to do. Never raises: a failed import must not stop the process from
    starting, and leaving the folder in place means the next boot retries.
    """
    roots = _legacy_data_dirs()
    if not roots:
        return None

    try:
        if not await store_is_empty():
            return None
        reports = await import_sources(roots)
    except Exception as exc:
        print(f"[import] import from {', '.join(map(str, roots))} failed, "
              f"leaving it in place: {exc}", flush=True)
        return None

    for report in reports:
        if "error" in report:
            print(f"[import] import from {report['path']} failed, leaving it in place: "
                  f"{report['error']}", flush=True)
            continue
        counts = merged_counts([report])
        print(
            f"[import] moved {sum(counts.values())} items from {report['path']} into the "
            f"store ({', '.join(f'{k}={v}' for k, v in counts.items() if v)}). "
            f"The old files are now under {report['retired_to']}.",
            flush=True,
        )
    imported = [r for r in reports if "error" not in r]
    return merged_counts(imported) if imported else None

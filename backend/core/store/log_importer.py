"""One-time import of a pre-store install's `backend/logs/` into the store.

## Why this is separate from `importer.py`

`importer.py` migrates `backend/data/` — orchestrations, agents, tools, MCP
servers, settings, usage. **Run logs were never in that folder**, so they were
never in its scope, and D29 moved where they are *read* from without moving
what already existed. The result is silent and specific:

* `OrchestrationLogger.list_logs()` reads the blob store and a scratch dir.
* `backend/logs/orchestration_logs/*.log` is read by nothing.
* The "Runs & Logs" screen still exists, its endpoints still answer, and every
  one of them returns `[]`.

On the install this was found on that is 3,685 orchestration logs, 1,293 agent
logs and 394 run-state files — intact on disk, invisible in the product, with
nothing anywhere saying where they went.

## Why it cannot reuse `importer.py`'s triggers

Both of its guards are wrong here, and in opposite directions:

* It keys off `backend/data/` existing. An install that has already upgraded has
  `data.migrated/`, so a log import hung off the same trigger would never run
  for exactly the people who need it.
* It refuses unless `store_is_empty()`. By definition the store is *not* empty
  by the time anyone notices their logs are missing.

So this has its own trigger — `backend/logs/` exists and holds something — and
its own idempotence: the rename to `logs.migrated/` is what stops it running
twice, the same mechanism `importer.py` uses on `data/`.

## What is not imported

`logs/orchestration_events/*.jsonl` are the live event journals a run appends to
while it is streaming, replayed by a browser that reconnects mid-run. They are a
buffer, not history — the run's durable record is its row and its log — and a
finished run's journal is closed. Copying them would restore nothing readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.store.models import DEFAULT_TENANT

#: `(directory under logs/, blob prefix)` for the two loggers, matching
#: `core/orchestration/logger.py` and `core/agent_logger.py`. Both write
#: `{prefix}/{run_id}.log` plus a `.meta.json` sidecar that `list_logs()` reads
#: so a listing does not have to download every body.
_LOG_TREES = (
    ("orchestration_logs", "logs/orchestration"),
    ("agent_logs", "logs/agent"),
)

#: Columns on `OrchestrationRunDB` that a legacy run-state file can fill. Named
#: rather than splatted: these files predate several columns and would
#: otherwise pass unknown keys straight into the model constructor.
_RUN_FIELDS = (
    "run_id", "orchestration_id", "session_id", "status",
    "shared_state", "step_history", "current_step_id",
    "waiting_for_human", "human_prompt", "human_fields",
    "nested_run_id", "nested_orch_id",
    "total_tokens_used", "total_cost_usd",
)

def legacy_logs_dir() -> Path | None:
    """Where a pre-store install kept its logs, or None if there is nothing.

    One candidate: every v1.x logger wrote beside the code (`backend/logs`),
    whichever installer put it there. `~/.synapse/logs` was never a v1.x
    location — it is where a v2 packaged install writes its own process logs,
    which is why it is not looked at. A legacy install whose code lives
    elsewhere (a script install being replaced) is named explicitly, through
    `synapse migrate --logs`.
    """
    candidates = (Path(__file__).resolve().parent.parent.parent / "logs",)
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if any((candidate / name).is_dir() for name, _ in _LOG_TREES):
            return candidate
        if (candidate / "orchestration_runs").is_dir():
            return candidate
    return None


def _import_log_tree(root: Path, folder: str, prefix: str) -> int:
    """Copy one logger's `.log` files into the blob store. Returns the count."""
    from core.storage import get_blob_store

    # Each logger's own sidecar builder, so a migrated log lists exactly like a
    # freshly written one — same fields, same size, same sort key. Importing
    # them rather than re-deriving the format here is the point: this module
    # would otherwise be a third copy of a header parser that has already
    # drifted once in this codebase.
    if prefix.endswith("agent"):
        from core.agent_logger import meta_for
    else:
        from core.orchestration.logger import meta_for

    source = root / folder
    if not source.is_dir():
        return 0

    store = get_blob_store()
    moved = 0
    for path in sorted(source.glob("*.log")):
        run_id = path.stem
        body_key = f"{prefix}/{run_id}.log"
        if store.exists(body_key):
            continue  # A run that has since been re-logged wins.
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        store.put(body_key, text)
        # The sidecar. Without it a listing shows a bare run id with no size and
        # sorts to the bottom, because it sorts on `started_at` and that only
        # exists in the meta.
        store.put(f"{prefix}/{run_id}.meta.json", json.dumps(meta_for(text)))
        moved += 1
    return moved


async def _import_run_state(root: Path, tenant_id: str) -> int:
    """Bring `logs/orchestration_runs/*.json` into `orchestration_runs`."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB

    source = root / "orchestration_runs"
    if not source.is_dir():
        return 0

    from core.store.importer import _parse_stamp

    imported = 0
    async with session() as s:
        for path in sorted(source.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("run_id"):
                continue

            run_id = str(data["run_id"])
            existing = (
                await s.execute(
                    select(OrchestrationRunDB.run_id).where(
                        OrchestrationRunDB.tenant_id == tenant_id,
                        OrchestrationRunDB.run_id == run_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                # A run the store already knows about is newer than the file by
                # construction — the file is what the store replaced.
                continue

            values: dict[str, Any] = {
                key: data[key] for key in _RUN_FIELDS if data.get(key) is not None
            }
            values["run_id"] = run_id
            values.setdefault("orchestration_id", "unknown")
            values.setdefault("status", "completed")
            for stamp in ("started_at", "ended_at"):
                parsed = _parse_stamp(data.get(stamp))
                if parsed is not None:
                    values[stamp] = parsed

            s.add(OrchestrationRunDB(tenant_id=tenant_id, **values))
            imported += 1
        await s.commit()
    return imported


async def import_logs_dir(logs_dir: str | Path, tenant_id: str = DEFAULT_TENANT) -> dict:
    """Import one `logs/` tree. Returns a per-source count."""
    root = Path(logs_dir)
    counts = {"orchestration_logs": 0, "agent_logs": 0, "orchestration_runs": 0}

    for folder, prefix in _LOG_TREES:
        counts[folder] = _import_log_tree(root, folder, prefix)
    counts["orchestration_runs"] = await _import_run_state(root, tenant_id)
    return counts


def _retire(root: Path) -> str:
    """Rename the folder so a second boot does not re-import it."""
    migrated = root.parent / f"{root.name}.migrated"
    if migrated.exists():
        migrated = root.parent / f"{root.name}.migrated.{int(root.stat().st_mtime)}"
    root.rename(migrated)
    return migrated.name


async def import_log_sources(roots, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """Import each named `logs/` folder and retire it. One report per folder.

    For a legacy install whose code is not this process's own — the installer
    replacing a v1.x script install names that install's `backend/logs`. Each
    report is ``{"path", "counts", "retired_to"}`` or ``{"path", "error"}``.
    """
    reports: list[dict] = []
    for root in (Path(r) for r in roots):
        if not root.is_dir():
            continue
        try:
            counts = await import_logs_dir(root, tenant_id)
        except Exception as exc:  # noqa: BLE001 — one folder must not stop the rest
            reports.append({"path": str(root), "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            where = _retire(root)
        except OSError:
            where = str(root)
        reports.append({"path": str(root), "counts": counts, "retired_to": where})
    return reports


async def import_legacy_logs_if_present() -> dict | None:
    """Bring a pre-store install's logs across once, on boot.

    Returns the counts when an import ran, None when there was nothing to do.
    Never raises: failing to migrate history must not stop the server starting,
    and leaving the folder in place means the next boot retries.
    """
    root = legacy_logs_dir()
    if root is None:
        return None

    try:
        counts = await import_logs_dir(root)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        print(f"[import] log import from {root} failed, leaving it in place: {exc}", flush=True)
        return None

    try:
        where = _retire(root)
    except OSError as exc:
        # Nothing re-imports a log the blob store already has, so a folder that
        # could not be renamed costs a directory walk on the next boot rather
        # than duplicate history.
        print(f"[import] logs imported, but could not retire {root}: {exc}", flush=True)
        where = str(root)

    total = sum(counts.values())
    if total:
        print(
            f"[import] moved {total} log items from {root} into the store "
            f"({', '.join(f'{k}={v}' for k, v in counts.items() if v)}). "
            f"The old files are now under {where}.",
            flush=True,
        )
    return counts

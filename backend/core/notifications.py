"""User-facing run notifications.

A process-wide hub that turns orchestration lifecycle events into
notifications the UI (and optionally a messaging channel) can consume:

- `human_input_required`  → "… needs your input"
- `orchestration_complete`→ run completed / run failed
- `orchestration_error`   → run failed

Fed by the runner pump via `observe_run_event` (registered on
`core.orchestration.runner.event_observers` at server startup), so every
entry point — UI, API, scheduler — produces notifications with no extra
wiring. Standalone-safe: a bounded in-memory ring with best-effort JSON
persistence, no external dependencies.

**One ring per tenant (F337).** The hub is a process singleton, and a
multi-tenant embedder's worker serves every tenant from one process. With one
ring per process, tenant A's eval regression joined the ring and the next
persist inside tenant B's job wrote it to B's `notifications.json` — the blob
store keys by whichever tenant is current. So each tenant has its own ring,
loaded from and saved to its own blob, and its own subscribers. A single-tenant
install has exactly one tenant, `default`, and behaves as it always did.

Delivery surfaces:
- `GET /api/notifications` + `/api/notifications/stream` (routes/notifications.py)
  for the in-app provider (toasts, badges, browser notifications).
- Optional messaging dispatch: when settings.json has
  `run_notification_channel_id` set to a configured messaging channel id,
  each notification is also sent there (`notify_chat_id` or the channel's
  last active chat).
"""
# Annotations stay unevaluated: NotificationHub has a method named `list`, and on
# Python 3.11–3.13 `list[dict]` in a later annotation of the class would call it
# at import (F308). 3.14 evaluates them lazily and never showed it.
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path

from core.native_builder import BUILDER_RUN_PREFIX
from core.tenancy import get_tenant

#: Blob key. A 200-item ring already rewritten whole on every publish, so it
#: maps onto put() directly — there is nothing append-shaped about it.
NOTIFICATIONS_KEY = "notifications.json"
CAPACITY = 200
#: Tenants whose rings are held in memory at once. Dropping the least recently
#: used one loses nothing — every change is saved as it happens — so this only
#: bounds memory in a process that serves many tenants.
MAX_TENANTS = 64


class _Ring:
    """One tenant's notifications and the id the next one gets."""

    __slots__ = ("items", "next_id")

    def __init__(self, items: list[dict]):
        self.items = items
        self.next_id = (items[-1]["id"] + 1) if items else 1


class NotificationHub:
    def __init__(self, capacity: int = CAPACITY, persist_path: str = NOTIFICATIONS_KEY,
                 max_tenants: int = MAX_TENANTS):
        self._capacity = capacity
        self._path = persist_path
        self._max_tenants = max_tenants
        # tenant → its ring, loaded on first use under that tenant (F337).
        # Nothing is read at construction: the hub is built at import, in
        # whatever tenant the importing code happened to run as.
        self._rings: OrderedDict[str, _Ring] = OrderedDict()
        # tenant → live streams. Kept apart from the rings so dropping a ring
        # from memory never drops someone's open stream.
        self._subs: dict[str, set[asyncio.Queue]] = {}
        # run_id → orchestration display name (cached from orchestration_start)
        self._orch_names: dict[str, str] = {}
        # Runs whose terminal notification already fired — engine failure paths
        # can emit both orchestration_error and orchestration_complete(failed).
        self._terminal_notified: set[str] = set()
        self._server_module = None

    def _ring(self) -> _Ring:
        """The current tenant's ring, read from its own blob the first time."""
        tenant = get_tenant()
        ring = self._rings.get(tenant)
        if ring is None:
            ring = self._rings[tenant] = _Ring(self._load())
            while len(self._rings) > self._max_tenants:
                self._rings.popitem(last=False)
        else:
            self._rings.move_to_end(tenant)
        return ring

    def _streams(self) -> list[asyncio.Queue]:
        return list(self._subs.get(get_tenant(), ()))

    # ── persistence ──────────────────────────────────────────────────────
    def reload(self):
        """Forget the rings, so the next read comes from the blob store.

        The hub is built at import, before the server installs its blob store,
        so anything read before then came from the default directory's copy.
        """
        self._rings.clear()

    def _load(self) -> list[dict]:
        try:
            from core.storage import get_blob_store
            raw = get_blob_store().get(self._path)
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _persist(self, ring: _Ring):
        """Save `ring` — the current tenant's, so the blob store's tenant prefix
        and the ring always agree."""
        try:
            from core.storage import get_blob_store
            get_blob_store().put(self._path, json.dumps(ring.items))
        except Exception:
            pass  # best-effort — the ring keeps working in memory

    # ── core API ─────────────────────────────────────────────────────────
    def publish(self, kind: str, title: str, body: str = "",
                run_id: str | None = None, data: dict | None = None) -> dict:
        ring = self._ring()
        item = {
            "id": ring.next_id,
            "ts": time.time(),
            "kind": kind,          # human_input | tool_approval | run_completed | run_failed
            "title": title[:200],
            "body": (body or "")[:500],
            "run_id": run_id,
            "data": data or {},
        }
        ring.next_id += 1
        ring.items.append(item)
        if len(ring.items) > self._capacity:
            ring.items = ring.items[-self._capacity:]
        self._persist(ring)
        for queue in self._streams():
            queue.put_nowait(item)
        self._dispatch_messaging(item)
        return item

    def list(self, after_id: int = 0) -> list[dict]:
        return [i for i in self._ring().items if i["id"] > after_id]

    def last_id(self) -> int:
        return self._ring().next_id - 1

    def subscribe(self) -> asyncio.Queue:
        """A stream of the current tenant's notifications, and no one else's."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(get_tenant(), set()).add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        for tenant, queues in list(self._subs.items()):
            queues.discard(queue)
            if not queues:
                del self._subs[tenant]

    def configure(self, server_module):
        """Give the hub access to the messaging manager (server startup)."""
        self._server_module = server_module

    # ── engine event observer (runner pump) ──────────────────────────────
    async def observe_run_event(self, run_id: str, event: dict):
        # The AI builder is driven by the same pump as every other run, so its
        # events arrive here — and none of them is news. The panel shows the
        # question in the conversation the person is already reading; a bell
        # item and a toast would say it twice, and the toast lands in the one
        # corner that already covers a parked run's Answer control.
        if run_id.startswith(BUILDER_RUN_PREFIX):
            return
        etype = event.get("type", "")
        if etype == "orchestration_start":
            self._orch_names[run_id] = (
                event.get("orchestration_name") or event.get("orchestration_id") or run_id
            )
            self._terminal_notified.discard(run_id)
        elif etype == "step_start":
            # Any forward progress re-arms terminal notification (a resumed
            # run emits no orchestration_start) and means any pending
            # human-input request for this run was answered.
            self._terminal_notified.discard(run_id)
            self._resolve_human_input(run_id, "answered")
        elif etype == "human_input_required":
            name = await self._orch_name(run_id)
            self.publish(
                "human_input", f"{name} needs your input",
                body=str(event.get("prompt") or ""),
                run_id=run_id,
                data={"step_id": event.get("orch_step_id")},
            )
        elif etype == "orchestration_complete":
            status = event.get("status", "completed")
            self._resolve_human_input(run_id, "cancelled" if status == "cancelled" else "answered")
            if run_id in self._terminal_notified:
                return
            self._terminal_notified.add(run_id)
            name = await self._orch_name(run_id)
            if status == "completed":
                self.publish("run_completed", f"{name} completed", run_id=run_id)
            else:
                self.publish("run_failed", f"{name} {status}", run_id=run_id)
        elif etype == "orchestration_error":
            # A cancel is announced as this, with these words, by the pump,
            # the fleet and `cancel_run` alike (F193): it is not a failure.
            cancelled = event.get("error") == "Cancelled"
            self._resolve_human_input(run_id, "cancelled" if cancelled else "answered")
            if run_id in self._terminal_notified:
                return
            self._terminal_notified.add(run_id)
            name = await self._orch_name(run_id)
            if cancelled:
                self.publish("run_failed", f"{name} cancelled", run_id=run_id)
            else:
                self.publish("run_failed", f"{name} failed",
                             body=str(event.get("error") or ""), run_id=run_id)
        elif etype == "run_stream_end" and not event.get("paused"):
            self._orch_names.pop(run_id, None)

    def _resolve_human_input(self, run_id: str, how: str = "answered"):
        """Mark this run's human-input notifications resolved, and how:
        `answered`, or `cancelled` when the run was cancelled while asking
        (F193) — the bell said "answered" for both. The updated items (same id,
        resolved=true) are re-pushed to live subscribers; clients upsert by id
        so the bell stops showing them as actionable."""
        self._resolve([
            item for item in self._ring().items
            if item["run_id"] == run_id and item["kind"] == "human_input"
            and not item.get("resolved")
        ], how)

    def resolve_approval(self, approval_id: str | None, how: str) -> None:
        """Mark a scheduled run's tool-approval item resolved (F268): `allowed`,
        `refused`, or `expired` when nobody answered or a newer run replaced it.
        Its Approve / Refuse go away on every open bell."""
        if not approval_id:
            return
        self._resolve([
            item for item in self._ring().items
            if item["kind"] == "tool_approval" and not item.get("resolved")
            and (item.get("data") or {}).get("approval_id") == approval_id
        ], how)

    def _resolve(self, updated: list[dict], how: str) -> None:
        if not updated:
            return
        for item in updated:
            item["resolved"] = True
            item["resolved_as"] = how
        self._persist(self._ring())
        for queue in self._streams():
            for item in updated:
                queue.put_nowait(item)

    async def _orch_name(self, run_id: str) -> str:
        """Cached name, with a checkpoint fallback for resumed runs (which
        emit no orchestration_start)."""
        name = self._orch_names.get(run_id)
        if name:
            return name
        try:
            from core.orchestration.state import SharedState
            from core.routes.orchestrations import load_orchestrations
            orch_id = (await SharedState.restore(run_id)).run.orchestration_id
            orch = next((o for o in await load_orchestrations() if o["id"] == orch_id), None)
            name = (orch or {}).get("name") or orch_id
        except Exception:
            name = "Orchestration"
        self._orch_names[run_id] = name
        return name

    # ── startup: reconstruct missed human-input notifications ────────────
    async def reconstruct_missed(self, limit: int = 100):
        """Re-publish 'needs your input' for runs still paused on a human step
        (the server may have restarted since the pause fired)."""
        try:
            from core.orchestration.state import SharedState
            # Only an *unresolved* item counts as already-notified: a run that
            # paused again at a later step deserves a fresh notification.
            notified_runs = {
                i["run_id"] for i in self._ring().items
                if i["kind"] == "human_input" and not i.get("resolved")
            }
            paused_waiting: set[str] = set()
            statuses: dict[str, str] = {}
            for summary in await SharedState.list_runs(limit=limit):
                statuses[summary.get("run_id") or ""] = summary.get("status") or ""
                if summary.get("status") != "paused":
                    continue
                run_id = summary.get("run_id") or ""
                try:
                    run = (await SharedState.restore(run_id)).run
                except Exception:
                    continue
                if not run.waiting_for_human:
                    continue
                paused_waiting.add(run_id)
                if run_id not in notified_runs:
                    self.publish(
                        "human_input",
                        f"{await self._orch_name(run_id)} needs your input",
                        body=str(run.human_prompt or ""),
                        run_id=run_id,
                    )
            # Inverse sweep: unresolved items whose run is no longer waiting
            # (answered/failed/swept while we were down) get resolved.
            for run_id in notified_runs - paused_waiting:
                self._resolve_human_input(
                    run_id, "cancelled" if statuses.get(run_id) == "cancelled" else "answered")
        except Exception as e:
            print(f"Warning: notification reconstruction failed: {e}", flush=True)

    # ── optional messaging dispatch ──────────────────────────────────────
    def _dispatch_messaging(self, item: dict):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_messaging(item))

    async def _send_messaging(self, item: dict):
        try:
            from core.config import load_settings
            channel_id = (load_settings() or {}).get("run_notification_channel_id") or ""
            if not channel_id:
                return
            manager = getattr(self._server_module, "messaging_manager", None)
            adapter = manager._adapters.get(channel_id) if manager else None
            if adapter is None:
                return
            from core.messaging import store as channel_store
            channel = await channel_store.get_channel(channel_id) or {}
            chat_id = (
                channel.get("notify_chat_id")
                or channel.get("default_conversation_id")
                or getattr(adapter, "_last_chat_id", None)
            )
            if not chat_id:
                return
            icon = {
                "human_input": "⏸", "tool_approval": "⏸", "run_completed": "✅", "run_failed": "❌",
                "eval_regression": "📉",
            }.get(item["kind"], "🔔")
            text = f"{icon} {item['title']}"
            if item.get("body"):
                text += f"\n{item['body']}"
            await adapter.send_message(chat_id, text)
        except Exception as e:
            print(f"Warning: messaging notification failed: {e}", flush=True)


hub = NotificationHub()

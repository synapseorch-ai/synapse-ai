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

Delivery surfaces:
- `GET /api/notifications` + `/api/notifications/stream` (routes/notifications.py)
  for the in-app provider (toasts, badges, browser notifications).
- Optional messaging dispatch: when settings.json has
  `run_notification_channel_id` set to a configured messaging channel id,
  each notification is also sent there (`notify_chat_id` or the channel's
  last active chat).
"""
import asyncio
import json
import time
from pathlib import Path

from core.native_builder import BUILDER_RUN_PREFIX

#: Blob key. A 200-item ring already rewritten whole on every publish, so it
#: maps onto put() directly — there is nothing append-shaped about it.
NOTIFICATIONS_KEY = "notifications.json"
CAPACITY = 200


class NotificationHub:
    def __init__(self, capacity: int = CAPACITY, persist_path: str = NOTIFICATIONS_KEY):
        self._capacity = capacity
        self._path = persist_path
        self._items: list[dict] = self._load()
        self._next_id = (self._items[-1]["id"] + 1) if self._items else 1
        self._subs: set[asyncio.Queue] = set()
        # run_id → orchestration display name (cached from orchestration_start)
        self._orch_names: dict[str, str] = {}
        # Runs whose terminal notification already fired — engine failure paths
        # can emit both orchestration_error and orchestration_complete(failed).
        self._terminal_notified: set[str] = set()
        self._server_module = None

    # ── persistence ──────────────────────────────────────────────────────
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

    def _persist(self):
        try:
            from core.storage import get_blob_store
            get_blob_store().put(self._path, json.dumps(self._items))
        except Exception:
            pass  # best-effort — the ring keeps working in memory

    # ── core API ─────────────────────────────────────────────────────────
    def publish(self, kind: str, title: str, body: str = "",
                run_id: str | None = None, data: dict | None = None) -> dict:
        item = {
            "id": self._next_id,
            "ts": time.time(),
            "kind": kind,          # human_input | run_completed | run_failed
            "title": title[:200],
            "body": (body or "")[:500],
            "run_id": run_id,
            "data": data or {},
        }
        self._next_id += 1
        self._items.append(item)
        if len(self._items) > self._capacity:
            self._items = self._items[-self._capacity:]
        self._persist()
        for queue in list(self._subs):
            queue.put_nowait(item)
        self._dispatch_messaging(item)
        return item

    def list(self, after_id: int = 0) -> list[dict]:
        return [i for i in self._items if i["id"] > after_id]

    def last_id(self) -> int:
        return self._next_id - 1

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subs.discard(queue)

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
            self._resolve_human_input(run_id)
        elif etype == "human_input_required":
            name = await self._orch_name(run_id)
            self.publish(
                "human_input", f"{name} needs your input",
                body=str(event.get("prompt") or ""),
                run_id=run_id,
                data={"step_id": event.get("orch_step_id")},
            )
        elif etype == "orchestration_complete":
            self._resolve_human_input(run_id)
            if run_id in self._terminal_notified:
                return
            self._terminal_notified.add(run_id)
            name = await self._orch_name(run_id)
            status = event.get("status", "completed")
            if status == "completed":
                self.publish("run_completed", f"{name} completed", run_id=run_id)
            else:
                self.publish("run_failed", f"{name} {status}", run_id=run_id)
        elif etype == "orchestration_error":
            self._resolve_human_input(run_id)
            if run_id in self._terminal_notified:
                return
            self._terminal_notified.add(run_id)
            name = await self._orch_name(run_id)
            self.publish("run_failed", f"{name} failed",
                         body=str(event.get("error") or ""), run_id=run_id)
        elif etype == "run_stream_end" and not event.get("paused"):
            self._orch_names.pop(run_id, None)

    def _resolve_human_input(self, run_id: str):
        """Mark this run's human-input notifications answered. The updated
        items (same id, resolved=true) are re-pushed to live subscribers;
        clients upsert by id so the bell stops showing them as actionable."""
        updated = [
            item for item in self._items
            if item["run_id"] == run_id and item["kind"] == "human_input"
            and not item.get("resolved")
        ]
        if not updated:
            return
        for item in updated:
            item["resolved"] = True
        self._persist()
        for queue in list(self._subs):
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
                i["run_id"] for i in self._items
                if i["kind"] == "human_input" and not i.get("resolved")
            }
            paused_waiting: set[str] = set()
            for summary in await SharedState.list_runs(limit=limit):
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
                self._resolve_human_input(run_id)
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
                "human_input": "⏸", "run_completed": "✅", "run_failed": "❌",
                "eval_regression": "📉",
            }.get(item["kind"], "🔔")
            text = f"{icon} {item['title']}"
            if item.get("body"):
                text += f"\n{item['body']}"
            await adapter.send_message(chat_id, text)
        except Exception as e:
            print(f"Warning: messaging notification failed: {e}", flush=True)


hub = NotificationHub()

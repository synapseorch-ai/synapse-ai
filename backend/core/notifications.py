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

**One ring in scale mode (F361).** There each API server and each worker kept
its own copy of a tenant's ring and wrote it whole, so a worker's eval
regression and the API's next publish overwrote each other, and three API
servers (OSS's k8s) did the same; the bell, which reads memory, never heard a
worker's item. `share(redis)` makes the blob the ring: every change is queued to
one writer per process, which takes a Redis lock, reads the blob, applies the
change (the id comes from the ring), writes it back and PUBLISHes what changed.
Every API server listens and passes it to its own streams, and reads the blob
when asked for the list. Standalone and cloud never call `share`.

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
import contextlib
import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from core.log import get_logger
from core.native_builder import BUILDER_RUN_PREFIX
from core.tenancy import get_tenant, tenant_scope

_log = get_logger(__name__)

#: Blob key. A 200-item ring already rewritten whole on every publish, so it
#: maps onto put() directly — there is nothing append-shaped about it.
NOTIFICATIONS_KEY = "notifications.json"
CAPACITY = 200
#: Tenants whose rings are held in memory at once. Dropping the least recently
#: used one loses nothing — every change is saved as it happens — so this only
#: bounds memory in a process that serves many tenants.
MAX_TENANTS = 64
#: Scale mode (F361): what changed, for every API server; and the lock a change is made under.
SHARED_CHANNEL = "synapse:notifications"
SHARED_LOCK = "synapse:notifications:lock:{tenant}"
SHARED_LOCK_MS = 10_000


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
        self._shared: _Shared | None = None

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

    def _items(self) -> list[dict]:
        """The current tenant's notifications: read from the blob in scale mode, where other processes change
        them (F361); this process's ring otherwise."""
        return self._load() if self._shared is not None else self._ring().items

    # ── scale mode (F361) ────────────────────────────────────────────────
    def share(self, redis, *, listen: bool) -> None:
        """Make the blob the ring, changed in turn by every process through `redis`. An API server `listen`s, so
        its streams hear every process's changes; a worker only writes. Call from the event loop that serves it."""
        self._shared = _Shared(self, redis, listen=listen)

    async def flush(self) -> None:
        """Wait until every change this process queued is written and announced."""
        if self._shared is not None:
            await self._shared.ops.join()

    async def unshare(self, timeout: float = 10.0) -> None:
        """Write what is queued (a worker shutting down), then stop the writer and the listener."""
        shared, self._shared = self._shared, None
        if shared is not None:
            await shared.close(timeout)

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
        fields = {
            "ts": time.time(),
            "kind": kind,          # human_input | tool_approval | run_completed | run_failed
            "title": title[:200],
            "body": (body or "")[:500],
            "run_id": run_id,
            "data": data or {},
        }
        if self._shared is not None:  # the ring's writer gives it its id
            self._shared.submit({"op": "publish", "fields": fields})
            item = {"id": None, **fields}
            self._dispatch_messaging(item)
            return item
        ring = self._ring()
        item = {"id": ring.next_id, **fields}
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
        return [i for i in self._items() if i["id"] > after_id]

    def last_id(self) -> int:
        if self._shared is not None:
            items = self._items()
            return items[-1]["id"] if items else 0
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
            # A team's questions are settled one by one, below (TEAM1e): one still asked stays on the bell.
            self._resolve_where({"kind": "human_input", "run_id": run_id, "plain": True}, "answered")
        elif etype == "human_input_required" and event.get("questions"):
            # A team's step asks each waiting member's question as its own item (TEAM1e, TEAM1c's shape), once:
            # one asked again by a later pause of the step is still on the bell.
            name = await self._orch_name(run_id)
            open_ids = {(i.get("data") or {}).get("question_id") for i in self._items()
                        if i.get("kind") == "human_input" and i.get("run_id") == run_id and not i.get("resolved")}
            for q in event["questions"]:
                if q.get("question_id") in open_ids:
                    continue
                from core.chat_approval import approval_prompt
                self.publish(
                    "human_input", f"{name}: {q.get('member_name') or 'a member'} needs approval",
                    body=approval_prompt(q.get("tool_name"), q.get("category")),
                    run_id=run_id,
                    data={"step_id": event.get("orch_step_id"), "question_id": q.get("question_id"),
                          "member_id": q.get("member_id"), "member_name": q.get("member_name"),
                          "approval_id": q.get("approval_id"), "tool_name": q.get("tool_name")},
                )
        elif etype in ("member_answered", "tool_approval_resolved") and event.get("question_id"):
            self._resolve_where({"kind": "human_input", "run_id": run_id, "question_id": event["question_id"]},
                                "answered")
        elif etype == "member_finished" and event.get("member_id"):
            # A member that ends — its question unanswered with its step — asks nothing any more.
            self._resolve_where({"kind": "human_input", "run_id": run_id, "member_id": event["member_id"]},
                                "cancelled" if event.get("status") == "cancelled" else "answered")
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
        self._resolve_where({"kind": "human_input", "run_id": run_id}, how)

    def resolve_approval(self, approval_id: str | None, how: str, *, question_id: str | None = None) -> None:
        """Mark a scheduled run's tool-approval item resolved (F268): `allowed`,
        `refused`, or `expired` when nobody answered or a newer run replaced it.
        Its Approve / Refuse go away on every open bell. A team's question is its
        own item (TEAM1c): `question_id` resolves that one, never another member's
        asking the same call."""
        if question_id and question_id != approval_id:
            self._resolve_where({"kind": "tool_approval", "question_id": question_id}, how)
            return
        if not approval_id:
            return
        self._resolve_where({"kind": "tool_approval", "approval_id": approval_id}, how)

    def _resolve_where(self, match: dict, how: str) -> None:
        if self._shared is not None:
            self._shared.submit({"op": "resolve", "match": match, "how": how})
            return
        self._resolve([item for item in self._ring().items if _matches(item, match)], how)

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
                i["run_id"] for i in self._items()
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


def _matches(item: dict, match: dict) -> bool:
    """Whether `item` is an unresolved one a resolve names: a run's human input, or a tool approval."""
    if item.get("resolved") or item.get("kind") != match["kind"]:
        return False
    if match["kind"] == "human_input":
        if item.get("run_id") != match["run_id"]:
            return False
        data = item.get("data") or {}
        # A team step's items, one per question (TEAM1e): settled by their question, or their member.
        if "question_id" in match:
            return data.get("question_id") == match["question_id"]
        if "member_id" in match:
            return bool(data.get("question_id")) and data.get("member_id") == match["member_id"]
        if match.get("plain"):
            return not data.get("question_id")
        return True
    if "question_id" in match:
        return (item.get("data") or {}).get("question_id") == match["question_id"]
    return (item.get("data") or {}).get("approval_id") == match["approval_id"]


class _Shared:
    """Scale mode's ring (F361): one writer per process, and, on an API server, a listener.

    The writer takes each queued change in turn under the change's own tenant:
    the tenant's Redis lock, the blob read, the change applied (a new item's id
    is the ring's next), the blob written, then what changed PUBLISHed. A resolve
    that names nothing is dropped after one read, without the lock: most of them
    (every step of a run asks) name nothing. If the lock cannot be had in its own
    lifetime, the change is made without it and that is logged."""

    def __init__(self, hub: NotificationHub, redis, *, listen: bool):
        self.hub = hub
        self.redis = redis
        self.ops: asyncio.Queue = asyncio.Queue()
        self.listening = asyncio.Event()
        self.writer = asyncio.create_task(self._write(), name="notifications-writer")
        self.listener = asyncio.create_task(self._listen(), name="notifications-listener") if listen else None

    def submit(self, op: dict) -> None:
        self.ops.put_nowait({**op, "tenant": get_tenant()})

    async def close(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self.ops.join(), timeout)
        except TimeoutError:
            _log.warning(f"notifications: {self.ops.qsize()} change(s) not written before shutdown")
        for task in (self.writer, self.listener):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _write(self) -> None:
        while True:
            op = await self.ops.get()
            try:
                scope = tenant_scope(op["tenant"]) if op["tenant"] != get_tenant() else contextlib.nullcontext()
                with scope:
                    await self._apply(op)
            except Exception as e:  # noqa: BLE001 — a notification never fails the work that raised it
                _log.warning(f"notifications: could not write a change to the shared ring: {e}")
            finally:
                self.ops.task_done()

    async def _apply(self, op: dict) -> None:
        hub = self.hub
        if op["op"] == "resolve" and not any(
                _matches(item, op["match"]) for item in await asyncio.to_thread(hub._load)):
            return
        key, token = SHARED_LOCK.format(tenant=op["tenant"]), uuid.uuid4().hex
        locked = await self._lock(key, token)
        try:
            ring = _Ring(await asyncio.to_thread(hub._load))
            if op["op"] == "publish":
                changed = [{"id": ring.next_id, **op["fields"]}]
                ring.items.append(changed[0])
                ring.items = ring.items[-hub._capacity:]
            else:
                changed = [item for item in ring.items if _matches(item, op["match"])]
                for item in changed:
                    item["resolved"] = True
                    item["resolved_as"] = op["how"]
                if not changed:
                    return
            await asyncio.to_thread(hub._persist, ring)
        finally:
            if locked:
                await self._unlock(key, token)
        await self.redis.publish(SHARED_CHANNEL, json.dumps({"tenant": op["tenant"], "items": changed},
                                                            default=str))

    async def _lock(self, key: str, token: str) -> bool:
        deadline = time.monotonic() + SHARED_LOCK_MS / 1000
        while time.monotonic() < deadline:
            if await self.redis.set(key, token, nx=True, px=SHARED_LOCK_MS):
                return True
            await asyncio.sleep(0.02)
        _log.warning(f"notifications: {key} stayed held for {SHARED_LOCK_MS} ms; writing without it")
        return False

    async def _unlock(self, key: str, token: str) -> None:
        held = await self.redis.get(key)
        if (held.decode() if isinstance(held, bytes) else held) == token:  # never another writer's
            await self.redis.delete(key)

    async def _listen(self) -> None:
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(SHARED_CHANNEL)
                self.listening.set()
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    change = json.loads(message["data"])
                    for queue in list(self.hub._subs.get(change["tenant"], ())):
                        for item in change["items"]:
                            queue.put_nowait(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — Redis went away: listen again
                _log.warning(f"notifications: lost {SHARED_CHANNEL}, listening again: {e}")
                await asyncio.sleep(1)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


hub = NotificationHub()

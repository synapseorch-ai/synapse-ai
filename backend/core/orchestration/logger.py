"""
Plain-text debug logging for orchestration runs.
Appends human-readable entries to a per-run .log file.

Writes go through the journal's single writer thread (`core.orchestration
.journal._executor`): one thread, so lines land in order; off the loop, so an
open/write/close per line — this used to do all three, ~30 call sites per
run — is not charged to the run that happens to be executing. `aclose` waits
for the writer before publishing the finished log; `close` does the same for
callers that cannot await.
"""
import asyncio
import json
import time
from concurrent.futures import Future
from pathlib import Path

from core.log import get_logger

_log = get_logger(__name__)

#: Blob key prefix for finished logs. Tenant-scoped by the blob store itself —
#: this used to be a flat `logs/orchestration/` prefix on a shared bucket, so
#: two tenants with the same run_id overwrote each other and list_logs()
#: returned everyone's runs, user_input included.
_BLOB_PREFIX = "logs/orchestration"


def _logs_dir() -> Path:
    """Where a run's log is appended while it is still running."""
    from core.storage.scratch import scratch_dir
    return scratch_dir("orchestration_logs")


def _blob_key(run_id: str) -> str:
    return f"{_BLOB_PREFIX}/{run_id}.log"


def _meta_key(run_id: str) -> str:
    """Sidecar for the fields list_logs() shows without reading whole logs.

    S3 object metadata did this before; BlobStore has no metadata API, and a
    sidecar blob behaves identically on both backends and comes back in the
    same list() call.
    """
    return f"{_BLOB_PREFIX}/{run_id}.meta.json"


def _parse_head(head: str) -> dict:
    def _extract(label: str) -> str:
        for line in head.split("\n"):
            if label in line:
                return line.split(":", 1)[1].strip()
        return ""

    return {
        "orchestration_name": _extract("Orchestration   :"),
        "orchestration_id": _extract("Orchestration ID:"),
        "session_id": _extract("Session ID      :"),
        "started_at": _extract("Started at      :"),
        "user_input": _extract("User Input      :")[:200],
    }


def meta_for(text: str) -> dict:
    """The `.meta.json` sidecar for a log body.

    `file_size_kb` is in here rather than computed by `list_logs()` because a
    blob store cannot cheaply stat an object — the listing hardcoded `0` and
    every finished run has shown `0KB` since logs moved to blobs in D29. The
    size is known at the moment the body is written, so it is written then.

    Shared with `core/store/log_importer.py` so a migrated log lists exactly
    like a freshly written one.
    """
    return {
        **_parse_head(text[:1000]),
        "file_size_kb": round(len(text.encode("utf-8")) / 1024, 1),
    }


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _fmt_args(args) -> str:
    """Format tool arguments for log output."""
    try:
        return json.dumps(args, indent=2, default=str)
    except Exception:
        return str(args)


class OrchestrationLogger:
    """Appends debug lines to  data/orchestration_logs/<run_id>.log"""

    def __init__(self, run_id: str, orchestration_id: str, orchestration_name: str, user_input: str, session_id: str | None = None):
        self.run_id = run_id
        self.session_id = session_id
        self.path = _logs_dir() / f"{run_id}.log"
        self._start_time = time.time()
        self._fh = None                 # writer-thread owned
        self._pending: Future | None = None

        self._write(f"""
{'='*80}
  ORCHESTRATION RUN LOG
{'='*80}
  Run ID          : {run_id}
  Orchestration ID: {orchestration_id}
  Orchestration   : {orchestration_name}
  Session ID      : {session_id or ''}
  Started at      : {_ts()}
  User Input      : {user_input or '(empty)'}
{'='*80}
""")

    # ── Core write ─────────────────────────────────────────────────

    def _write(self, text: str):
        from core.orchestration.journal import _executor

        def guarded():
            try:
                if self._fh is None:
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(text)
                self._fh.flush()
            except Exception as e:  # noqa: BLE001 — never raise on the writer thread
                _log.warning(f"run log write failed for '{self.run_id}': {e}")

        self._pending = _executor().submit(guarded)

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # ── Run lifecycle ──────────────────────────────────────────────

    def run_end(self, status: str):
        elapsed = round(time.time() - self._start_time, 2)
        self._write(f"""
{'='*80}
  RUN FINISHED
  Status   : {status}
  Ended at : {_ts()}
  Duration : {elapsed}s
{'='*80}
""")

    async def aclose(self) -> None:
        """Wait for the writer, close the file, publish. For the engine."""
        from core.orchestration.journal import _executor
        if self._pending is not None:
            await asyncio.get_running_loop().run_in_executor(_executor(), self._close_fh)
            self._pending = None
        self._publish()

    def close(self) -> None:
        """`aclose` for a caller that cannot await: blocks until the writer has
        landed every line, then publishes."""
        from core.orchestration.journal import _executor
        if self._pending is not None:
            _executor().submit(self._close_fh).result()
            self._pending = None
        self._publish()

    def _publish(self) -> None:
        """Publish the finished log to the blob store.

        One put() at the end rather than a write per line: put() replaces the
        whole object, so appending through it would be quadratic — and on S3,
        quadratic over the network.
        """
        try:
            if not self.path.exists():
                return
            from core.storage import get_blob_store

            text = self.path.read_text(encoding="utf-8")
            store = get_blob_store()
            store.put(_blob_key(self.run_id), text)
            store.put(_meta_key(self.run_id), json.dumps(meta_for(text)))
        except Exception:
            pass

    # ── Step lifecycle ─────────────────────────────────────────────

    def step_start(self, step_id: str, step_name: str, step_type: str,
                   agent_id: str | None = None, agent_name: str | None = None):
        agent_line = f"  Agent          : {agent_name} ({agent_id})" if agent_id else ""
        self._write(f"""
{'─'*80}
▶ STEP START: {step_name}
  Step ID        : {step_id}
  Type           : {step_type}
{agent_line}
  Time           : {_ts()}
{'─'*80}
""")

    def step_end(self, step_id: str, status: str = "completed", error: str | None = None):
        err_line = f"\n  Error          : {error}" if error else ""
        self._write(f"""
◼ STEP END: {step_id}
  Status         : {status}{err_line}
  Time           : {_ts()}
""")

    # ── Event logging ──────────────────────────────────────────────

    def log_event(self, event: dict):
        """Process an SSE event and write relevant info to the log."""
        etype = event.get("type", "")

        if etype == "_log_prompt":
            prompt = event.get("prompt", "")
            system_prompt_extra = event.get("system_prompt_extra", "")
            self._write(f"""
  📝 INPUT PROMPT:
{self._indent(prompt)}
""")
            if system_prompt_extra:
                self._write(f"""  🗺  SYSTEM CONTEXT (workflow graph + step position):
{self._indent(system_prompt_extra)}
""")

        elif etype == "_log_llm_call":
            turn = event.get("turn", "?")
            model = event.get("model", "")
            sys_chars = event.get("system_chars", 0)
            prompt_chars = event.get("prompt_chars", 0)
            mem_chars = event.get("memory_chars", 0)
            hist_turns = event.get("history_turns", 0)
            total_chars = event.get("total_chars", 0)
            prompt_text = event.get("prompt", "")
            sys_text = event.get("system_prompt", "")

            MAX_SYS_LOG = 3000
            sys_display = sys_text if len(sys_text) <= MAX_SYS_LOG else (
                sys_text[:MAX_SYS_LOG] + f"\n    [...truncated {len(sys_text) - MAX_SYS_LOG:,} chars]"
            )

            self._write(f"""
{'═'*80}
  🔄 LLM CALL — TURN {turn}  │  model: {model}
{'─'*80}
  System Prompt   : {sys_chars:>10,} chars  (~{sys_chars // 4:,} tokens est.)
  Context/Prompt  : {prompt_chars:>10,} chars  (~{prompt_chars // 4:,} tokens est.)
  Memory Context  : {mem_chars:>10,} chars
  History Turns   : {hist_turns:>10} turns
  ── TOTAL ───────: {total_chars:>10,} chars  (~{total_chars // 4:,} tokens est.)
{'─'*80}
  SYSTEM PROMPT ({sys_chars:,} chars):
{self._indent(sys_display)}

  CONTEXT / PROMPT SENT ({prompt_chars:,} chars):
{self._indent(prompt_text)}
{'═'*80}
""")

        elif etype == "_log_evaluator":
            prompt = event.get("prompt", "")
            response = event.get("llm_response", "")
            self._write(f"""
  📝 EVALUATOR PROMPT:
{self._indent(prompt)}

  🤖 EVALUATOR LLM RESPONSE:
{self._indent(response)}
""")

        elif etype == "_log_tool_step_llm":
            prompt = event.get("prompt", "")
            response = event.get("llm_response", "")
            self._write(f"""
  📝 TOOL STEP LLM CALL:
{self._indent(prompt)}

  🤖 LLM RESPONSE (tool call JSON):
{self._indent(response)}
""")

        elif etype == "tool_execution":
            tool_name = event.get("tool_name", "")
            args = event.get("args", {})
            self._write(f"""
  🔧 TOOL CALL: {tool_name}
     Arguments:
{self._indent(_fmt_args(args), 6)}
""")

        elif etype == "tool_result":
            tool_name = event.get("tool_name", "")
            preview = event.get("preview", "")
            self._write(f"""
  📤 TOOL RESULT: {tool_name}
     Preview: {preview}
""")

        elif etype == "llm_reasoning":
            reasoning = event.get("reasoning", "")
            turn = event.get("turn", "")
            self._write(f"""
  💭 REASONING (turn {turn}):
{self._indent(reasoning)}
""")

        elif etype == "llm_thought":
            thought = event.get("thought", "")
            turn = event.get("turn", "")
            self._write(f"""
  🛠️  ACTION (turn {turn}):
{self._indent(thought)}
""")

        elif etype == "final":
            response = event.get("response", "")
            self._write(f"""
  ✅ AGENT RESPONSE:
{self._indent(response)}
""")

        elif etype == "evaluator_decision":
            # The evaluator step's own event: the route the model picked, and
            # why. The router's `routing_decision` below says where that
            # actually led. This used to share that name and carry neither of
            # its fields.
            tool_name = event.get("tool_name", "")
            label = tool_name.replace("route_", "", 1) if tool_name.startswith("route_") else tool_name
            reasoning = event.get("arguments", {}).get("reasoning", "")
            self._write(f"  🔀 EVALUATOR: {label or '(no route)'}"
                        f"{f' — {reasoning}' if reasoning else ''}\n")

        elif etype == "routing_decision":
            # One event, three routing step types; `step_type` says which one
            # routed. Dispatch on it *before* reading `decision`, because a
            # `None` decision means two different things: an IF whose step
            # never set its state key, and a SWITCH that matched no case.
            #
            # Its absence means an archived run from before the rename, when
            # only the evaluator used this name — so no `step_type` is an
            # evaluator.
            step_type = event.get("step_type") or "evaluator"
            target = event.get("target_step_id")

            if step_type == "if_else":
                # `or` and not `??` — an unset decision reads as false, which
                # is the branch the engine actually takes.
                self._write(
                    f"  ↪ BRANCH: {event.get('decision') or 'false'}"
                    f" → {target or 'end'}\n")

            elif step_type == "switch":
                # `is not None`, because a matched case can be a falsy string.
                matched = event.get("decision")
                self._write(
                    f"  ↪ BRANCH: case {matched if matched is not None else 'default'}"
                    f" → {target or 'end'}\n")

            else:
                decision = event.get("decision", "")
                reasoning = event.get("reasoning", "")
                tool_name = event.get("tool_name", "")
                label = decision or (tool_name.replace("route_", "", 1) if tool_name.startswith("route_") else tool_name)
                self._write(f"""
  🔀 ROUTING DECISION: {label}
     Target Step  : {target or '(end orchestration)'}
     Reasoning    : {reasoning}
""")

        elif etype == "parallel_start":
            self._write(f"  ⚡ PARALLEL START: {event.get('branch_count', 0)} branches\n")

        elif etype == "parallel_complete":
            self._write(f"  ⚡ PARALLEL COMPLETE\n")

        elif etype == "branch_start":
            self._write(f"  ├─ BRANCH {event.get('branch_index', 0)} START\n")

        elif etype == "branch_end":
            self._write(f"  └─ BRANCH END\n")

        elif etype == "loop_iteration":
            # A while-loop has no total to count towards.
            total = event.get("total")
            of_total = f"/{total}" if total is not None else ""
            self._write(f"  🔄 LOOP ITERATION {event.get('iteration', 0)}{of_total}\n")

        elif etype == "loop_complete":
            reason = event.get("stopped_reason")
            why = f", stopped: {reason}" if reason else ""
            self._write(f"  🔄 LOOP COMPLETE ({event.get('iterations_completed', 0)} iterations{why})\n")

        elif etype == "merge_complete":
            self._write(f"  🔗 MERGE COMPLETE: {event.get('input_count', 0)} inputs, strategy='{event.get('strategy', 'list')}'\n")

        elif etype == "transform_result":
            result = event.get("result", "")
            self._write(f"  ⚙️ TRANSFORM RESULT: {str(result)}\n")

        elif etype == "human_input_required":
            self._write(f"  ⏸️ HUMAN INPUT REQUIRED: {event.get('prompt', '')}\n")

        elif etype == "step_start":
            step_id = event.get("orch_step_id", "")
            self._write(f"    ▶ SUB-STEP: {event.get('step_name', '')} ({event.get('step_type', '')}) [{step_id}]\n")

        elif etype == "step_complete":
            self._write(f"    ◼ SUB-STEP DONE: {event.get('step_name', '')} ({event.get('duration_seconds', '')}s)\n")

        elif etype == "step_error":
            self._write(f"    ❌ STEP ERROR: {event.get('error', '')}\n")

        elif etype == "step_warning":
            # Every warning the engine emits was missing from the transcript —
            # a condition that failed to evaluate, a `loop_over` that resolved
            # to nothing, a loop that hit its ceiling. The stream carried them
            # and the archive did not, so the one record that outlives the run
            # showed a branch being taken and never said the expression behind
            # it had not run.
            self._write(f"  ⚠️ WARNING: {event.get('message', '')}\n")

        elif etype == "if_decision":
            # The step executor's own event: the condition and what it came to.
            # `_resolve_next` used to answer to this name as well, with a
            # payload sharing no field; it emits `routing_decision` now. The
            # router branch below stays for good — a run journalled before the
            # rename replays its raw events, so the old shape still arrives.
            if "condition" in event:
                self._write(
                    f"  🔀 IF: {event.get('condition', '')} → {event.get('result', '')}\n")
            else:
                self._write(
                    f"  ↪ BRANCH: {event.get('decision') or 'false'}"
                    f" → {event.get('target_step_id') or 'end'}\n")

        elif etype == "switch_decision":
            # As above: the step's own event, plus a legacy router branch for
            # runs journalled before `_resolve_next` moved to `routing_decision`.
            if "expression" in event:
                matched = event.get("matched_case")
                self._write(
                    f"  🔀 SWITCH: {event.get('expression', '')} → {event.get('value', '')}"
                    f" (case: {matched if matched is not None else 'default'})\n")
            else:
                matched = event.get("matched_case")
                self._write(
                    f"  ↪ BRANCH: case {matched if matched is not None else 'default'}"
                    f" → {event.get('target_step_id') or 'end'}\n")

        elif etype == "orchestration_error":
            self._write(f"\n  ❌ ORCHESTRATION ERROR: {event.get('error', '')}\n")

        elif etype == "loop_limit_reached":
            self._write(f"  ⚠️ LOOP LIMIT: step {event.get('orch_step_id', '')} hit {event.get('iterations', 0)}/{event.get('max_iterations', 0)}\n")

        elif etype == "thinking":
            pass  # skip noise

        elif etype == "error":
            self._write(f"  ❌ ERROR: {event.get('message', '')}\n")

        elif etype == "context_compact":
            stage = event.get("stage", "unknown").upper()
            cb = event.get("chars_before", 0)
            ca = event.get("chars_after", 0)
            pct = event.get("reduction_pct", 0)
            archive = event.get("archive_path")
            archive_line = f"\n  Archive  : {archive}" if archive else ""
            sep = "=" * 60
            self._write(f"""
{sep}
  CONTEXT COMPACTED [{stage}]
  Before   : {cb:>12,} chars  (~{cb // 4:,} tokens est.)
  After    : {ca:>12,} chars  (~{ca // 4:,} tokens est.)
  Saved    : {cb - ca:>12,} chars  (-{pct}%){archive_line}
{sep}
""")

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _indent(text: str, spaces: int = 4) -> str:
        prefix = " " * spaces
        return "\n".join(f"{prefix}{line}" for line in text.split("\n"))

    # ── Query helpers (for API endpoints) ──────────────────────────

    @staticmethod
    def get_log(run_id: str) -> str | None:
        path = _logs_dir() / f"{run_id}.log"
        if path.exists():
            return path.read_text(encoding="utf-8")
        try:
            from core.storage import get_blob_store
            return get_blob_store().get(_blob_key(run_id))
        except Exception:
            pass
        return None

    @staticmethod
    def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
        """Runs still in flight (scratch) plus finished ones (blobs).

        Everything here is scoped to the current tenant: the scratch directory
        carries the tenant in its path and the blob store applies the prefix
        inside list(). Neither used to be true.
        """
        logs: list[dict] = []
        seen: set[str] = set()

        logs_dir = _logs_dir()
        for f in sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
            seen.add(f.stem)
            try:
                entry = _parse_head(f.read_text(encoding="utf-8", errors="replace")[:1000])
                entry["file_size_kb"] = round(f.stat().st_size / 1024, 1)
            except OSError:
                entry = {"file_size_kb": 0}
            logs.append({"run_id": f.stem, **entry})

        try:
            from core.storage import get_blob_store
            store = get_blob_store()
            for key in store.list(f"{_BLOB_PREFIX}/"):
                if not key.endswith(".log"):
                    continue
                run_id = Path(key).stem
                if run_id in seen:
                    continue
                seen.add(run_id)
                # The sidecar, so listing does not download every log body.
                entry = {}
                try:
                    raw = store.get(_meta_key(run_id))
                    if raw:
                        entry = json.loads(raw)
                except (ValueError, OSError):
                    pass
                logs.append({"run_id": run_id, "file_size_kb": 0, **entry})
        except Exception:
            pass

        logs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return logs[offset: offset + limit]

    @staticmethod
    def delete_log(run_id: str) -> bool:
        path = _logs_dir() / f"{run_id}.log"
        deleted = False
        if path.exists():
            path.unlink()
            deleted = True
        try:
            from core.storage import get_blob_store
            store = get_blob_store()
            if store.exists(_blob_key(run_id)):
                store.delete(_blob_key(run_id))
                store.delete(_meta_key(run_id))
                deleted = True
        except Exception:
            pass
        return deleted

"""
Plain-text debug logging for individual agent runs.
Logs each call to an agent (from chat or orchestration) including all tools
used and their responses, in the same terminal-style format as orchestration logs.
"""
import json
import queue
import threading
import time
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs" / "agent_logs"


def _ensure_logs_dir():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _fmt_args(args) -> str:
    try:
        return json.dumps(args, indent=2, default=str)
    except Exception:
        return str(args)


class AgentLogger:
    """Appends debug lines to logs/agent_logs/<run_id>.log for a single agent execution."""

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        session_id: str,
        source: str,
        user_message: str,
    ):
        _ensure_logs_dir()
        # run_id encodes the agent and timestamp for easy identification
        short_id = agent_id.replace("agent_", "") if agent_id.startswith("agent_") else agent_id
        self.run_id = f"agentrun_{short_id}_{int(time.time() * 1000)}"
        self.path = LOGS_DIR / f"{self.run_id}.log"
        self._start_time = time.time()
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._drain, daemon=True, name=f"agent-log-{self.run_id}")
        self._thread.start()

        self._write(f"""
{'='*80}
  AGENT RUN LOG
{'='*80}
  Run ID          : {self.run_id}
  Agent ID        : {agent_id}
  Agent Name      : {agent_name}
  Session ID      : {session_id}
  Source          : {source}
  Started at      : {_ts()}
  User Input      : {user_message}
{'='*80}
""")

    # ── Core write ─────────────────────────────────────────────────

    def _write(self, text: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def _write_bg(self, text: str):
        """Fire-and-forget: enqueue for the background writer thread."""
        self._q.put(text)

    def _drain(self):
        """Background thread: drains the write queue in order."""
        while True:
            text = self._q.get()
            if text is None:
                break
            try:
                self._write(text)
            except Exception:
                pass

    # ── Run lifecycle ──────────────────────────────────────────────

    def run_end(self, status: str):
        elapsed = round(time.time() - self._start_time, 2)
        self._write_bg(f"""
{'='*80}
  AGENT RUN FINISHED
  Status   : {status}
  Ended at : {_ts()}
  Duration : {elapsed}s
{'='*80}
""")

    def close(self) -> None:
        """Drain the write queue, then upload the completed log to S3 (scale mode)."""
        self._q.put(None)  # poison pill — stops the drain thread
        self._thread.join(timeout=10)
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3 and self.path.exists():
                head = self.path.read_text(encoding="utf-8", errors="replace")[:1000]

                def _extract(label: str) -> str:
                    for line in head.split("\n"):
                        if label in line:
                            return line.split(":", 1)[1].strip()
                    return ""

                s3.upload_text(
                    f"logs/agent/{self.path.name}",
                    self.path.read_text(encoding="utf-8"),
                    metadata={
                        "agent_name": _extract("Agent Name      :"),
                        "agent_id": _extract("Agent ID        :"),
                        "source": _extract("Source          :"),
                        "session_id": _extract("Session ID      :"),
                        "started_at": _extract("Started at      :"),
                        "user_input": _extract("User Input      :")[:200],
                    },
                )
        except Exception:
            pass  # S3 upload failure must never surface to callers

    # ── Event logging ──────────────────────────────────────────────

    def log_event(self, event: dict):
        """Process an SSE event and write relevant info to the log."""
        etype = event.get("type", "")

        if etype == "_log_prompt":
            prompt = event.get("prompt", "")
            self._write_bg(f"""
{'─'*80}
  📝 INPUT PROMPT:
{self._indent(prompt)}
{'─'*80}
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

            self._write_bg(f"""
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

        elif etype == "tool_execution":
            tool_name = event.get("tool_name", "")
            args = event.get("args", {})
            self._write_bg(f"""
  🔧 TOOL CALL: {tool_name}
     Arguments:
{self._indent(_fmt_args(args), 6)}
""")

        elif etype == "tool_result":
            tool_name = event.get("tool_name", "")
            preview = event.get("preview", "")
            self._write_bg(f"""
  📤 TOOL RESULT: {tool_name}
     Preview: {preview}
""")

        elif etype == "llm_thought":
            thought = event.get("thought", "")
            turn = event.get("turn", "")
            self._write_bg(f"""
  🧠 LLM THOUGHT (turn {turn}):
{self._indent(thought)}
""")

        elif etype == "final":
            response = event.get("response", "")
            self._write_bg(f"""
  ✅ AGENT RESPONSE:
{self._indent(response)}
""")

        elif etype == "error":
            self._write_bg(f"\n  ❌ ERROR: {event.get('message', '')}\n")

        elif etype == "context_compact":
            stage = event.get("stage", "unknown").upper()
            cb = event.get("chars_before", 0)
            ca = event.get("chars_after", 0)
            pct = event.get("reduction_pct", 0)
            archive = event.get("archive_path")
            archive_line = f"\n  Archive  : {archive}" if archive else ""
            sep = "=" * 60
            self._write_bg(f"""
{sep}
  CONTEXT COMPACTED [{stage}]
  Before   : {cb:>12,} chars  (~{cb // 4:,} tokens est.)
  After    : {ca:>12,} chars  (~{ca // 4:,} tokens est.)
  Saved    : {cb - ca:>12,} chars  (-{pct}%){archive_line}
{sep}
""")

        elif etype == "thinking":
            pass  # skip noise

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _indent(text: str, spaces: int = 4) -> str:
        prefix = " " * spaces
        return "\n".join(f"{prefix}{line}" for line in text.split("\n"))

    # ── Query helpers (for API endpoints) ──────────────────────────

    @staticmethod
    def get_log(run_id: str) -> str | None:
        path = LOGS_DIR / f"{run_id}.log"
        if path.exists():
            return path.read_text(encoding="utf-8")
        # Fall back to S3 for logs written by other workers
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                return s3.download_text(f"logs/agent/{run_id}.log")
        except Exception:
            pass
        return None

    @staticmethod
    def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
        _ensure_logs_dir()

        def _parse_head(head: str, run_id: str, size_kb: float) -> dict:
            def _extract(label: str) -> str:
                for line in head.split("\n"):
                    if label in line:
                        return line.split(":", 1)[1].strip()
                return ""
            return {
                "run_id": run_id,
                "agent_name": _extract("Agent Name      :"),
                "agent_id": _extract("Agent ID        :"),
                "source": _extract("Source          :"),
                "session_id": _extract("Session ID      :"),
                "started_at": _extract("Started at      :"),
                "user_input": _extract("User Input      :")[:200],
                "file_size_kb": size_kb,
            }

        # Collect local entries
        local_ids: set[str] = set()
        logs: list[dict] = []
        files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            run_id = f.stem
            local_ids.add(run_id)
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:1000]
                logs.append(_parse_head(head, run_id, round(f.stat().st_size / 1024, 1)))
            except Exception:
                logs.append({"run_id": run_id, "file_size_kb": 0})

        # Merge S3 entries that aren't already local
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                from concurrent.futures import ThreadPoolExecutor
                s3_keys = s3.list_keys("logs/agent/")
                missing_keys = [k for k in s3_keys if k.endswith(".log") and Path(k).stem not in local_ids]

                def _fetch_meta(rel_key: str) -> dict:
                    run_id = Path(rel_key).stem
                    try:
                        meta = s3.get_metadata(rel_key) or {}
                        return {
                            "run_id": run_id,
                            "agent_name": meta.get("agent_name", ""),
                            "agent_id": meta.get("agent_id", ""),
                            "source": meta.get("source", ""),
                            "session_id": meta.get("session_id", ""),
                            "started_at": meta.get("started_at", ""),
                            "user_input": meta.get("user_input", "")[:200],
                            "file_size_kb": 0,
                        }
                    except Exception:
                        return {"run_id": run_id}

                with ThreadPoolExecutor(max_workers=10) as pool:
                    s3_entries = list(pool.map(_fetch_meta, missing_keys))
                logs.extend(s3_entries)
        except Exception:
            pass

        # Sort by started_at descending (local files already sorted; blend with S3)
        logs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return logs[offset: offset + limit]

    @staticmethod
    def delete_log(run_id: str) -> bool:
        path = LOGS_DIR / f"{run_id}.log"
        deleted = False
        if path.exists():
            path.unlink()
            deleted = True
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                s3.delete(f"logs/agent/{run_id}.log")
                deleted = True
        except Exception:
            pass
        return deleted

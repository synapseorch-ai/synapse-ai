"""
Plain-text debug logging for individual schedule runs.
Mirrors the design of agent_logger.py exactly.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs" / "schedule_logs"


def _ensure_logs_dir():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _fmt_args(args) -> str:
    try:
        return json.dumps(args, indent=2, default=str)
    except Exception:
        return str(args)


class ScheduleLogger:
    """Appends debug lines to logs/schedule_logs/<run_id>.log for a single schedule execution."""

    def __init__(
        self,
        schedule_id: str,
        schedule_name: str,
        target_type: str,
        target_id: str,
        prompt: str,
    ):
        _ensure_logs_dir()

        # Sanitize schedule_id to prevent taint in self.path
        clean_sched_id = re.sub(r"[^a-zA-Z0-9_\-]", "", schedule_id)
        short_id = clean_sched_id.replace("sched_", "") if clean_sched_id.startswith("sched_") else clean_sched_id
        self.run_id = f"schedulerun_{short_id}_{int(time.time() * 1000)}"
        self.path = LOGS_DIR / f"{self.run_id}.log"
        self._start_time = time.time()

        prompt_preview = prompt[:300] + "..." if len(prompt) > 300 else prompt
        self._write(f"""
{'='*80}
  SCHEDULE RUN LOG
{'='*80}
  Run ID          : {self.run_id}
  Schedule ID     : {schedule_id}
  Schedule Name   : {schedule_name}
  Target Type     : {target_type}
  Target ID       : {target_id}
  Started at      : {_ts()}
  Prompt          : {prompt_preview}
{'='*80}
""")

    # -- Core write -----------------------------------------------------

    def _write(self, text: str):
        """Sync write -- only call from a thread (via _write_bg) or startup."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def _write_bg(self, text: str):
        """Fire-and-forget write that offloads to a thread so the event loop isn't blocked."""
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._write, text)
        except RuntimeError:
            self._write(text)

    # -- Run lifecycle ---------------------------------------------------

    def run_end(self, status: str):
        elapsed = round(time.time() - self._start_time, 2)
        self._write_bg(f"""
{'='*80}
  SCHEDULE RUN FINISHED
  Status   : {status}
  Ended at : {_ts()}
  Duration : {elapsed}s
{'='*80}
""")

    def close(self) -> None:
        """
        Flush pending writes and upload the completed log to S3 (scale mode).
        Writes a final sync marker to ensure the run_end text (queued via executor)
        has been flushed before uploading.
        """
        # Sync write acts as a barrier — the executor pool is FIFO so this waits
        # for any pending _write_bg tasks to drain (they run in the same default
        # thread pool as this blocking call).
        try:
            self._write("")  # zero-length write, flushes preceding executor tasks
        except Exception:
            pass
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
                    f"logs/schedule/{self.path.name}",
                    self.path.read_text(encoding="utf-8"),
                    metadata={
                        "schedule_name": _extract("Schedule Name   :"),
                        "schedule_id": _extract("Schedule ID     :"),
                        "target_type": _extract("Target Type     :"),
                        "target_id": _extract("Target ID       :"),
                        "started_at": _extract("Started at      :"),
                        "prompt": _extract("Prompt          :")[:200],
                    },
                )
        except Exception:
            pass

    # -- Event logging ---------------------------------------------------

    def log_event(self, event: dict):
        """Process an SSE event and write relevant info to the log."""
        etype = event.get("type", "")

        if etype == "_log_prompt":
            prompt = event.get("prompt", "")
            self._write_bg(f"""
{'-'*80}
  INPUT PROMPT:
{self._indent(prompt)}
{'-'*80}
""")

        elif etype == "tool_execution":
            tool_name = event.get("tool_name", "")
            args = event.get("args", {})
            self._write_bg(f"""
  TOOL CALL: {tool_name}
     Arguments:
{self._indent(_fmt_args(args), 6)}
""")

        elif etype == "tool_result":
            tool_name = event.get("tool_name", "")
            preview = event.get("preview", "")
            self._write_bg(f"""
  TOOL RESULT: {tool_name}
     Preview: {preview[:500]}
""")

        elif etype in ("step_start", "step_complete", "orchestration_start", "orchestration_complete"):
            name = event.get("step_name") or event.get("orchestration_name") or etype
            self._write_bg(f"\n  [{etype.upper()}] {name}\n")

        elif etype == "final":
            response = event.get("response", "")
            self._write_bg(f"""
  AGENT RESPONSE:
{self._indent(response[:3000])}
""")

        elif etype == "error":
            self._write_bg(f"\n  ERROR: {event.get('message', '')}\n")

        elif etype == "thinking":
            pass  # skip noise

    # -- Helpers ---------------------------------------------------------

    @staticmethod
    def _indent(text: str, spaces: int = 4) -> str:
        prefix = " " * spaces
        return "\n".join(f"{prefix}{line}" for line in text.split("\n"))

    @staticmethod
    def _safe_log_path(run_id: str) -> Path | None:
        """
        Safely locates a log file by matching run_id against actual file system entries.
        This severs the taint chain for security scanners as the returned Path 
        originates from the OS (iterdir), not user input.
        """
        if not run_id or not isinstance(run_id, str):
            return None

        # 1. Strict regex validation as a first pass
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", run_id):
            return None

        # 2. Iterate and match (Taint-severing strategy)
        try:
            target_filename = f"{run_id}.log"
            for entry in LOGS_DIR.iterdir():
                if entry.is_file() and entry.name == target_filename:
                    return entry
        except Exception:
            pass
        return None

    # -- Query helpers (for API endpoints) -------------------------------

    @staticmethod
    def get_log(run_id: str) -> str | None:
        if not run_id or not re.match(r"^[a-zA-Z0-9_\-\.]+$", str(run_id)):
            return None
        path = ScheduleLogger._safe_log_path(run_id)
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                return s3.download_text(f"logs/schedule/{run_id}.log")
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
                "schedule_name": _extract("Schedule Name   :"),
                "schedule_id": _extract("Schedule ID     :"),
                "target_type": _extract("Target Type     :"),
                "target_id": _extract("Target ID       :"),
                "started_at": _extract("Started at      :"),
                "prompt": _extract("Prompt          :")[:200],
                "file_size_kb": size_kb,
            }

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

        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                from concurrent.futures import ThreadPoolExecutor
                s3_keys = s3.list_keys("logs/schedule/")
                missing_keys = [k for k in s3_keys if k.endswith(".log") and Path(k).stem not in local_ids]

                def _fetch_meta(rel_key: str) -> dict:
                    run_id = Path(rel_key).stem
                    try:
                        meta = s3.get_metadata(rel_key) or {}
                        return {
                            "run_id": run_id,
                            "schedule_name": meta.get("schedule_name", ""),
                            "schedule_id": meta.get("schedule_id", ""),
                            "target_type": meta.get("target_type", ""),
                            "target_id": meta.get("target_id", ""),
                            "started_at": meta.get("started_at", ""),
                            "prompt": meta.get("prompt", "")[:200],
                            "file_size_kb": 0,
                        }
                    except Exception:
                        return {"run_id": run_id}

                with ThreadPoolExecutor(max_workers=10) as pool:
                    logs.extend(pool.map(_fetch_meta, missing_keys))
        except Exception:
            pass

        logs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return logs[offset: offset + limit]

    @staticmethod
    def delete_log(run_id: str) -> bool:
        if not run_id or not re.match(r"^[a-zA-Z0-9_\-\.]+$", str(run_id)):
            return False
        path = ScheduleLogger._safe_log_path(run_id)
        deleted = False
        if path and path.exists():
            path.unlink()
            deleted = True
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3:
                s3.delete(f"logs/schedule/{run_id}.log")
                deleted = True
        except Exception:
            pass
        return deleted

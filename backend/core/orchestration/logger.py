"""
Plain-text debug logging for orchestration runs.
Appends human-readable entries to a per-run .log file.
"""
import json
import time
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent.parent / "logs" / "orchestration_logs"


def _ensure_logs_dir():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


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
        _ensure_logs_dir()
        self.run_id = run_id
        self.session_id = session_id
        self.path = LOGS_DIR / f"{run_id}.log"
        self._start_time = time.time()

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
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

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

        elif etype == "llm_thought":
            thought = event.get("thought", "")
            turn = event.get("turn", "")
            self._write(f"""
  🧠 LLM THOUGHT (turn {turn}):
{self._indent(thought)}
""")

        elif etype == "final":
            response = event.get("response", "")
            self._write(f"""
  ✅ AGENT RESPONSE:
{self._indent(response)}
""")

        elif etype == "routing_decision":
            decision = event.get("decision", "")
            target = event.get("target_step_id", "")
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
            self._write(f"  🔄 LOOP ITERATION {event.get('iteration', 0)}/{event.get('total', 0)}\n")

        elif etype == "loop_complete":
            self._write(f"  🔄 LOOP COMPLETE ({event.get('iterations_completed', 0)} iterations)\n")

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
        path = LOGS_DIR / f"{run_id}.log"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    @staticmethod
    def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
        _ensure_logs_dir()
        logs = []
        files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[offset : offset + limit]:
            run_id = f.stem
            # Parse header lines for summary
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:1000]
                def _extract(label: str) -> str:
                    for line in head.split("\n"):
                        if label in line:
                            return line.split(":", 1)[1].strip()
                    return ""

                logs.append({
                    "run_id": run_id,
                    "orchestration_name": _extract("Orchestration   :"),
                    "orchestration_id": _extract("Orchestration ID:"),
                    "session_id": _extract("Session ID      :"),
                    "started_at": _extract("Started at      :"),
                    "user_input": _extract("User Input      :")[:200],
                    "file_size_kb": round(f.stat().st_size / 1024, 1),
                })
            except Exception:
                logs.append({"run_id": run_id})
        return logs

    @staticmethod
    def delete_log(run_id: str) -> bool:
        path = LOGS_DIR / f"{run_id}.log"
        if path.exists():
            path.unlink()
            return True
        return False

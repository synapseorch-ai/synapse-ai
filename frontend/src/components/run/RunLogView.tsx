"use client";

/**
 * The run log's vocabulary, its renderer, and the reducer that fills it.
 *
 * Every surface that shows what a run did reads it through these: cloud's
 * builder `RunPanel` following the stream live, and both products' run pages
 * (M2b) replaying it. They were built weeks apart and drifted immediately — the
 * builder grew tool-call disclosure, result previews, step-result bubbles and
 * collapsible reasoning, while the run page printed `type: value` for every
 * event — so the vocabulary lives here once. `applyRunEvent` is a pure reducer
 * from an engine event to log state; `LogLine` is the renderer.
 *
 * Moved upstream from cloud's `components/RunLogView.tsx` in M2b, and off the raw
 * `zinc-*`/hue utilities onto the tokens: cyan for the machine acting, amber
 * for a person, red and green only for how a step ended. Reading a stream is
 * each product's (`RunHost.events`); cloud's builder keeps its own `consume`.
 *
 * The shapes are OSS `BottomPanel`'s, kept deliberately: moving between the
 * self-hosted product and cloud should not mean relearning the log.
 */

import {
  AlertTriangle,
  BookOpenText,
  Brain,
  Check,
  CornerDownRight,
  ExternalLink,
  GitBranch,
  GitFork,
  GitMerge,
  Info,
  Minimize2,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  Square as SquareIcon,
  Wrench,
  X as XIcon,
} from "lucide-react";
import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { RetrievalTrace } from "@/components/RetrievalTrace";
import { Spinner } from "@/components/ui";
import { DEGRADED, getQueryTrace, type SearchTrace } from "@/lib/knowledge";

import type { Task } from "./model";
import { emptyTeam, foldTeam, isTeamEvent, type TeamState } from "@/components/team/team";

// The OSS BottomPanel's log-entry union, kept shape-for-shape so the renderer
// below can stay a faithful port.
export type ToolCallEntry = { kind: "tool_call"; tool_name: string; args: Record<string, unknown>; step_name?: string };
export type ToolResultEntry = { kind: "tool_result"; tool_name: string; preview: string };
export type StepResultEntry = { kind: "step_result"; step_name: string; step_type: string; content: string };
export type ProseEntry = { kind: "reasoning" | "thought"; step_name?: string; content: string };
// A structural log line: the tone picks an icon and a colour in the renderer —
// icons where the emoji prefixes were, matching the OSS pass that removed them.
export type LineTone = "start" | "ok" | "error" | "pause" | "loop" | "flow" | "parallel" | "merge" | "end" | "info" | "warn" | "compact" | "branch" | "muted";
export type LineEntry = { kind: "line"; tone: LineTone; text: string };
// A Retrieve step's search (R1): its trace is stored by id and fetched when the row is opened.
export type RetrievalEntry = {
  kind: "retrieval";
  step_name?: string;
  query_id: string;
  hit_count: number;
  cost_usd: number;
  degraded: string[];
};
export type LogEntry =
  | string
  | ToolCallEntry
  | ToolResultEntry
  | StepResultEntry
  | ProseEntry
  | LineEntry
  | RetrievalEntry;

export const line = (tone: LineTone, text: string): LineEntry => ({ kind: "line", tone, text });

const LINE_META: Record<LineTone, { icon: React.FC<{ size?: number; className?: string }>; cls: string }> = {
  start:    { icon: Play,            cls: "text-accent-ink" },
  ok:       { icon: Check,           cls: "text-success" },
  error:    { icon: XIcon,           cls: "text-danger" },
  pause:    { icon: Pause,           cls: "text-person-ink" },
  loop:     { icon: RefreshCw,       cls: "text-accent-ink" },
  flow:     { icon: GitFork,         cls: "text-text-muted" },
  parallel: { icon: GitBranch,       cls: "text-text-muted" },
  merge:    { icon: GitMerge,        cls: "text-text-muted" },
  end:      { icon: SquareIcon,      cls: "text-text-muted" },
  info:     { icon: Info,            cls: "text-text-muted" },
  warn:     { icon: AlertTriangle,   cls: "text-text" },
  compact:  { icon: Minimize2,       cls: "text-text-muted" },
  branch:   { icon: CornerDownRight, cls: "pl-2 text-text-faint" },
  muted:    { icon: Info,            cls: "text-text-faint" },
};

/** Past this the oldest entries are dropped; a chatty agent must not grow without bound. */
export const MAX_LOG_LINES = 500;

/** Step types whose streamed `final` response is worth showing as a bubble. */
const CAPTURED_TYPES = new Set(["agent", "llm", "print", "extract_json"]);

export type RunLogState = {
  entries: LogEntry[];
  /**
   * The run's working plan. State rather than a log entry: `update_tasks`
   * submits the complete list every call, so the newest one is all of it and an
   * appended history of superseded copies is noise.
   */
  tasks: Task[];
  /**
   * Per-step capture of the streamed `final` response, flushed as a bubble on
   * `step_complete`. The OSS `pendingStepResultRef`.
   */
  pending: Map<string, StepResultEntry>;
  /**
   * What the model is doing *right now* — a one-line, transient signal, never a
   * log entry.
   *
   * The chatty events (`thinking`, and the tool it just reached for) are the
   * difference between a live run that looks stalled and one you can see moving,
   * and they are far too noisy to keep: twelve `thinking` events in a two-step
   * run, each superseding the last. So they land here and nowhere else, and
   * anything terminal clears it.
   */
  liveActivity: string | null;
  /**
   * A Team agent step's members (TEAM1b): its task list and one lane per member,
   * folded as chat folds them. Empty until a step runs a team (TEAM1e feeds it).
   */
  team: TeamState;
};

export function emptyRunLog(): RunLogState {
  return { entries: [], tasks: [], pending: new Map(), liveActivity: null, team: emptyTeam() };
}

function append(entries: LogEntry[], entry: LogEntry): LogEntry[] {
  return [...entries.slice(-(MAX_LOG_LINES - 1)), entry];
}

/**
 * A line the product writes itself, not the engine: OSS's editor says
 * "Human response submitted", "[Cancelled]", "[Connection lost]" and the like
 * into the same log, under the same cap.
 */
export function appendRunLog(state: RunLogState, entry: LogEntry): RunLogState {
  return { ...state, entries: append(state.entries, entry) };
}

/**
 * One engine event folded into the log. Pure: same events in, same log out,
 * whether they arrived over SSE a second ago or out of an S3 archive.
 *
 * Events with no log consequence — `chunk`, `token_usage`, `_step_progress` —
 * fall through and return the state untouched, deliberately rather than by
 * omission: they are either captured elsewhere (`final`) or genuinely not worth
 * a line.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
/** Step events a nested run forwards that belong to its own steps (F251). */
const NESTED_STEP_EVENTS = new Set(["step_start", "step_complete", "step_error", "final", "tasks_updated", "orchestration_error"]);

/**
 * A nested run's own step event (F251). It arrives under the step that called
 * it — `orch_step_id` and `step_name` are the parent's — with the child's run
 * and step beside them. Logged with both names, results held per child step,
 * and never the parent step's status: a child's step ending used to read as the
 * parent step ending, three times over.
 */
function applyNestedStepEvent(state: RunLogState, data: any, where: string): RunLogState {
  const key = `${data.nested_run_id}:${data.nested_step_id ?? ""}`;
  const { entries, pending } = state;
  switch (data.type) {
    case "step_start": {
      const next = new Map(pending);
      if (CAPTURED_TYPES.has(data.step_type)) {
        next.set(key, { kind: "step_result", step_name: where, step_type: data.step_type, content: "" });
      }
      return { ...state, pending: next, entries: append(entries, line("start", `${where} (${data.step_type})`)) };
    }
    case "final": {
      // The nested run's own end (its whole result) is not a step's answer.
      if (data.intent === "orchestration") return state;
      const held = pending.get(key);
      if (!held || !data.response) return state;
      const next = new Map(pending);
      next.set(key, { ...held, content: String(data.response) });
      return { ...state, pending: next };
    }
    case "step_complete": {
      const held = pending.get(key);
      const next = new Map(pending);
      next.delete(key);
      let out = entries;
      if (held && held.content.trim()) out = append(out, { ...held, content: held.content.trim() });
      out = append(
        out,
        line("ok", `${where} completed${data.duration_seconds != null ? ` (${data.duration_seconds.toFixed(1)}s)` : ""}`),
      );
      return { ...state, pending: next, entries: out };
    }
    case "step_error": {
      const next = new Map(pending);
      next.delete(key);
      return { ...state, pending: next, entries: append(entries, line("error", `${where}: ${data.reason || data.error}`)) };
    }
    default:
      // The child's own plan and its own end: the parent's step reports how it went.
      return state;
  }
}

export function applyRunEvent(state: RunLogState, data: any): RunLogState {
  if (data?.nested_run_id) {
    const where = `${data.step_name} › ${data.nested_step_name || "a nested step"}`;
    if (NESTED_STEP_EVENTS.has(data.type)) return applyNestedStepEvent(state, data, where);
    // Its thoughts and calls name the child's step too, not only the caller.
    data = { ...data, step_name: where };
  }
  if (isTeamEvent(data)) {
    // A member's own work is its lane's, never a log line of the step's.
    return { ...state, team: foldTeam(state.team ?? emptyTeam(), data) };
  }
  const { entries, pending } = state;
  const add = (entry: LogEntry): RunLogState => ({ ...state, entries: append(entries, entry) });

  switch (data?.type) {
    case "orchestration_start":
      return add(line("muted", `Started run ${data.run_id}`));
    case "step_start": {
      const next = new Map(pending);
      if (CAPTURED_TYPES.has(data.step_type)) {
        next.set(data.orch_step_id, {
          kind: "step_result",
          step_name: data.step_name,
          step_type: data.step_type,
          content: "",
        });
      } else {
        next.delete(data.orch_step_id);
      }
      return {
        ...state,
        pending: next,
        entries: append(entries, line("start", `${data.step_name} (${data.step_type})`)),
      };
    }
    case "final": {
      const held = data.orch_step_id && pending.get(data.orch_step_id);
      if (!held || !data.response) return state;
      const next = new Map(pending);
      next.set(data.orch_step_id, { ...held, content: String(data.response) });
      return { ...state, pending: next };
    }
    case "step_complete": {
      const held = pending.get(data.orch_step_id);
      const next = new Map(pending);
      next.delete(data.orch_step_id);
      state = { ...state, liveActivity: null };
      let out = entries;
      if (held && held.content.trim()) out = append(out, { ...held, content: held.content.trim() });
      out = append(
        out,
        line(
          "ok",
          `${data.step_name} completed${data.duration_seconds != null ? ` (${data.duration_seconds.toFixed(1)}s)` : ""}`,
        ),
      );
      return { ...state, pending: next, entries: out };
    }
    case "step_error": {
      const next = new Map(pending);
      next.delete(data.orch_step_id);
      return {
        ...state,
        pending: next,
        liveActivity: null,
        // The step's own words (`reason`, the engine's F195) — every reader of
        // this log is a member; `error` is the sanitised sentence kept for
        // readers that forward the stream unread.
        entries: append(entries, line("error", `Step error: ${data.reason || data.error}`)),
      };
    }
    case "llm_reasoning":
      if (!data.reasoning) return state;
      state = {
        ...state,
        liveActivity: `reasoning · ${String(data.reasoning).split("\n")[0].slice(0, 140)}`,
      };
      return {
        ...state,
        entries: append(entries, { kind: "reasoning", step_name: data.step_name, content: String(data.reasoning) }),
      };
    case "thinking":
      // Progress chatter. The live line only — never an entry.
      return data.message ? { ...state, liveActivity: `thinking · ${data.message}` } : state;
    case "llm_thought":
      return data.thought
        ? add({ kind: "thought", step_name: data.step_name, content: String(data.thought) })
        : state;
    case "status":
      return data.message ? add(line("info", data.message)) : state;
    case "step_warning":
      return add(line("warn", data.message || "Step warning"));
    case "context_compact":
      return add(
        line("compact", `Context compacted (${data.chars_before ?? "?"} → ${data.chars_after ?? "?"} chars)`),
      );
    case "evaluator_decision": {
      // The evaluator step's own event: the route the model picked. It
      // answered to `routing_decision` until the engine separated them, and
      // carries neither `decision` nor `target_step_id` — so this line read
      // `Evaluator routed → undefined` before every real decision.
      const picked = String(data.tool_name || "").replace(/^route_/, "");
      const why = (data.arguments as Record<string, unknown> | undefined)?.reasoning;
      return add(line("flow", `Evaluator chose ${picked || "(no route)"}${why ? ` (${why})` : ""}`));
    }
    case "routing_decision": {
      // One router event, three routing step types; `step_type` says which.
      // Read it before `decision`, because a `None` decision means two
      // different things: a switch that matched no case, and an if whose step
      // never evaluated. Absent `step_type` is an archived run from before the
      // rename, when only the evaluator used this name.
      const st = data.step_type as string | undefined;
      // An evaluator has already said where it went, in its own
      // `evaluator_decision` line: printing the router's too wrote every
      // decision twice (U66, OSS's editor; cloud's builder panel and run pages
      // printed both until M3a-2). The step's line is the one that is always
      // there — when its label is not in the route map the router says nothing.
      if (st === "evaluator") return state;
      if (st === "if_else")
        return add(line("flow", `Branch: ${data.decision ?? "false"} → ${data.target_step_id || "end"}`));
      if (st === "switch")
        return add(line("flow", `Branch: case ${data.decision ?? "default"} → ${data.target_step_id || "end"}`));
      return add(line("flow", `Evaluator routed → ${data.decision}${data.reasoning ? ` (${data.reasoning})` : ""}`));
    }
    // The step executors' own events. The router shared these names with a
    // payload that had no field in common; it emits `routing_decision` now.
    // The second arm of each stays for runs archived before that change — the
    // S3 NDJSON archive replays raw events, so the old shapes still arrive.
    case "if_decision":
      return add(line("flow", data.condition !== undefined
        ? `If/Else: ${data.condition} → ${data.result}`
        : `Branch: ${data.decision ?? "false"} → ${data.target_step_id || "end"}`));
    case "switch_decision":
      return add(
        line("flow", data.expression !== undefined
          ? `Switch: ${data.expression} = "${data.value}" → ${data.matched_case ?? "default"}`
          : `Branch: case ${data.matched_case ?? "default"} → ${data.target_step_id || "end"}`),
      );
    case "parallel_start":
      return add(line("parallel", `Parallel: running ${data.branch_count} branches`));
    case "branch_start":
      return add(line("branch", `Branch ${(data.branch_index ?? 0) + 1}/${data.branch_count}`));
    case "parallel_complete":
      return add(line("parallel", `Parallel: all ${data.branch_count} branches done`));
    case "loop_iteration":
      return add(line("loop", `Loop iteration ${data.iteration}/${data.total}`));
    case "loop_limit_reached":
      return add(line("loop", `Loop limit reached (${data.iterations} iterations)`));
    case "merge_complete":
      return add(line("merge", `Merged ${data.input_count} inputs (${data.strategy})`));
    case "orchestration_end":
      return add(line("end", "End node reached"));
    case "tool_execution":
      // `update_tasks` renders as the plan, not as a call and a result around it.
      if (data.tool_name === "update_tasks") {
        return { ...state, liveActivity: "tasks · updating plan" };
      }
      state = { ...state, liveActivity: `tool · ${data.tool_name}` };
      return {
        ...state,
        entries: append(entries, {
          kind: "tool_call",
          tool_name: data.tool_name,
          args: data.args || {},
          step_name: data.step_name,
        }),
      };
    case "tool_result":
      if (data.tool_name === "update_tasks") return state;
      return add({ kind: "tool_result", tool_name: data.tool_name, preview: data.preview || "" });
    case "tasks_updated":
      // A delegate's plan is its own, never the run's (F536): the engine forwards
      // it tagged `delegate_from`, and the run keeps its lead's.
      if (data.delegate_from) return state;
      return { ...state, tasks: Array.isArray(data.tasks) ? data.tasks : [] };
    case "tasks_incomplete": {
      if (data.delegate_from) return state;
      const open = Array.isArray(data.tasks) ? data.tasks.length : 0;
      return add(line("warn", `Step ended with ${open} open task${open === 1 ? "" : "s"}`));
    }
    case "human_input_required":
      state = { ...state, liveActivity: null };
      return { ...state, entries: append(entries, line("pause", "Waiting for human input…")) };
    case "orchestration_complete":
      state = { ...state, liveActivity: null };
      return {
        ...state,
        entries: append(
          entries,
          line(data.status === "completed" ? "ok" : "error", `Done — status: ${data.status}`),
        ),
      };
    case "orchestration_error":
      state = { ...state, liveActivity: null };
      return { ...state, entries: append(entries, line("error", `Error: ${data.error}`)) };
    // Knowledge's two steps (R1, D166; R1c, D173), as OSS's run log draws them.
    case "knowledge_retrieve":
      return add({
        kind: "retrieval",
        step_name: data.step_name,
        query_id: data.query_id,
        hit_count: data.hit_count ?? 0,
        cost_usd: data.cost_usd ?? 0,
        degraded: Array.isArray(data.degraded) ? data.degraded : [],
      });
    case "knowledge_ingest": {
      const docs: { status?: string }[] = Array.isArray(data.documents) ? data.documents : [];
      const byStatus = new Map<string, number>();
      for (const d of docs) byStatus.set(d.status || "unknown", (byStatus.get(d.status || "unknown") || 0) + 1);
      const statuses = [...byStatus].map(([status, n]) => `${n} ${status}`).join(", ");
      return add(
        line(
          "info",
          `Ingest · ${data.step_name || ""}: ${docs.length} record${docs.length !== 1 ? "s" : ""} → ${data.kb_id || ""}${statuses ? ` · ${statuses}` : ""}`,
        ),
      );
    }
    default:
      return state;
  }
}
/* eslint-enable @typescript-eslint/no-explicit-any */

/**
 * A Retrieve step's search in the run log (R1): the passage count, cost and
 * what degraded, and its trace — the legs, cuts, index versions and passages —
 * fetched the first time the row is opened (`GET /api/knowledge/queries/{id}`,
 * kept 30 days). A port of OSS's `RetrievalRow`; `RetrievalTrace` itself is synced.
 */
function RetrievalRow({ entry }: { entry: RetrievalEntry }) {
  const [trace, setTrace] = React.useState<SearchTrace | null>(null);
  const [failed, setFailed] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const load = () => {
    if (trace || loading) return;
    setLoading(true);
    setFailed(null);
    getQueryTrace(entry.query_id)
      .then(setTrace)
      .catch((err) => setFailed(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  };
  const n = entry.hit_count;
  return (
    <div className="pl-2 text-accent-ink">
      <details
        onToggle={(e) => {
          if ((e.currentTarget as HTMLDetailsElement).open) load();
        }}
      >
        <summary className="cursor-pointer list-none">
          <BookOpenText size={11} className="mr-1 inline-block align-[-1px]" aria-hidden />
          Retrieve{entry.step_name ? ` · ${entry.step_name}` : ""}: {n} passage{n !== 1 ? "s" : ""}
          <span className="text-text-faint"> · ${entry.cost_usd.toFixed(4)}</span>
          {entry.degraded.length > 0 && (
            <span className="text-text-faint"> · {degradedWords(entry.degraded)}</span>
          )}
        </summary>
        <div className="mt-0.5 rounded-md bg-surface-2 px-2 py-1 text-text-muted">
          {loading && <Spinner label="Loading trace…" />}
          {failed && (
            <span role="alert" className="text-danger">
              Trace unavailable: {failed}
            </span>
          )}
          {trace && <RetrievalTrace trace={trace} withDegraded withHits dense />}
          <div className="pt-0.5 text-[10px] text-text-faint" translate="no">
            {entry.query_id}
          </div>
        </div>
      </details>
    </div>
  );
}

/** A Retrieve row's degraded search, in words as its opened trace says them (F372): the
 *  row printed the codes. Each is a sentence; a code the map does not know is shown as is. */
export function degradedWords(codes: readonly string[]): string {
  return codes.map((code) => DEGRADED[code] ?? code).join(" ");
}

/** One log entry, shape-for-shape with the OSS BottomPanel renderer, on the tokens. */
export function LogLine({ entry, onOpen }: { entry: LogEntry; onOpen: (e: StepResultEntry) => void }) {
  if (typeof entry !== "string") {
    if (entry.kind === "line") {
      const meta = LINE_META[entry.tone];
      const LineIcon = meta.icon;
      return (
        <div className={`flex items-start gap-1.5 ${meta.cls}`}>
          <LineIcon size={11} className="mt-[3px] shrink-0" aria-hidden />
          {/* Plain text, not markdown. These lines are built here from event
              fields — an expression, a step name, a tool name, a customer's
              own data — and none of them is markdown anyone wrote. Rendering
              them as markdown ate the underscores in
              `().__class__.__bases__[0].__subclasses__()`, so a run log
              explaining which attribute the engine refused displayed a
              different attribute. */}
          <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{entry.text}</span>
        </div>
      );
    }
    if (entry.kind === "tool_call") {
      return (
        <div className="pl-2 text-accent-ink">
          <details>
            <summary className="cursor-pointer list-none">
              <Wrench size={11} className="mr-1 inline-block align-[-1px]" aria-hidden />
              {entry.tool_name}
              {entry.step_name && <span className="text-[10px] text-text-faint"> · {entry.step_name}</span>}
            </summary>
            <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap rounded-md bg-surface-2 p-1 font-code text-[10px] text-text-muted">
              {JSON.stringify(entry.args, null, 2)}
            </pre>
          </details>
        </div>
      );
    }
    if (entry.kind === "retrieval") return <RetrievalRow entry={entry} />;
    if (entry.kind === "tool_result") {
      return (
        <div className="flex items-start gap-1.5 pl-4 text-[10px] text-text-faint">
          <CornerDownRight size={10} className="mt-[2px] shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">{entry.preview.slice(0, 200)}{entry.preview.length > 200 ? "…" : ""}</span>
        </div>
      );
    }
    if (entry.kind === "step_result") {
      const preview = entry.content.slice(0, 200).replace(/\n+/g, " ").trim();
      const isTruncated = entry.content.length > 200;
      const isAgent = entry.step_type === "agent";
      const isPrint = entry.step_type === "print";
      const isExtractJson = entry.step_type === "extract_json";
      // Colour for meaning only (D202): a model wrote it (cyan) or a step printed it (grey).
      const dotColor = isPrint || isExtractJson ? "bg-border-strong" : "bg-machine";
      const labelColor = "text-text-muted";
      const typeLabel = isAgent ? "agent response" : isPrint ? "print output" : isExtractJson ? "json output" : "llm output";
      return (
        <div className="my-1.5">
          <div className="mb-1 flex items-center gap-1.5 pl-1">
            <div className={`size-1.5 flex-shrink-0 rounded-full ${dotColor}`} />
            <span className={`text-[10px] font-semibold uppercase tracking-wider ${labelColor}`} translate="no">
              {entry.step_name}
            </span>
            <span className="text-[9px] uppercase tracking-wide text-text-faint">{typeLabel}</span>
          </div>
          {/* The whole card opens the full response; the hover hint is the signpost. */}
          <button
            type="button"
            onClick={() => onOpen(entry)}
            title="View full response"
            className="group ml-3 flex w-[calc(100%-0.75rem)] items-start gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-left transition-colors hover:border-border-strong"
          >
            <span className="min-w-0 flex-1 leading-relaxed text-text">
              {preview}{isTruncated ? "…" : ""}
            </span>
            <span className="ml-1 flex shrink-0 items-center gap-1 self-center whitespace-nowrap text-[10px] text-text-faint opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
              <ExternalLink size={10} aria-hidden />
              View full
            </span>
          </button>
        </div>
      );
    }
    if (entry.kind === "reasoning" || entry.kind === "thought") {
      const isReasoning = entry.kind === "reasoning";
      const firstLine = entry.content.split("\n")[0].slice(0, 120);
      return (
        <div className="my-0.5 pl-2">
          <details>
            <summary className={`cursor-pointer list-none text-[10px] ${isReasoning ? "text-accent-ink" : "text-text-faint"}`}>
              {isReasoning
                ? <Brain size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />
                : <MessageSquare size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />}
              {firstLine}{entry.content.length > firstLine.length ? "…" : ""}
              {entry.step_name && <span className="text-text-faint"> · {entry.step_name}</span>}
            </summary>
            <div className="mt-0.5 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-2 text-[10px] text-text-muted">
              {entry.content}
            </div>
          </details>
        </div>
      );
    }
    return null;
  }

  return (
    <div
      className={
        entry.startsWith("✓") ? "text-success" :
        entry.startsWith("✗") ? "text-danger" :
        entry.startsWith("▶") ? "text-accent-ink" :
        entry.startsWith("⏸") ? "text-person-ink" :
        entry.startsWith("⟳") ? "text-accent-ink" :
        "text-text-muted"
      }
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <span>{children}</span>,
          code: ({ children }) => <code className="rounded-md bg-surface-2 px-1 font-code text-[10px]">{children}</code>,
          pre: ({ children }) => <pre className="mt-0.5 overflow-x-auto rounded-md bg-surface-2 p-1 font-code">{children}</pre>,
          a: ({ href, children }) => <a href={href} className="underline opacity-70" target="_blank" rel="noreferrer">{children}</a>,
        }}
      >{entry}</ReactMarkdown>
    </div>
  );
}

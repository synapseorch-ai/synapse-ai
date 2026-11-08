"use client";

/**
 * The run's events exactly as the engine emitted them, on demand.
 *
 * The **debugging** view, deliberately unpretty. The readable one is
 * `RunActivity`: the same events, folded through the log's reducer. This stays
 * because when a run goes wrong the question stops being "what happened" and
 * becomes "what exactly did the engine emit", and a renderer that interprets
 * events is the wrong tool for that — it is the thing under suspicion.
 *
 * Moved upstream from cloud's run page (`RunLog.tsx`) with the run screens
 * (M2b); where the events come from is the product's (`RunHost.rawEvents`).
 * Fetched when opened, once, and kept: a long run's log is thousands of events
 * and most visits never want it.
 */

import * as React from "react";

import { Button, ErrorNote, Section } from "@/components/ui";
import { cn } from "@/lib/cn";

import { useRunHost } from "./host";

type LogEvent = { type?: string; [key: string]: unknown };

/** Types that carry no detail worth printing beside the name. */
const BARE = new Set(["run_stream_end", "done", "worker_picked_up"]);

function tint(type: string | undefined): string {
  if (!type) return "text-text-muted";
  if (type.includes("error") || type.includes("failed")) return "text-danger";
  if (type === "orchestration_complete" || type === "done") return "text-success";
  if (type === "human_input_required") return "text-person-ink";
  return "text-accent-ink";
}

export function RawEventLog({ runId }: { runId: string }) {
  const host = useRunHost();
  const [events, setEvents] = React.useState<LogEvent[] | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [raw, setRaw] = React.useState(false);
  const requested = React.useRef(false);

  const load = React.useCallback(async () => {
    if (requested.current) return;
    requested.current = true;
    setLoading(true);
    try {
      const got = await host.rawEvents(runId);
      setEvents(got.map((e) => (e && typeof e === "object" ? (e as LogEvent) : { type: "unparsed", line: String(e) })));
    } catch (e) {
      setError(e instanceof Error ? e.message : "The log could not be read.");
      requested.current = false; // Let them try again.
    } finally {
      setLoading(false);
    }
  }, [host, runId]);

  return (
    <Section
      title="Raw event log"
      description="Every event exactly as the engine emitted it — for when the Activity view above is not enough."
      summary={events ? `${events.length} events` : "Open to load"}
    >
      <Opener onOpen={load} />

      {loading && <p className="text-sm text-text-muted">Reading the log…</p>}
      <ErrorNote>{error}</ErrorNote>

      {events && events.length === 0 && (
        <p className="text-sm text-text-muted">No events were recorded for this run.</p>
      )}

      {events && events.length > 0 && (
        <div className="space-y-3">
          <div className="flex justify-end">
            <Button variant="secondary" size="sm" onClick={() => setRaw((r) => !r)}>
              {raw ? "Show summary" : "Show raw NDJSON"}
            </Button>
          </div>

          {raw ? (
            <pre className="scroll-x max-h-96 overflow-y-auto rounded-md border border-border bg-bg px-4 py-3 font-mono text-xs leading-relaxed text-text-muted">
              {events.map((e) => JSON.stringify(e)).join("\n")}
            </pre>
          ) : (
            <ol className="max-h-96 space-y-1 overflow-y-auto rounded-md border border-border bg-bg px-4 py-3 font-mono text-xs leading-relaxed">
              {events.map((event, index) => {
                const { type, ...rest } = event;
                return (
                  <li key={index} className="flex gap-2">
                    <span className={cn("shrink-0", tint(type))}>{type ?? "event"}</span>
                    {!BARE.has(type ?? "") && Object.keys(rest).length > 0 && (
                      <span className="min-w-0 flex-1 truncate text-text-muted">
                        {JSON.stringify(rest)}
                      </span>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </Section>
  );
}

/**
 * Fires once, when the Section first renders its children.
 *
 * `Section` owns its own open state and exposes no `onOpenChange`, and it
 * unmounts its children when closed — so a component that mounts inside it *is*
 * the "was opened" signal. Cheaper than widening the shared primitive's API for
 * one caller, and it keeps the lazy-load decision in the module that needs it.
 */
function Opener({ onOpen }: { onOpen: () => void }) {
  React.useEffect(() => {
    onOpen();
  }, [onOpen]);
  return null;
}

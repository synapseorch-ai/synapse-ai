/*
 * The Logs tab's rows (M2h), pure so both products and their tests share them.
 * A log is a transcript file; a workflow's log has the same id as its run, so a
 * product that has read its runs can say how that run went beside the log.
 * Agent and schedule logs carry no status in their listing, so their rows show
 * none — never "Failed", which is what RunList's `bucketOf` makes of a missing
 * status.
 */
import { bucketOf, type RunBucket } from './model';

export type LogKind = 'agents' | 'orchestrations' | 'schedules';

/** How the run behind a log went, when the product knows it. */
export type LogRun = {
    status: string;
    waitingForHuman?: boolean;
    startedAt: string | null;
    endedAt: string | null;
    /** `undefined` when the product doesn't know it, `null` when unpriced. */
    costUsd?: number | null;
    costHint?: string;
};

/** A session's heading in OSS's list: "Session 1a2b3c4d" or "Individual run", and the first thing it was asked. */
export type LogGroup = { key: string; label: string; quote?: string | null };

export type LogListRow = {
    /** The log's id (a workflow log's is its run's). */
    id: string;
    name: string;
    /** Line 2: the source, the workflow's id, or what started it. */
    sub?: string | null;
    /** A run started by another run (OSS: an id with `__`). */
    nested?: boolean;
    sizeBytes?: number | null;
    /** When it was written (an ISO instant). */
    at: string | null;
    run?: LogRun | null;
    group?: LogGroup;
};

/** The run's bucket, or null when the log has no run the product could match. */
export function logBucket(row: LogListRow): RunBucket | null {
    return row.run ? bucketOf(row.run.status, row.run.waitingForHuman) : null;
}

/** "812 B", "3.8 KB", "1.2 MB". */
export function logSize(bytes: number | null | undefined): string {
    if (bytes == null) return '';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** What a log's listing says about where it came from, as each product reads it. */
export type LogFacts = {
    runId: string;
    agentId?: string | null;
    /** An agent log's source: `chat`, `schedule`, `voice`, `openai`, or `orchestration:<run id>` for a step. */
    source?: string | null;
    /** A schedule log's target: `agent` or `orchestration`. */
    targetType?: string | null;
};

/** The name Synapse AI's own logs go by: its steps' agents and its run are the product's, not the person's. */
export const ASSISTANT_LOG_NAME = 'Synapse AI';

/** Every Synapse AI run's id begins with this (the engine's `BUILDER_RUN_PREFIX`). */
const ASSISTANT_RUN = 'builder_';

/**
 * Whether a log is Synapse AI's own (F1416): one of its step agents (`agent_native_builder…`, as the engine's
 * `is_builder_agent` knows them), its run, or a step of its run. A person knows these as Synapse AI, never by the
 * agents' own names ("Builder — Classifier") or the run's key.
 */
export function isAssistantLog(log: LogFacts): boolean {
    const agent = log.agentId ?? '';
    return agent === 'agent_native_builder' || agent.startsWith('agent_native_builder_')
        || log.runId.startsWith(ASSISTANT_RUN)
        || (log.source ?? '').startsWith(`orchestration:${ASSISTANT_RUN}`);
}

const SOURCES = new Map<string, string>([
    ['chat', 'Chat'], ['schedule', 'Schedule'], ['voice', 'Phone call'], ['api', 'API'],
    ['openai', 'OpenAI-compatible API'],
]);

/**
 * Line 2 of a log's row in words a person knows (F1416): where an agent's log came from, what a schedule runs. A
 * workflow's log has none: its name says which workflow. Synapse AI's has none either. A source no product names
 * yet is shown as it is.
 */
export function logLine(kind: LogKind, log: LogFacts): string | null {
    if (isAssistantLog(log)) return null;
    if (kind === 'agents') {
        if (!log.source) return null;
        if (log.source.startsWith('orchestration:')) return 'From a workflow';
        return SOURCES.get(log.source) ?? log.source;
    }
    if (kind === 'schedules') {
        if (log.targetType === 'orchestration') return 'Runs a workflow';
        if (log.targetType === 'agent') return 'Runs an agent';
        return log.targetType || null;
    }
    return null;
}

/** Every word of the query appears in the row's name, id, line 2 or its group's question. */
export function matchLog(row: LogListRow, query: string): boolean {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (words.length === 0) return true;
    const hay = [row.name, row.id, row.sub, row.group?.quote].filter(Boolean).join(' ').toLowerCase();
    return words.every((w) => hay.includes(w));
}

/**
 * Rows in order, with each run of rows that share a group under one heading.
 * A row with no group stands alone (no heading).
 */
export function groupLogs(rows: LogListRow[]): { group: LogGroup | null; rows: LogListRow[] }[] {
    const out: { group: LogGroup | null; rows: LogListRow[] }[] = [];
    for (const row of rows) {
        const last = out[out.length - 1];
        if (row.group && last?.group?.key === row.group.key) last.rows.push(row);
        else out.push({ group: row.group ?? null, rows: [row] });
    }
    return out;
}

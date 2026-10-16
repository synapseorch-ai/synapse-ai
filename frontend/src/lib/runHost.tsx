'use client';

/*
 * OSS's side of the run screens (M2b): where things are, how a run's events are
 * read, what a person can do to a run, and this API's rows mapped onto the
 * shared shapes in `components/run/model.ts`. Cloud keeps its own copy of this
 * file; `components/run/` is the same in both.
 */
import {
    triggerOf, type LogRun, type NestedRun, type RunHost, type RunRecord, type RunRow, type WorkflowLastRun,
} from '@/components/run';
import type { RunSummary } from '@/components/runs/summary';
import { refusedAnswerMessage } from '@/lib/answers';
import type { Orchestration } from '@/types/orchestration';

const enc = encodeURIComponent;

/** Events after which nothing more comes, when a stream closes without saying so. */
const TERMINAL = new Set(['orchestration_complete', 'orchestration_error', 'done', 'stream_complete']);

/**
 * A run's journal, replayed from the start and followed live (`/events/stream`,
 * F49's reattach stream): the server says `stream_complete` once the run is
 * over, and keeps the stream open while it waits for a person. A dropped
 * connection resumes from the last event id. A fleet run's stream is bridged
 * from Redis by the same route.
 */
async function followJournal(runId: string, onEvent: (event: unknown) => void, signal: AbortSignal): Promise<void> {
    let last = '';
    let attempts = 0;
    let lastType = '';
    while (!signal.aborted) {
        const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/events/stream?after=0`, {
            signal,
            headers: last ? { 'Last-Event-ID': last } : undefined,
        });
        if (res.status === 404) return;  // a run from before the journal
        if (!res.ok || !res.body) throw new Error(`The log could not be read (HTTP ${res.status}).`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
            let chunk: ReadableStreamReadResult<Uint8Array>;
            try {
                chunk = await reader.read();
            } catch (e) {
                if (signal.aborted) return;
                throw e;
            }
            if (chunk.done) break;
            attempts = 0;
            buffer += decoder.decode(chunk.value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
                if (line.startsWith('id: ')) {
                    last = line.slice(4).trim();
                } else if (line.startsWith('data: ')) {
                    let event: { type?: string };
                    try { event = JSON.parse(line.slice(6)); } catch { continue; }
                    if (event?.type === 'stream_complete') return;
                    lastType = event?.type ?? '';
                    onEvent(event);
                }
            }
        }
        // Closed without `stream_complete`: over if its last word was an ending,
        // else a dropped connection to resume.
        if (TERMINAL.has(lastType) || signal.aborted) return;
        if (++attempts > 5) throw new Error('The live log stopped answering. Reload to read it again.');
        await new Promise((r) => setTimeout(r, Math.min(3000, 500 * attempts)));
    }
}

/** A POST that answers with the run's event stream: the run belongs to the server (F49), so the stream is let go. */
async function postAndRelease(url: string, body: unknown, refused: (res: Response) => Promise<string>): Promise<void> {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await refused(res));
    await res.body?.cancel().catch(() => {});
}

/** Workflow names by id, read once each, for nested runs. */
const names = new Map<string, string>();
async function workflowName(id: string): Promise<void> {
    if (!id || names.has(id)) return;
    const wf = await fetch(`/api/orchestrations/${enc(id)}`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (wf?.name) names.set(id, String(wf.name));
}

async function detailOf(res: Response, fallback: string): Promise<string> {
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    return typeof body.detail === 'string' ? body.detail : `${fallback} (HTTP ${res.status}).`;
}

export const OSS_RUN_HOST: RunHost = {
    links: {
        runs: '/runs',
        run: (id) => `/runs/${enc(id)}`,
        // The run restored on its canvas, the failed step marked (the editor's deep link).
        editor: (run) => `/workflows?run=${enc(run.id)}`,
        // Its settings open, where the cap per run is (CAP1).
        workflowSettings: (run) => `/workflows?id=${enc(run.workflowId)}&settings=1`,
        models: '/settings/models',
        agents: '/agents',
        knowledge: '/knowledge',
    },
    events: (runId, _live, onEvent, signal) => followJournal(runId, onEvent, signal),
    rawEvents: async (runId) => {
        const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/events`);
        if (res.status === 404) return [];
        if (!res.ok) throw new Error(`The log could not be read (HTTP ${res.status}).`);
        const body = (await res.json()) as { events?: { event?: unknown }[] };
        return (body.events ?? []).map((e) => e?.event ?? e);
    },
    children: async (runId) => {
        const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/children`);
        if (!res.ok) return [];
        const rows = await res.json();
        if (!Array.isArray(rows)) return [];
        // OSS's rows carry the workflow's id, not its name (cloud's carry both).
        const kids = rows as NestedRun[];
        await Promise.all([...new Set(kids.map((k) => k.orchestration_id))].map(workflowName));
        return kids.map((k) => ({ ...k, orchestration_name: k.orchestration_name ?? names.get(k.orchestration_id) ?? null }));
    },
    answer: async (runId, response) => {
        await postAndRelease(`/api/orchestrations/runs/${enc(runId)}/human-input`, { response }, refusedAnswerMessage);
        return 'Answer sent — the run is resuming.';
    },
    cancel: async (runId) => {
        const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/cancel`, { method: 'POST' });
        if (!res.ok) throw new Error(await detailOf(res, 'The run was not cancelled'));
        return 'Run cancelled.';
    },
    replay: async (runId) => {
        await postAndRelease(`/api/orchestrations/runs/${enc(runId)}/resume`, {}, (res) => detailOf(res, 'The run was not replayed'));
        return 'Replaying from where it stopped.';
    },
    // One login (F452): whoever is here may answer, cancel and replay.
    canAnswer: true,
    canControl: true,
    // A Team step's lines (TEAM1e): a question answered by its own id — a paused run resumes, a working one carries
    // that member on — and a member stopped or written to.
    team: {
        answer: async (runId, questionId, approve, reason = '') => {
            await postAndRelease(`/api/orchestrations/runs/${enc(runId)}/human-input`,
                { response: { approve, reason }, question_id: questionId }, refusedAnswerMessage);
            return approve ? 'Allowed — it carries on.' : 'Declined — it carries on without it.';
        },
        stop: async (runId, memberId) => {
            const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/members/${enc(memberId)}/stop`, { method: 'POST' });
            if (!res.ok) throw new Error(await detailOf(res, 'The member was not stopped'));
            return Boolean(((await res.json()) as { stopped?: boolean }).stopped);
        },
        message: async (runId, memberId, text) => {
            const res = await fetch(`/api/orchestrations/runs/${enc(runId)}/members/${enc(memberId)}/message`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
            });
            if (!res.ok) throw new Error(await detailOf(res, 'The message was not sent'));
            return Boolean(((await res.json()) as { delivered?: boolean }).delivered);
        },
    },
};

/** A row of `GET /api/orchestrations/runs`, for the list. */
export function ossRunRow(
    run: RunSummary,
    workflows: Pick<Orchestration, 'id' | 'name' | 'steps'>[],
    schedules: { id: string; name: string }[],
): RunRow {
    const wf = workflows.find((w) => w.id === run.orchestration_id);
    const stepName = run.current_step_id ? wf?.steps.find((s) => s.id === run.current_step_id)?.name : null;
    return {
        id: run.run_id,
        workflowId: run.orchestration_id,
        workflowName: wf?.name ?? run.orchestration_id,
        status: run.status,
        waitingForHuman: Boolean(run.waiting_for_human),
        startedAt: run.started_at ?? null,
        endedAt: run.ended_at ?? null,
        // The list does not carry tokens (the run page does): no column, rather than a column of dashes.
        costUsd: run.total_cost_usd ?? null,
        trigger: triggerOf(run.session_id, schedules),
        currentStepId: run.current_step_id ?? null,
        currentStepName: stepName || run.last_step_name || null,
        stepsDone: run.steps_completed ?? null,
        score: run.score ?? null,
        scoreRegression: run.score_regression,
        error: run.error ?? null,
    };
}

/** A workflow's newest run, for its row on the Workflows list (M2k). */
export function ossLastRun(run: RunSummary): WorkflowLastRun {
    return {
        id: run.run_id,
        status: run.status,
        waitingForHuman: Boolean(run.waiting_for_human),
        at: run.started_at ?? null,
        startedAt: run.started_at ?? null,
        endedAt: run.ended_at ?? null,
        costUsd: run.total_cost_usd ?? null,
    };
}

/** A run as the Logs tab shows it beside its log (M2h): the log's id is the run's. */
export function ossLogRun(run: RunSummary): LogRun {
    return {
        status: run.status,
        waitingForHuman: Boolean(run.waiting_for_human),
        startedAt: run.started_at ?? null,
        endedAt: run.ended_at ?? null,
        costUsd: run.total_cost_usd ?? null,
    };
}

/** `GET /api/orchestrations/runs/{id}`, as the run page reads it. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function ossRunRecord(data: any, workflow: Orchestration | null, schedules: { id: string; name: string }[]): RunRecord {
    return {
        id: data.run_id,
        workflowId: data.orchestration_id ?? '',
        workflowName: workflow?.name ?? null,
        status: data.status ?? 'running',
        waitingForHuman: Boolean(data.waiting_for_human),
        startedAt: data.started_at ?? null,
        endedAt: data.ended_at ?? null,
        currentStepId: data.current_step_id ?? null,
        tokens: typeof data.total_tokens_used === 'number' ? data.total_tokens_used : null,
        costUsd: typeof data.total_cost_usd === 'number' ? data.total_cost_usd : null,
        trigger: triggerOf(data.session_id, schedules),
        history: Array.isArray(data.step_history) ? data.step_history : [],
        sharedState: data.shared_state && typeof data.shared_state === 'object' ? data.shared_state : {},
        tasks: Array.isArray(data.tasks) ? data.tasks : [],
        humanPrompt: data.waiting_for_human ? (data.human_prompt ?? null) : null,
        humanFields: Array.isArray(data.human_fields) ? data.human_fields : null,
        humanContext: data.human_context ?? null,
        error: data.error ?? null,
        // Each step's spend, swept from the run's calls (CE1).
        ...(data.step_usage && typeof data.step_usage === 'object' ? { stepUsage: data.step_usage } : null),
        ...(Array.isArray(data.entry_usage) ? { entryUsage: data.entry_usage } : null),
    };
}

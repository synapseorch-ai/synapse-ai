'use client';
/**
 * The self-hosted install's way to press the assistant's action card (ASSIST1, D291): its act route, and the run
 * page's own card for a run's question. What a button does is decided by the server from the button's token; this
 * only carries it.
 */
import { useEffect, useState, type ReactNode } from 'react';

import type { ActResult, Actor } from '@/components/assistant';
import { ApprovalCard, RunHostProvider, type RunRecord } from '@/components/run';
import { ErrorNote, Spinner } from '@/components/ui';
import type { ActionButton } from '@/lib/builderEvents';
import { OSS_RUN_HOST, ossRunRecord } from '@/lib/runHost';

const enc = encodeURIComponent;

async function press(button: ActionButton): Promise<ActResult> {
    const res = await fetch('/api/assistant/act', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: button.tool, arguments: button.arguments, token: button.token }),
    });
    let body: { detail?: unknown; result?: { run_id?: string | null } } = {};
    try { body = await res.json(); } catch { /* not JSON */ }
    if (res.ok) return { ok: true, runId: body.result?.run_id ?? null };
    const message = typeof body.detail === 'string' && body.detail ? body.detail : `That did not work (${res.status}).`;
    // Done before, expired, or not this person's: the card cannot be pressed again.
    return { ok: false, message, spent: res.status === 403 || res.status === 409 || res.status === 410 };
}

/** A waiting run's question, answered with the run page's card. */
function RunQuestion({ runId, onDone }: { runId: string; onDone: (message: string) => void }) {
    const [run, setRun] = useState<RunRecord | null | 'gone'>(null);
    useEffect(() => {
        let live = true;
        (async () => {
            const res = await fetch(`/api/orchestrations/runs/${enc(runId)}`);
            const data = res.ok ? await res.json() : null;
            const flow = data ? await fetch(`/api/orchestrations/${enc(data.orchestration_id)}`)
                .then((r) => (r.ok ? r.json() : null)).catch(() => null) : null;
            const record = data ? ossRunRecord(data, flow, []) : null;
            if (live) setRun(record && record.status === 'paused' && record.humanPrompt ? record : 'gone');
        })().catch(() => live && setRun('gone'));
        return () => { live = false; };
    }, [runId]);
    if (run === null) return <Spinner size={14} />;
    if (run === 'gone') return <ErrorNote>That run is not waiting for an answer any more.</ErrorNote>;
    return (
        <RunHostProvider value={OSS_RUN_HOST}>
            <ApprovalCard
                run={run}
                busy={false}
                onDone={onDone}
                onCancel={() => void OSS_RUN_HOST.cancel(run.id).then(onDone, () => undefined)}
                openHref={`/runs/${enc(run.id)}`}
                showWorkflow
            />
        </RunHostProvider>
    );
}

export const selfHostedActor: Actor = {
    press,
    question: (runId: string, onDone: (message: string) => void): ReactNode => <RunQuestion runId={runId} onDone={onDone} />,
    runHref: (runId: string) => `/runs/${enc(runId)}`,
};

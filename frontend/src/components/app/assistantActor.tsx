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
import { widgetSnippet } from '@/lib/widgetSnippet';

const enc = encodeURIComponent;

async function press(button: ActionButton): Promise<ActResult> {
    const res = await fetch('/api/assistant/act', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            tool: button.tool, arguments: button.arguments, token: button.token, run_id: button.run_id ?? null,
        }),
    });
    let body: {
        detail?: unknown;
        result?: {
            run_id?: string | null; public_key?: string | null; response?: string; status?: string; note?: string;
            say?: string;
        };
    } = {};
    try { body = await res.json(); } catch { /* not JSON */ }
    if (res.ok) {
        const result = body.result ?? {};
        if (button.tool === 'chat_with_agent') return agentAnswer(result);
        // The act's own words for what it did, when it has them (F1380: a tested fix says how the test went).
        const message = typeof result.say === 'string' && result.say.trim() ? result.say.trim() : undefined;
        return { ok: true, runId: result.run_id ?? null, publicKey: result.public_key ?? null, message };
    }
    const message = typeof body.detail === 'string' && body.detail ? body.detail : `That did not work (${res.status}).`;
    // Done before, expired, or not this person's: the card cannot be pressed again.
    return { ok: false, message, spent: res.status === 403 || res.status === 409 || res.status === 410 };
}

/** An agent's turn on a card that asked it something (F1274): its answer, or where the turn is now. */
function agentAnswer(result: { response?: string; status?: string; note?: string }): ActResult {
    if (result.status === 'completed') return { ok: true, reply: result.response || 'It answered with nothing.' };
    if (result.status === 'running') return { ok: true, message: result.note || 'It is still working.' };
    // Waiting on a person: its own tool approval, as ever.
    return { ok: true, message: [result.response, result.note].filter(Boolean).join(' ') || 'It is waiting for you.' };
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
    embedSnippet: (publicKey: string) => widgetSnippet(window.location.origin, publicKey),
};

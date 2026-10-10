'use client';

/*
 * A scheduled agent's tool call waiting on a person (F268), answered.
 *
 * The bell asked it first (`NotificationBell`'s row); Home's "Needs you" asks it
 * too (M2a). Both send the one request — `POST /api/schedules/{id}/approve`,
 * naming the question (`approval_id`) so an answer never lands on a newer one —
 * through `answerToolApproval`. The item turns resolved when the server
 * re-sends it on the stream; until then the control says what was sent. The
 * control is cloud's `ApprovalActions`, as OSS draws it.
 */
import { useState } from 'react';

import { Button } from '@/components/ui';

import type { RunNotification } from './NotificationProvider';

/** Answer the call; throws with the server's words when it is refused. */
export async function answerToolApproval(n: RunNotification, approve: boolean): Promise<void> {
    let res: Response;
    try {
        // A schedule's question goes to its schedule; an API turn's (OAI1) names its own place.
        const url = typeof n.data?.answer_url === 'string' && n.data.answer_url.startsWith('/api/')
            ? n.data.answer_url
            : `/api/schedules/${encodeURIComponent(n.data?.schedule_id)}/approve`;
        res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ approve, approval_id: n.data?.approval_id }),
        });
    } catch {
        throw new Error('That answer did not go through.');
    }
    if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail;
        throw new Error(typeof detail === 'string' ? detail : 'That answer did not go through.');
    }
}

/** The call in one line, and Approve / Refuse. */
export function ToolApprovalActions({ n }: { n: RunNotification }) {
    const [busy, setBusy] = useState<'allow' | 'refuse' | null>(null);
    const [sent, setSent] = useState<'allowed' | 'refused' | null>(null);
    const [error, setError] = useState('');
    const args = n.data?.tool_args;

    const answer = async (approve: boolean) => {
        setBusy(approve ? 'allow' : 'refuse');
        setError('');
        try {
            await answerToolApproval(n, approve);
            setSent(approve ? 'allowed' : 'refused');
        } catch (e) {
            setError(e instanceof Error ? e.message : 'That answer did not go through.');
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="grid gap-2">
            {n.data?.tool_name && (
                <code className="block truncate font-code text-xs text-text-muted" translate="no" title={JSON.stringify(args ?? {})}>
                    {n.data.tool_name}({JSON.stringify(args ?? {})})
                </code>
            )}
            {sent ? (
                <p role="status" className="m-0 text-sm text-text-muted">
                    {sent === 'allowed' ? 'Allowed — the schedule’s run carries on.' : 'Refused — not run.'}
                </p>
            ) : (
                <div className="flex flex-wrap items-center gap-2">
                    <Button size="sm" onClick={() => void answer(true)} loading={busy === 'allow'} disabled={busy !== null}>
                        Approve
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => void answer(false)} loading={busy === 'refuse'} disabled={busy !== null}>
                        Refuse
                    </Button>
                </div>
            )}
            {error && <p role="alert" className="m-0 text-sm text-danger">{error}</p>}
        </div>
    );
}

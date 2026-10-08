'use client';
/**
 * What is waiting on you, from any screen (M2c, Naveen's ask: "somewhere the
 * user can see and click to go and answer").
 *
 * The menu shows an amber "Waiting on you" row while a run waits for a person's
 * answer or a scheduled agent's tool call waits for an approval. Its count is
 * the bell's own: unresolved `human_input` and `tool_approval` items, which the
 * server resolves once they are answered — so the row costs no request. Pressed,
 * it opens Home's Needs you in a drawer: each run answered in place with the run
 * page's card, the rest as rows that open their run. On Home itself the row
 * goes to the section already on the page.
 *
 * Opening the drawer reads each waiting run's record, as Home does.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useNotifications, type RunNotification } from '@/components/notifications/NotificationProvider';
import { ToolApprovalActions } from '@/components/notifications/ToolApproval';
import { MAX_CARDS, NeedsYou, RunHostProvider, type RunRecord, type ToolCall, type WaitingBrief } from '@/components/run';
import { Drawer } from '@/components/ui';
import { plainText } from '@/lib/plainText';
import { OSS_RUN_HOST, ossRunRecord } from '@/lib/runHost';
import type { Orchestration } from '@/types/orchestration';

const runHref = (id: string) => `/runs/${encodeURIComponent(id)}`;

/** The runs (oldest wait first) and tool calls waiting on a person, as the bell's stream has them. */
export function useWaitingOnYou(): { runs: RunNotification[]; tools: RunNotification[]; count: number } {
    const { notifications } = useNotifications();
    return useMemo(() => {
        const open = notifications.filter(n => !n.resolved).sort((a, b) => a.ts - b.ts);
        const seen = new Set<string>();
        const runs = open.filter(n => {
            if (n.kind !== 'human_input' || !n.run_id || seen.has(n.run_id)) return false;
            seen.add(n.run_id);
            return true;
        });
        const tools = open.filter(n => n.kind === 'tool_approval');
        return { runs, tools, count: runs.length + tools.length };
    }, [notifications]);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the run's JSON, as `ossRunRecord` takes it
type Record_ = { key: string; data: any | null };

export function WaitingOnYouDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
    const { runs, tools } = useWaitingOnYou();
    const [records, setRecords] = useState<Record<string, Record_>>({});
    const [names, setNames] = useState<Map<string, Orchestration>>(new Map());
    const reading = useRef(new Set<string>());
    const [round, setRound] = useState(0);

    // The workflows' names, once per opening.
    useEffect(() => {
        if (!open) return;
        let live = true;
        fetch('/api/orchestrations')
            .then(r => (r.ok ? r.json() : null))
            .then(list => {
                if (live && Array.isArray(list)) setNames(new Map(list.map((w: Orchestration) => [w.id, w])));
            })
            .catch(() => { });
        return () => { live = false; };
    }, [open]);

    // Each run answered in place: its record, as its run page reads it, once per question.
    useEffect(() => {
        if (!open) return;
        for (const n of runs.slice(0, MAX_CARDS)) {
            const id = n.run_id as string;
            const key = `${n.id}:${round}`;
            if (records[id]?.key === key || reading.current.has(key)) continue;
            reading.current.add(key);
            fetch(`/api/orchestrations/runs/${encodeURIComponent(id)}`)
                .then(res => (res.ok ? res.json() : null))
                .catch(() => null)
                .then(data => setRecords(cur => ({ ...cur, [id]: { key, data } })))
                .finally(() => reading.current.delete(key));
        }
    }, [open, runs, records, round]);

    const reread = useCallback(() => setRound(r => r + 1), []);

    const cards: RunRecord[] = [];
    const more: WaitingBrief[] = [];
    runs.forEach((n, i) => {
        const id = n.run_id as string;
        const data = records[id]?.data;
        const workflow = data ? names.get(data.orchestration_id) ?? null : null;
        const record = data ? ossRunRecord(data, workflow, []) : null;
        if (i < MAX_CARDS && record && record.status === 'paused' && record.humanPrompt) cards.push(record);
        else if (i >= MAX_CARDS || records[id]) {
            more.push({
                id,
                workflowName: workflow?.name ?? null,
                prompt: record?.humanPrompt ?? (n.body ? plainText(n.body) : null),
                since: new Date(n.ts * 1000).toISOString(),
            });
        }
    });
    const calls: ToolCall[] = tools.map(n => ({
        id: String(n.id),
        title: n.title,
        body: n.body ? plainText(n.body) : null,
        at: new Date(n.ts * 1000).toISOString(),
        actions: <ToolApprovalActions n={n} />,
    }));

    return (
        <Drawer
            open={open}
            onClose={onClose}
            title="Waiting on you"
            description="Runs waiting for your answer, and tool calls waiting for your approval. Answer them here, or open the run."
            width="lg"
        >
            <RunHostProvider value={OSS_RUN_HOST}>
                <NeedsYou runs={cards} more={more} tools={calls} hrefOf={runHref} onChanged={reread} />
            </RunHostProvider>
        </Drawer>
    );
}

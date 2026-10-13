'use client';

/*
 * What the run produced (UX1, M2b): each value a step wrote, by the key it wrote
 * it under, with the key's description from the workflow's state schema and the
 * step it came from. The workflow's Result (`result_key`, F249) comes first.
 * Each opens whole, with Copy.
 */
import { useState } from 'react';

import { Badge, Button, Modal } from '@/components/ui';
import type { Orchestration } from '@/types/orchestration';

import { isLive, REMOVED_NOTE, type RunRecord } from './model';
import { OutputValue } from './StepDetail';

type Output = { key: string; value: unknown; from: string; description?: string; result: boolean };

function isEmpty(value: unknown): boolean {
    if (value === null || value === undefined) return true;
    if (typeof value === 'string') return !value.trim();
    if (Array.isArray(value)) return value.length === 0;
    if (typeof value === 'object') return Object.keys(value as object).length === 0;
    return false;
}

export function outputsOf(run: RunRecord, workflow: Orchestration | null): Output[] {
    const byKey = new Map<string, Output>();
    for (const h of run.history) {
        if (!h.output_key || h.status === 'failed') continue;
        const value = run.sharedState[h.output_key];
        if (isEmpty(value)) continue;
        byKey.set(h.output_key, {
            key: h.output_key,
            value,
            from: h.step_name || h.step_id,
            description: workflow?.state_schema?.[h.output_key]?.description || undefined,
            result: Boolean(workflow?.result_key && workflow.result_key === h.output_key),
        });
    }
    const out = [...byKey.values()].reverse();
    out.sort((a, b) => Number(b.result) - Number(a.result));
    return out;
}

function preview(value: unknown): string {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    const flat = text.replace(/\s+/g, ' ').trim();
    return flat.length > 140 ? `${flat.slice(0, 140)}…` : flat;
}

export function RunOutputs({ run, workflow }: { run: RunRecord; workflow: Orchestration | null }) {
    const outputs = outputsOf(run, workflow);
    const [opened, setOpened] = useState<Output | null>(null);
    if (outputs.length === 0) {
        const none = isLive(run.status) ? 'Nothing yet.' : run.stepDataRemovedAt ? REMOVED_NOTE : 'This run wrote no outputs.';
        return <p className="text-sm text-text-muted">{none}</p>;
    }
    return (
        <>
            <ul className="grid">
                {outputs.map((o) => (
                    <li key={o.key} className="grid min-h-[50px] grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3.5 border-b border-border px-2.5 py-2">
                        <span className="grid min-w-0 gap-0.5">
                            <span className="flex min-w-0 items-center gap-2">
                                <span className="truncate font-code text-[13px] text-text" translate="no">{o.key}</span>
                                {o.result && <Badge tone="accent" size="sm">Result</Badge>}
                            </span>
                            <span className="truncate text-xs text-text-faint">
                                {o.description ? `${o.description} · ` : ''}from {o.from}
                            </span>
                            <span className="truncate text-xs text-text-muted">{preview(o.value)}</span>
                        </span>
                        <Button variant="secondary" size="sm" onClick={() => setOpened(o)} aria-label={`Open ${o.key}`}>Open</Button>
                    </li>
                ))}
            </ul>
            <Modal
                open={opened !== null}
                onClose={() => setOpened(null)}
                title={opened ? opened.key : ''}
                description={opened ? `${opened.description ? `${opened.description} · ` : ''}from ${opened.from}` : undefined}
                size="lg"
                footer={
                    <Button
                        variant="secondary"
                        onClick={() => {
                            if (!opened) return;
                            navigator.clipboard.writeText(typeof opened.value === 'string' ? opened.value : JSON.stringify(opened.value, null, 2));
                        }}
                    >
                        Copy
                    </Button>
                }
            >
                {opened && <div className="max-h-[60vh] overflow-y-auto"><OutputValue value={opened.value} /></div>}
            </Modal>
        </>
    );
}

'use client';

/*
 * A failed run leads with one plain sentence and the fix (UX1, M2b).
 *
 * UX1 measured a failed run's page showing a raw Python traceback three times,
 * in red, before any plain reason. Here the sentence comes first
 * (`explainFailure`), then what to do, then the step's own words — unedited —
 * behind Technical details. A cancelled run is not a failure: same card, no red.
 */
import { AlertTriangle, CircleSlash } from 'lucide-react';

import { Button, LinkButton } from '@/components/ui';
import { cn } from '@/lib/cn';

import { explainFailure, parentRunId, type FixKind } from './failures';
import { useRunHost } from './host';
import type { NestedRun, RunRecord } from './model';

/** The failed step's history entry, and the words to explain. */
export function failureOf(run: RunRecord) {
    const failed = [...run.history].reverse().find((h) => h.status === 'failed');
    return {
        step: failed ?? null,
        reason: (failed?.error || run.error || '').trim() || null,
    };
}

export function FailureCard({ run, nested, onReplay, busy }: {
    run: RunRecord;
    nested: NestedRun[];
    onReplay: () => void;
    busy: boolean;
}) {
    const host = useRunHost();
    const { step, reason } = failureOf(run);
    const cancelled = run.status === 'cancelled';
    const said = explainFailure({
        status: run.status,
        reason,
        stepName: step?.step_name ?? null,
        stepId: step?.step_id ?? null,
        runId: run.id,
        newest: run.history[run.history.length - 1] ?? null,
        children: nested,
    });
    const isNested = run.id.includes('__');
    const parent = parentRunId(run.id);

    const fix = (kind: FixKind, primary: boolean) => {
        const variant = primary ? 'primary' : 'secondary';
        switch (kind) {
            case 'editor':
                return <LinkButton key={kind} href={host.links.editor({ id: run.id, workflowId: run.workflowId })} variant={variant} size="sm">Open in editor</LinkButton>;
            case 'replay':
                // A nested run is replayed by the run that called it (the API refuses it here).
                if (isNested) return parent ? <LinkButton key={kind} href={host.links.run(parent)} variant={variant} size="sm">Open the calling run</LinkButton> : null;
                return <Button key={kind} variant={variant} size="sm" onClick={onReplay} disabled={busy}>Replay</Button>;
            case 'models':
                return host.links.models ? <LinkButton key={kind} href={host.links.models} variant={variant} size="sm">Model settings</LinkButton> : null;
            case 'agents':
                return host.links.agents ? <LinkButton key={kind} href={host.links.agents} variant={variant} size="sm">Agents</LinkButton> : null;
            case 'knowledge':
                return host.links.knowledge ? <LinkButton key={kind} href={host.links.knowledge} variant={variant} size="sm">Knowledge</LinkButton> : null;
            case 'billing':
                return host.links.billing ? <LinkButton key={kind} href={host.links.billing} variant={variant} size="sm">Add balance</LinkButton> : null;
            case 'credits':
                return host.links.billing ? <LinkButton key={kind} href={host.links.billing} variant={variant} size="sm">Add credits</LinkButton> : null;
            case 'nested':
                return said.nestedRunId ? <LinkButton key={kind} href={host.links.run(said.nestedRunId)} variant={variant} size="sm">Open the nested run</LinkButton> : null;
            case 'parent':
                return parent ? <LinkButton key={kind} href={host.links.run(parent)} variant={variant} size="sm">Open the calling run</LinkButton> : null;
            case 'log':
                return <LinkButton key={kind} href="#activity" variant={variant} size="sm" external>Read the activity</LinkButton>;
        }
    };
    // The first fix this product can offer is the primary one.
    const buttons: React.ReactNode[] = [];
    for (const kind of said.fixes) {
        const node = fix(kind, buttons.length === 0);
        if (node) buttons.push(node);
    }

    const Icon = cancelled ? CircleSlash : AlertTriangle;
    return (
        <section
            aria-label={cancelled ? 'Why this run stopped' : 'Why this run failed'}
            data-kind={said.kind}
            className={cn(
                'grid gap-3 rounded-[14px] border px-[22px] py-5',
                cancelled ? 'border-border bg-surface-2' : 'border-danger/35 bg-danger-subtle',
            )}
        >
            <h2 className="m-0 flex items-start gap-2.5 text-[17px] font-semibold leading-snug tracking-[-0.012em] text-text">
                <Icon className={cn('mt-0.5 size-[18px] shrink-0', cancelled ? 'text-text-faint' : 'text-danger')} aria-hidden />
                <span>{said.sentence}</span>
            </h2>
            {said.code && (
                <pre className="m-0 max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-bg px-3 py-2 font-code text-[13px] text-text">{said.code}</pre>
            )}
            {said.detail && <p className="m-0 max-w-[66ch] break-words text-sm text-text-muted">{said.detail}</p>}
            {said.advice && <p className="m-0 max-w-[66ch] text-sm text-text">{said.advice}</p>}
            {buttons.length > 0 && <div className="flex flex-wrap gap-2">{buttons}</div>}
            {reason && (
                <details className={cn('border-t pt-2.5', cancelled ? 'border-border' : 'border-danger/20')}>
                    <summary className="cursor-pointer text-[13px] text-text-muted">Technical details</summary>
                    <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg px-3 py-2 font-code text-xs leading-relaxed text-text-muted">{reason}</pre>
                </details>
            )}
        </section>
    );
}

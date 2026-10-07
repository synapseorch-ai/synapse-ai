'use client';

/*
 * A waiting or failed run as its workflow's steps, in order, each with how far
 * the run got (UX1, M2b): done, failed, waiting on a person, working, or not
 * reached. Where the run is is the point — the timeline's seconds matter less
 * than "step 3 of 4 waits for accounts payable". A step that ran opens in place
 * to what it wrote and why it failed.
 */
import { ChevronRight } from 'lucide-react';
import { useState } from 'react';

import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

import { duration, stepperRows, type RunRecord, type StepState } from './model';
import { StepDetail, hasDetail } from './StepDetail';
import { StepIcon, typeLabel } from './StepIcon';

const STATE: Record<StepState, { label: string; dot: string; text: string }> = {
    done: { label: 'Done', dot: 'bg-success', text: 'text-text-muted' },
    failed: { label: 'Failed', dot: 'bg-danger', text: 'font-medium text-danger' },
    waiting: { label: 'Needs input', dot: 'bg-person motion-safe:animate-pulse', text: 'font-medium text-person-ink' },
    running: { label: 'Working', dot: 'bg-machine motion-safe:animate-pulse', text: 'text-accent-ink' },
    cancelled: { label: 'Cancelled', dot: 'bg-text-faint', text: 'text-text-faint' },
    'not-reached': { label: 'Not reached', dot: '', text: 'text-text-faint' },
};

export function RunStepper({ run, workflow }: { run: RunRecord; workflow: Orchestration | null }) {
    const rows = stepperRows(run, workflow);
    const [open, setOpen] = useState<Set<string>>(new Set());
    if (rows.length === 0) return <p className="text-sm text-text-muted">This run recorded no steps.</p>;
    const toggle = (id: string) => setOpen((cur) => {
        const next = new Set(cur);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
    });
    return (
        <ol aria-label="Steps" className="grid">
            {rows.map((row) => {
                const entry = row.index !== undefined ? run.history[row.index] : undefined;
                const usage = entry ? run.stepUsage?.[entry.step_id] : undefined;
                const expandable = Boolean(entry && hasDetail(entry, run.sharedState, usage));
                const isOpen = open.has(row.stepId);
                const state = STATE[row.state];
                const took = entry?.duration_seconds;
                const note = [
                    typeLabel(row.type),
                    row.times ? `ran ${row.times} times` : null,
                    row.state === 'done' && typeof took === 'number' ? duration(took) : null,
                ].filter(Boolean).join(' · ');
                const name = <span className={cn('truncate font-medium', row.state === 'not-reached' ? 'text-text-muted' : 'text-text')}>{row.name}</span>;
                return (
                    <li key={row.stepId} className="border-b border-border">
                        <div className="grid min-h-[50px] grid-cols-[30px_minmax(0,1fr)_auto] items-center gap-x-3.5 px-2.5" style={{ paddingLeft: row.depth ? `${10 + row.depth * 18}px` : undefined }}>
                            <StepIcon type={row.type} className={cn(row.state === 'not-reached' && 'opacity-60')} />
                            <span className="grid min-w-0 gap-0.5 py-2">
                                {expandable ? (
                                    <button
                                        type="button"
                                        onClick={() => toggle(row.stepId)}
                                        aria-expanded={isOpen}
                                        aria-controls={`step-output-${row.stepId}`}
                                        aria-label={`${isOpen ? 'Hide' : 'Show'} output of ${row.name}`}
                                        className="flex min-w-0 items-center gap-1.5 rounded text-left"
                                    >
                                        {name}
                                        <ChevronRight className={cn('size-3.5 shrink-0 text-text-faint transition-transform', isOpen && 'rotate-90')} aria-hidden />
                                    </button>
                                ) : name}
                                <span className="truncate text-xs text-text-faint">{note}</span>
                            </span>
                            <span className={cn('inline-flex items-center gap-2 whitespace-nowrap text-[13px]', state.text)}>
                                {state.dot && <span aria-hidden className={cn('size-2 rounded-full', state.dot)} />}
                                {state.label}
                            </span>
                        </div>
                        {expandable && isOpen && entry && (
                            <div className="px-2.5 pb-3" style={{ paddingLeft: `${54 + row.depth * 18}px` }}>
                                <StepDetail id={`step-output-${row.stepId}`} step={entry} sharedState={run.sharedState} usage={usage} />
                            </div>
                        )}
                    </li>
                );
            })}
        </ol>
    );
}

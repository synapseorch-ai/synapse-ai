"use client";

/*
 * A helper's lane in a conversation (SUB1a, S7, S9; TEAM0's artboard 2), and
 * the note a background helper's result is posted under.
 *
 * The lane shows what the helper is doing as it does it — its newest calls, its
 * answer as it is written — with Stop while it works; when it has ended, how it
 * went, "View its work" (every call and its answer) and "Save as agent", which
 * opens a new agent made from it that nothing saves until the person does.
 * Cool colours: the product at work. Green and red only for how it ended.
 *
 * Props and callbacks only (this folder's rule): the product says how to stop
 * one and where "Save as agent" goes.
 */
import React from 'react';
import { Bot, Square, Bookmark, ChevronRight } from 'lucide-react';

import { cn } from '@/lib/cn';
import { Button } from '@/components/ui';

import { helperStatusLine, type HelperLaneData } from './helpers';

/** How many of its newest calls a working lane shows. */
const SHOWN_CALLS = 3;

function callLine(call: HelperLaneData['calls'][number]): string {
    const args = call.args && typeof call.args === 'object'
        ? Object.values(call.args).filter((v) => typeof v === 'string' || typeof v === 'number').slice(0, 2).join(', ')
        : '';
    const result = (call.result ?? '').replace(/\s+/g, ' ').trim();
    return [args, call.refused ? `not run: ${call.reason || 'refused'}` : result].filter(Boolean).join(' · ');
}

const DOT: Record<string, string> = {
    completed: 'bg-success',
    failed: 'bg-danger',
    timed_out: 'bg-danger',
    cancelled: 'bg-text-faint',
};

export function HelperLane({
    helper, onStop, onSaveAsAgent, stopping = false, className,
}: {
    helper: HelperLaneData;
    /** Stop it (only offered while it works). */
    onStop?: () => void;
    /** Open a new, unsaved agent made from it. */
    onSaveAsAgent?: () => void;
    /** Stop was pressed and the helper has not ended yet. */
    stopping?: boolean;
    className?: string;
}) {
    const [open, setOpen] = React.useState(false);
    const working = helper.status === 'running';
    const calls = working ? helper.calls.slice(-SHOWN_CALLS) : helper.calls;
    const statusId = `helper-${helper.id}-status`;

    return (
        <section
            aria-label={`Helper: ${helper.name}`}
            aria-describedby={statusId}
            className={cn('rounded-lg border',
                helper.status === 'running' ? 'border-machine/30 bg-machine-subtle/40' : 'border-border bg-surface',
                className)}
        >
            <div className="flex items-start gap-3 px-3 py-2.5">
                <span aria-hidden className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md bg-machine-subtle text-accent-ink">
                    <Bot className="size-3.5" />
                </span>
                <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-text">
                        <span className="text-text-muted">Helper · </span>{helper.name}
                    </p>
                    <p id={statusId} className="mt-0.5 flex items-center gap-1.5 text-xs text-text-muted" aria-live="polite">
                        <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full',
                            DOT[helper.status] ?? 'bg-machine motion-safe:animate-pulse')} />
                        {stopping && working ? 'Stopping…' : helperStatusLine(helper)}
                    </p>
                </div>
                {working && onStop && (
                    <Button size="sm" variant="secondary" onClick={onStop} disabled={stopping}
                            aria-label={`Stop the helper ${helper.name}`}>
                        <Square aria-hidden className="size-3" /> Stop
                    </Button>
                )}
            </div>

            {working && (calls.length > 0 || helper.streaming || helper.activity) && (
                <div className="space-y-1 border-t border-border px-3 py-2 font-mono text-xs text-text-muted">
                    {calls.map((call, i) => (
                        <p key={`${call.tool}-${i}`} className="truncate">
                            <span className="text-text">{call.tool}</span>
                            {callLine(call) && <span> · {callLine(call)}</span>}
                            {!call.result && !call.refused && <span className="text-text-faint"> …</span>}
                        </p>
                    ))}
                    {helper.calls.length > SHOWN_CALLS && (
                        <p className="font-sans text-text-faint">{helper.calls.length} calls so far</p>
                    )}
                    {helper.streaming && <p className="whitespace-pre-wrap font-sans text-text">{helper.streaming}</p>}
                    {!helper.streaming && helper.activity && calls.length === 0 && (
                        <p className="font-sans text-text-faint">{helper.activity}</p>
                    )}
                </div>
            )}

            {!working && (
                <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2">
                    <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
                        <ChevronRight aria-hidden className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
                        View its work
                    </Button>
                    {onSaveAsAgent && (
                        <Button size="sm" variant="ghost" onClick={onSaveAsAgent}>
                            <Bookmark aria-hidden className="size-3.5" /> Save as agent
                        </Button>
                    )}
                </div>
            )}

            {!working && open && (
                <div className="space-y-3 border-t border-border px-3 py-3 text-xs">
                    <div>
                        <p className="mb-1 font-medium text-text-muted">What it was asked</p>
                        <p className="whitespace-pre-wrap text-text">{helper.task}</p>
                    </div>
                    {helper.withheld && helper.withheld.length > 0 && (
                        <p className="text-text-muted">Not given: {helper.withheld.join('; ')}</p>
                    )}
                    {helper.calls.length > 0 && (
                        <div>
                            <p className="mb-1 font-medium text-text-muted">Its calls</p>
                            <ol className="space-y-1.5">
                                {helper.calls.map((call, i) => (
                                    <li key={`${call.tool}-${i}`} className="rounded-md bg-surface-2 px-2 py-1.5 font-mono">
                                        <p className="text-text">{call.tool}
                                            {call.args && Object.keys(call.args).length > 0 && (
                                                <span className="text-text-faint"> {JSON.stringify(call.args)}</span>
                                            )}
                                        </p>
                                        <p className={cn('mt-0.5 whitespace-pre-wrap break-words',
                                            call.refused ? 'text-danger' : 'text-text-muted')}>
                                            {call.refused ? `Not run: ${call.reason || 'refused'}` : (call.result || '(no output)')}
                                        </p>
                                    </li>
                                ))}
                            </ol>
                        </div>
                    )}
                    <div>
                        <p className="mb-1 font-medium text-text-muted">Its answer</p>
                        <p className="whitespace-pre-wrap text-text">{helper.answer || (helper.error ? `Failed: ${helper.error}` : '(none)')}</p>
                    </div>
                    {helper.model && <p className="text-text-faint">Ran on {helper.model}</p>}
                </div>
            )}
        </section>
    );
}

/** Several lanes, in the order they started. */
export function HelperLanes({ helpers, ...rest }: {
    helpers: HelperLaneData[];
    onStop?: (helper: HelperLaneData) => void;
    onSaveAsAgent?: (helper: HelperLaneData) => void;
    stopping?: Set<string>;
    className?: string;
}) {
    if (helpers.length === 0) return null;
    return (
        <div className={cn('flex flex-col gap-2', rest.className)}>
            {helpers.map((helper) => (
                <HelperLane
                    key={helper.id}
                    helper={helper}
                    stopping={rest.stopping?.has(helper.id)}
                    onStop={rest.onStop ? () => rest.onStop?.(helper) : undefined}
                    onSaveAsAgent={rest.onSaveAsAgent ? () => rest.onSaveAsAgent?.(helper) : undefined}
                />
            ))}
        </div>
    );
}

/**
 * Over a message nobody typed: a background helper's result, posted when it
 * finished (S2, artboard 2). Never the person's bubble: the agent's reply below
 * it says what was found, and the result itself opens under "Its result".
 */
export function HelperNote({ name, text, status, className }: {
    name: string;
    text: string;
    status?: string;
    className?: string;
}) {
    const [open, setOpen] = React.useState(false);
    const ended = status && status !== 'completed'
        ? (status === 'timed_out' ? 'it ran out of time' : status === 'failed' ? 'it failed' : 'it stopped')
        : null;
    return (
        <section aria-label={`From your helper: ${name}`} className={cn('flex flex-col gap-1.5', className)}>
            <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="inline-flex h-6 items-center gap-1.5 rounded-md bg-machine-subtle px-2 font-medium text-accent-ink">
                    <Bot aria-hidden className="size-3.5" />
                    From your helper · {name}
                </span>
                <span className="text-text-muted">
                    Automated: posted when the helper finished{ended ? ` (${ended})` : ''}, not something you said
                </span>
            </div>
            {text && (
                <div>
                    <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}
                            className="inline-flex items-center gap-1 text-xs text-text-faint transition-colors hover:text-text-muted">
                        <ChevronRight aria-hidden className={cn('size-3 transition-transform', open && 'rotate-90')} />
                        Its result
                    </button>
                    {open && <p className="mt-1 whitespace-pre-wrap rounded-md bg-surface-2 px-3 py-2 text-xs text-text">{text}</p>}
                </div>
            )}
        </section>
    );
}

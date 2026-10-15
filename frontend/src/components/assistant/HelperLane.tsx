"use client";

/*
 * A helper in a conversation (SUB1a, S7, S9): one line, the way a turn's "Tools
 * used" and "Reasoning" are, closed by default. While it works the line says what
 * it is thinking (or just "working"), with Stop beside it; opened, it shows its
 * brief, its calls, its answer and "Save as agent", which opens a new agent made
 * from it that nothing saves until the person does. Kept plain on purpose
 * (Naveen, 2026-10-15): close to how the rest of an agent's turn looks.
 *
 * Props and callbacks only (this folder's rule): the product says how to stop
 * one and where "Save as agent" goes.
 */
import React from 'react';
import { Bot, Square, Bookmark, ChevronRight } from 'lucide-react';

import { cn } from '@/lib/cn';
import { Button } from '@/components/ui';

import { helperStatusLine, type HelperLaneData } from './helpers';

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
    const status = stopping && working ? 'stopping…' : working && helper.activity
        ? helper.activity : helperStatusLine(helper).toLowerCase();
    const chip = 'inline-flex min-w-0 max-w-full items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-text-muted';

    return (
        <section aria-label={`Helper: ${helper.name}`} className={cn('flex min-w-0 flex-col items-start gap-1.5', className)}>
            <div className="flex min-w-0 max-w-full items-center gap-2">
                <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpen((v) => !v)}
                    className={cn(chip, 'transition-colors hover:border-border-strong hover:text-text')}
                >
                    <Bot aria-hidden className={cn('size-3.5 shrink-0 text-machine', working && 'motion-safe:animate-pulse')} />
                    <span className="shrink-0 text-text">Helper · {helper.name}</span>
                    <span className="min-w-0 truncate" aria-live="polite">· {status}</span>
                    <ChevronRight aria-hidden className={cn('size-3.5 shrink-0 text-text-faint transition-transform', open && 'rotate-90')} />
                </button>
                {working && onStop && (
                    <Button size="sm" variant="ghost" onClick={onStop} disabled={stopping}
                            aria-label={`Stop the helper ${helper.name}`}>
                        <Square aria-hidden className="size-3" /> Stop
                    </Button>
                )}
            </div>

            {open && (
                <div className="w-full min-w-0 space-y-3 border-l border-border pl-3 text-xs">
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
                                            {call.refused ? `Not run: ${call.reason || 'refused'}`
                                                : (call.result || (working ? '…' : '(no output)'))}
                                        </p>
                                    </li>
                                ))}
                            </ol>
                        </div>
                    )}
                    {(helper.answer || helper.streaming || !working) && (
                        <div>
                            <p className="mb-1 font-medium text-text-muted">Its answer</p>
                            <p className="whitespace-pre-wrap text-text">
                                {helper.answer || helper.streaming || (helper.error ? `Failed: ${helper.error}` : '(none)')}
                            </p>
                        </div>
                    )}
                    {!working && (
                        <div className="flex flex-wrap items-center gap-3">
                            {helper.model && <span className="text-text-faint">Ran on {helper.model}</span>}
                            {onSaveAsAgent && (
                                <Button size="sm" variant="ghost" onClick={onSaveAsAgent}>
                                    <Bookmark aria-hidden className="size-3.5" /> Save as agent
                                </Button>
                            )}
                        </div>
                    )}
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
 * finished (S2). Just who it is from; the agent's reply below says what it found.
 */
export function HelperNote({ name, className }: {
    name: string;
    className?: string;
}) {
    return (
        <section aria-label={`From your helper: ${name}`} className={className}>
            <span className="inline-flex h-6 items-center gap-1.5 rounded-md bg-machine-subtle px-2 text-xs font-medium text-accent-ink">
                <Bot aria-hidden className="size-3.5" />
                From your helper · {name}
            </span>
        </section>
    );
}

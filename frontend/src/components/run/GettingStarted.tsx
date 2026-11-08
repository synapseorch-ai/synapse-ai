'use client';

/*
 * A new workspace's first workflow, as a path (M2a; Naveen: "for newly onboarded
 * company lets bring in the steps in home with tips and something different").
 *
 * The steps tick themselves from what the workspace has (`startSteps`): a done
 * step says what was done, the current one says what to do, with its action and
 * one tip, and the rest wait their turn. Under them, the shortest way in:
 * describe what to automate, and Build with AI drafts it, or pick a suggestion.
 * It goes away once the path is done, or on "Don't show again". Once a workflow
 * has run, it folds to one line — what is next, and Show — so Home stays
 * approval-first for a team at work (Naveen's call); Show and Hide are
 * remembered in this browser.
 *
 * The words and the order are shared; what each step's button does is the
 * product's (`actions`), and so is where a suggestion goes.
 */
import Link from 'next/link';
import { useState, type ReactNode } from 'react';
import { Check, Lightbulb } from 'lucide-react';

import { Button, Mark, sectionClass, usePersisted } from '@/components/ui';
import { cn } from '@/lib/cn';

import { startDone, type StartStep, type StartStepId } from './home';

export type StartSuggestion = { label: string; prompt?: string; href?: string };

export function GettingStarted({ steps, actions, onDismiss, describe, foldable = false }: {
    steps: StartStep[];
    /** Fold to one line (once a workflow has run). */
    foldable?: boolean;
    /** Each step's own button or links, from the product. */
    actions: Partial<Record<StartStepId, ReactNode>>;
    onDismiss: () => void;
    /**
     * Describe it to Build with AI: what opens the builder with the text, and a few things to try. `starters` (ONB3,
     * F1059) are the prompts written for this person (`components/starters`), shown above the field.
     */
    describe?: { onSubmit: (text: string) => void; suggestions: StartSuggestion[]; starters?: ReactNode };
}) {
    const [text, setText] = useState('');
    const [opened, setOpened] = usePersisted<'0' | '1'>('synapseHomeStartOpen', (raw) => (raw === '1' ? '1' : '0'), '0');
    const needed = steps.filter((s) => !s.optional);
    const doneCount = needed.filter((s) => s.done).length;
    const current = steps.find((s) => !s.done && !s.optional) ?? steps.find((s) => !s.done) ?? null;
    if (startDone(steps) && !current) return null;

    const dots = (
        <span className="flex items-center" aria-hidden>
            {steps.map((s, i) => (
                <span key={s.id} className="flex items-center">
                    {i > 0 && <i className={cn('block h-px w-5 sm:w-7', steps[i - 1].done ? 'bg-text-faint' : 'bg-border-strong')} />}
                    <i
                        className={cn(
                            'block size-2.5 rounded-full',
                            s.done ? 'bg-success'
                                : s === current ? 'bg-accent ring-4 ring-accent-subtle'
                                    : s.optional ? 'border border-dashed border-border-strong bg-surface'
                                        : 'border border-border-strong bg-surface',
                        )}
                    />
                </span>
            ))}
        </span>
    );

    if (foldable && opened !== '1') {
        return (
            <section aria-labelledby="home-start" className="relative flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 rounded-[14px] border border-border bg-surface py-2.5 pl-[18px] pr-2.5">
                <h2 id="home-start" className="m-0 text-sm font-semibold text-text">Getting started</h2>
                {dots}
                <span className="text-xs tabular-nums text-text-faint">{doneCount} of {needed.length}</span>
                {current && <span className="min-w-0 truncate text-sm text-text-muted">Next: {current.title}</span>}
                <span className="hidden flex-1 sm:block" />
                <Button size="sm" variant="ghost" onClick={() => setOpened('1')} aria-expanded={false}>
                    Show
                </Button>
            </section>
        );
    }

    return (
        <section aria-labelledby="home-start" className="relative grid min-w-0 gap-4 rounded-[14px] border border-border bg-surface px-[22px] py-5 shadow-elevated">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                <h2 id="home-start" className={sectionClass}>Getting started</h2>
                <div className="flex items-center gap-3">
                    {dots}
                    <span className="text-xs tabular-nums text-text-faint">{doneCount} of {needed.length}</span>
                    {foldable && (
                        <Button size="sm" variant="ghost" onClick={() => setOpened('0')} aria-expanded>
                            Hide
                        </Button>
                    )}
                </div>
            </div>

            <ol className="m-0 grid list-none gap-0 p-0" aria-label="Your first workflow">
                {steps.map((s, i) => {
                    const isCurrent = s === current;
                    return (
                        <li
                            key={s.id}
                            aria-current={isCurrent ? 'step' : undefined}
                            className={cn('grid grid-cols-[24px_minmax(0,1fr)] gap-x-3 border-t border-border py-3 first:border-t-0 first:pt-1')}
                        >
                            <span
                                className={cn(
                                    'mt-px grid size-6 place-items-center rounded-full text-xs font-semibold tabular-nums',
                                    s.done ? 'bg-success-subtle text-success'
                                        : isCurrent ? 'bg-accent text-accent-fg'
                                            : 'border border-border-strong text-text-faint',
                                )}
                                aria-hidden
                            >
                                {s.done ? <Check className="size-3.5" strokeWidth={2.5} /> : i + 1}
                            </span>
                            <div className="min-w-0">
                                <p className={cn('m-0 leading-6', isCurrent ? 'text-[15px] font-medium text-text' : s.done ? 'text-sm text-text-muted' : 'text-sm text-text-muted')}>
                                    {s.title}
                                    {s.done && s.doneText && <span className="text-text-faint"> · {s.doneText}</span>}
                                    {!s.done && s.optional && <span className="text-text-faint"> · optional</span>}
                                    <span className="sr-only">{s.done ? ' (done)' : isCurrent ? ' (next)' : ''}</span>
                                </p>
                                {isCurrent && (
                                    <div className="mt-1 grid gap-2.5">
                                        <p className="m-0 max-w-[66ch] text-sm text-text-muted">{s.detail}</p>
                                        {actions[s.id] && <div className="flex flex-wrap items-center gap-2">{actions[s.id]}</div>}
                                        {s.tip && (
                                            <p className="m-0 flex max-w-[66ch] items-start gap-1.5 text-xs text-text-faint">
                                                <Lightbulb className="mt-px size-3.5 shrink-0" aria-hidden />
                                                <span>{s.tip}</span>
                                            </p>
                                        )}
                                    </div>
                                )}
                            </div>
                        </li>
                    );
                })}
            </ol>

            {describe && (
                <form
                    className="grid min-w-0 gap-2.5 border-t border-border pt-4"
                    onSubmit={(e) => { e.preventDefault(); describe.onSubmit(text.trim()); }}
                >
                    {describe.starters}
                    <label htmlFor="home-describe" className="text-xs font-semibold tracking-[0.02em] text-text-faint">
                        Or describe what you want to automate
                    </label>
                    <div className="flex items-center gap-2 rounded-xl border border-border-strong bg-bg py-1.5 pl-3 pr-1.5 transition-colors focus-within:border-accent">
                        <Mark size={20} compact={false} className="text-accent" />
                        <input
                            id="home-describe"
                            data-no-focus-ring
                            // No intrinsic width: on a phone the field shrinks to what is left beside the button.
                            size={1}
                            value={text}
                            onChange={(e) => setText(e.target.value)}
                            placeholder="Every Monday, check new supplier invoices and ask me before paying any over $1,000"
                            className="min-w-0 flex-1 bg-transparent py-1 text-sm text-text outline-none placeholder:text-text-faint"
                        />
                        <Button size="sm" type="submit">Build with AI</Button>
                    </div>
                    {describe.suggestions.length > 0 && (
                        <div className="flex flex-wrap gap-2" aria-label="Things to try">
                            {describe.suggestions.map((s) => {
                                const chip = 'inline-flex h-8 items-center rounded-full border border-border px-3 text-[13px] text-text-muted transition-colors hover:border-border-strong hover:text-text';
                                return s.href ? (
                                    <Link key={s.label} href={s.href} className={chip}>{s.label}</Link>
                                ) : (
                                    <button key={s.label} type="button" className={chip} onClick={() => describe.onSubmit(s.prompt ?? s.label)}>
                                        {s.label}
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </form>
            )}

            <div className="flex justify-end">
                <button type="button" onClick={onDismiss} className="rounded-sm text-xs text-text-faint transition-colors hover:text-text">
                    Don’t show again
                </button>
            </div>
        </section>
    );
}

/** Things to try when the product has nothing of the workspace's own to suggest. */
export const START_SUGGESTIONS: StartSuggestion[] = [
    { label: 'Triage supplier invoices', prompt: 'Check each new supplier invoice against our terms, and ask me before paying any over $1,000.' },
    { label: 'A weekly report', prompt: 'Every Monday at 9, write a short report of last week’s work and send it to me.' },
    { label: 'Reply to customer emails', prompt: 'Draft replies to customer emails from our help docs, and let me approve each one before it is sent.' },
    { label: 'Watch competitors', prompt: 'Every morning, check our competitors’ pricing pages and tell me what changed.' },
];

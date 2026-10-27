'use client';

/*
 * A person's first prompts, written for them (ONB3, D326, F1059): one or two rows from `pickStarters`, each a
 * press that opens a new Synapse AI chat with its words waiting, unsent. A row that needs an app says so; Synapse AI
 * asks to connect it after the person's yes. OSS Home adds "I work in", which swaps the rows. Each product passes
 * where a press goes (`onPick`); nothing here sends anything.
 */
import { ArrowRight } from 'lucide-react';

import { Segmented } from '@/components/ui';

import type { StarterPick } from './pick';
import { WORK_IN } from './pick';
import { NEED_LABELS, type Persona } from './table';

/** What a row that needs an app says under its words. */
export function needsLine(needs: readonly string[]): string | null {
    if (!needs.length) return null;
    const names = needs.map(n => NEED_LABELS[n] ?? n);
    return `Needs ${names.join(' and ')} · you’ll connect it after your yes`;
}

export function StarterPrompts({ picks, onPick, workIn }: {
    picks: StarterPick[];
    onPick: (text: string) => void;
    /** OSS Home's "I work in": the chosen persona and how to change it. */
    workIn?: { value: Persona; onChange: (p: Persona) => void };
}) {
    return (
        <div className="grid min-w-0 gap-2.5" data-testid="starters">
            {workIn && (
                <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
                    <span id="starters-work-in" className="text-xs font-semibold tracking-[0.02em] text-text-faint">I work in</span>
                    <Segmented
                        label="I work in"
                        id="starters-work-in"
                        value={workIn.value}
                        onChange={workIn.onChange}
                        options={WORK_IN.map(w => ({ value: w.key, label: w.label }))}
                    />
                </div>
            )}
            <ul className="m-0 grid list-none gap-2 p-0" aria-label="Prompts written for you">
                {picks.map(p => {
                    const needs = needsLine(p.needs);
                    return (
                        <li key={p.id}>
                            <button
                                type="button"
                                data-testid="starter"
                                data-starter={p.id}
                                onClick={() => onPick(p.text)}
                                className="group grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-xl border border-accent/40 bg-surface px-4 py-3 text-left transition-colors hover:border-accent hover:bg-accent-subtle focus-visible:border-accent"
                            >
                                <span className="grid min-w-0 gap-1">
                                    <span className="text-sm font-medium text-text">{p.title}</span>
                                    <span className="text-[13px] leading-5 text-text-muted">{p.text}</span>
                                    {needs && <span className="text-xs text-text-faint">{needs}</span>}
                                </span>
                                <ArrowRight className="size-4 shrink-0 text-accent transition-transform group-hover:translate-x-0.5" aria-hidden />
                            </button>
                        </li>
                    );
                })}
            </ul>
        </div>
    );
}

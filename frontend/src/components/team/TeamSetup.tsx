"use client";

/*
 * A Team agent's setup (artboard 1; Q1, Q2a, Q8): how its members work, how many at once,
 * its task budget and an optional spend limit. Stored as the agent's `team` block; an
 * agent without one works one member at a time, as every delegate did before (D204), and
 * nothing here writes the block until the person changes something.
 *
 * Props only (this folder's rule): each product passes the block, its ceiling and where
 * that ceiling comes from (an install's Settings, a plan).
 */
import React from 'react';

import { cn } from '@/lib/cn';
import { Input } from '@/components/ui';

export type TeamMode = 'one_at_a_time' | 'team';

export type TeamBlock = {
    mode: TeamMode;
    at_once: number;
    task_budget: number;
    spend_limit_usd: number | null;
};

export const TEAM_DEFAULTS: TeamBlock = { mode: 'team', at_once: 3, task_budget: 6, spend_limit_usd: null };

/** The block a new Team agent starts with: as a team (Q1). */
export function newTeamBlock(): TeamBlock {
    return { ...TEAM_DEFAULTS };
}

/** How an agent's members work: one at a time unless its block says team. */
export function teamMode(block: Partial<TeamBlock> | null | undefined): TeamMode {
    return block?.mode === 'team' ? 'team' : 'one_at_a_time';
}

const MODES: { value: TeamMode; title: string; body: string }[] = [
    { value: 'one_at_a_time', title: 'One at a time',
      body: 'Hands each task to one member and waits for it. How these agents worked until now.' },
    { value: 'team', title: 'As a team',
      body: 'Members work in parallel, write to the lead while they work, and share one task list.' },
];

function NumberField({ id, label, hint, value, min, max, onCommit, optional = false, step, prefix, disabled }: {
    id: string;
    label: string;
    hint?: React.ReactNode;
    value: number | null;
    min: number;
    max?: number;
    onCommit: (value: number | null) => void;
    optional?: boolean;
    step?: string;
    prefix?: string;
    disabled?: boolean;
}) {
    const [text, setText] = React.useState(value === null ? '' : String(value));
    React.useEffect(() => { setText(value === null ? '' : String(value)); }, [value]);
    const commit = () => {
        const raw = text.trim();
        if (!raw) {
            if (optional) onCommit(null);
            else setText(value === null ? '' : String(value));
            return;
        }
        const n = Number(raw);
        if (!Number.isFinite(n) || n <= 0) {
            setText(value === null ? '' : String(value));
            return;
        }
        const whole = step ? n : Math.round(n);
        const clamped = Math.max(min, max !== undefined ? Math.min(max, whole) : whole);
        setText(String(clamped));
        if (clamped !== value) onCommit(clamped);
    };
    const hintId = hint ? `${id}-hint` : undefined;
    return (
        <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor={id} className="text-sm font-medium text-text">{label}</label>
            <div className="flex items-center gap-1.5">
                {prefix && <span className="text-sm text-text-muted">{prefix}</span>}
                <Input id={id} type="number" inputMode={step ? 'decimal' : 'numeric'} min={min} max={max} step={step}
                       value={text} disabled={disabled} aria-describedby={hintId}
                       placeholder={optional ? 'No limit' : undefined}
                       onChange={(e) => setText(e.target.value)} onBlur={commit}
                       onKeyDown={(e) => { if (e.key === 'Enter') commit(); }}
                       className="w-28" />
            </div>
            {hint && <p id={hintId} className="text-xs text-text-faint">{hint}</p>}
        </div>
    );
}

export function TeamSetup({ block, onChange, ceiling, ceilingNote, members, disabled = false, className }: {
    /** The agent's `team` block; null for one that has none (one at a time). */
    block: Partial<TeamBlock> | null | undefined;
    onChange: (block: TeamBlock) => void;
    /** The most members that may work at once here (the install's or the plan's). */
    ceiling: number;
    /** Where that ceiling comes from, e.g. "This install allows up to 10 (Settings → General)". */
    ceilingNote?: React.ReactNode;
    /** The product's member picker, drawn under "How members work" (its candidate rules are the product's). */
    members?: React.ReactNode;
    disabled?: boolean;
    className?: string;
}) {
    const mode = teamMode(block);
    const full: TeamBlock = { ...TEAM_DEFAULTS, ...(block ?? {}), mode } as TeamBlock;
    const set = (change: Partial<TeamBlock>) => onChange({ ...full, ...change });

    return (
        <div className={cn('flex min-w-0 flex-col gap-4', className)}>
            <fieldset className="min-w-0" disabled={disabled}>
                <legend className="mb-2 text-sm font-medium text-text">How members work</legend>
                <div role="radiogroup" aria-label="How members work" className="grid gap-2 sm:grid-cols-2">
                    {MODES.map((m) => {
                        const on = mode === m.value;
                        return (
                            <label key={m.value}
                                   className={cn('flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors',
                                       on ? 'border-accent bg-accent-subtle/40' : 'border-border hover:border-border-strong')}>
                                <input type="radio" name="team-mode" value={m.value} checked={on}
                                       onChange={() => set({ mode: m.value })}
                                       className="mt-0.5 size-4 shrink-0 accent-[var(--color-accent)]" />
                                <span className="min-w-0">
                                    <span className="block text-sm font-medium text-text">{m.title}</span>
                                    <span className="block text-xs text-text-muted">{m.body}</span>
                                </span>
                            </label>
                        );
                    })}
                </div>
            </fieldset>

            {members}

            {mode === 'team' && (
                <>
                    <div className="grid gap-4 sm:grid-cols-3">
                        <NumberField id="team-at-once" label="Members working at once" value={Math.min(full.at_once, ceiling)}
                                     min={1} max={ceiling} disabled={disabled}
                                     hint={ceilingNote ?? `Up to ${ceiling}. Others wait for a free slot.`}
                                     onCommit={(v) => set({ at_once: v ?? TEAM_DEFAULTS.at_once })} />
                        <NumberField id="team-task-budget" label="Task budget per turn" value={full.task_budget}
                                     min={1} max={50} disabled={disabled}
                                     hint="Tasks the lead may hand out in one turn, helpers included."
                                     onCommit={(v) => set({ task_budget: v ?? TEAM_DEFAULTS.task_budget })} />
                        <NumberField id="team-spend-limit" label="Spend limit per turn" value={full.spend_limit_usd}
                                     min={0.01} step="0.01" prefix="$" optional disabled={disabled}
                                     hint="Optional. Model costs, lead and members together."
                                     onCommit={(v) => set({ spend_limit_usd: v })} />
                    </div>
                    <p className="text-xs text-text-muted">
                        Checked before every model call. When the budget or the limit is used, the lead finishes with
                        what its members have found. A team costs what its members&apos; work would cost one by one:
                        working together saves time, not money.
                    </p>
                </>
            )}

            <p className="text-xs text-text-faint">
                {mode === 'team'
                    ? 'Works as a team in chat and in your channels. In a workflow’s Agent step its members work one at a time for now, and a visitor on your widget talks to the lead alone.'
                    : 'Works in chat, in your channels and as a workflow’s Agent step. A visitor on your widget talks to the lead alone.'}
            </p>
        </div>
    );
}

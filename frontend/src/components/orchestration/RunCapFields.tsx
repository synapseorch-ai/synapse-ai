'use client';
/**
 * A workflow's cap on what one run may spend (CAP1, D272), under Cost per run.
 *
 * The engine asks before every step what the run has spent, and a run at its
 * cap stops before the step that would take it past, in its own words. Credits
 * are cloud's — self-hosted has none, so it caps dollars only — counted the way
 * a run is charged: its steps, or its minutes when those are more. Dollars are
 * the run's model cost as its page shows it. Empty is no cap, the default.
 *
 * Under the fields, how the cap sits against what a run costs (CE1's estimate):
 * one below the cheapest run stops every run; one inside the range stops the
 * dearer ones. Words, never a refusal: the cheapest path is not a floor (a loop
 * over an empty list runs nothing), and a false stop is the worst bug (F109).
 *
 * Synced into the cloud app; nothing here calls an API.
 */
import { AlertTriangle } from 'lucide-react';
import { useState } from 'react';

import { creditsText } from '@/components/run/model';
import { Field, Input } from '@/components/ui';
import type { Orchestration } from '@/types/orchestration';

import { aboutUsd, type CapUnit, type CostSpan, type RunEstimate } from './estimate';

const FIELD = { credits: 'max_run_credits', usd: 'max_run_usd' } as const;

/** A typed cap's problem, or '' — the save refuses the same (the engine's `run_cap.clean`). */
export function capError(unit: CapUnit, text: string): string {
    const typed = text.trim();
    if (!typed) return '';
    const n = Number(typed);
    if (!Number.isFinite(n)) return unit === 'credits' ? 'Type a number of credits.' : 'Type a dollar amount.';
    if (unit === 'credits' && n < 1) return 'At least 1 credit: every run is charged one.';
    if (unit === 'usd' && n <= 0) return 'More than $0, or leave it empty for no cap.';
    return '';
}

/**
 * The model figure a dollar cap is measured against: its past runs' once it has
 * them, else the sides that pay, added — the cap counts the wallet and your own
 * keys alike, so its measure does too.
 */
export function modelSpan(estimate: RunEstimate): CostSpan | null {
    if (estimate.basis === 'runs' && estimate.runs) return estimate.runs;
    const paying = Object.entries(estimate.models.sides).filter(([side]) => side !== 'local');
    if (!paying.length) return null;
    return {
        low: paying.reduce((sum, [, span]) => sum + span.low, 0),
        high: paying.reduce((sum, [, span]) => sum + span.high, 0),
    };
}

/** How a cap sits against what a run costs, in words; null where it sits above every run. */
export function capVersus(cap: number, span: CostSpan | null | undefined, say: (n: number) => string): string | null {
    if (!span || !(cap > 0)) return null;
    if (cap < span.low) return `Its cheapest run costs about ${say(span.low)}, so this cap stops every run before it ends.`;
    if (cap < span.high) return `Its dearest runs cost about ${say(span.high)} and may stop at this cap.`;
    return null;
}

function shown(value: number | null | undefined): string {
    return value == null ? '' : String(value);
}

function parsed(text: string): number | null {
    const typed = text.trim();
    return typed && Number.isFinite(Number(typed)) ? Number(typed) : null;
}

/** One cap's text, kept while it is typed and following the draft when it changes elsewhere (undo, the builder). */
function useCapText(value: number | null | undefined) {
    const [text, setText] = useState(() => shown(value));
    const [seen, setSeen] = useState(value ?? null);
    if (seen !== (value ?? null)) {
        setSeen(value ?? null);
        if (parsed(text) !== (value ?? null)) setText(shown(value));
    }
    return { text, setText, setSeen };
}

export function RunCapFields({ draft, onChange, units, estimate, readOnly }: {
    draft: Orchestration;
    onChange: (next: Orchestration) => void;
    /** What this product caps: cloud both, self-hosted `['usd']`. */
    units: readonly CapUnit[];
    /** What a run costs (CE1), for the lines that say how the cap sits against it. */
    estimate?: RunEstimate | null;
    readOnly?: boolean;
}) {
    const credits = useCapText(draft.max_run_credits);
    const usd = useCapText(draft.max_run_usd);
    const texts = { credits, usd };

    const edit = (unit: CapUnit, text: string) => {
        const state = texts[unit];
        const value = parsed(text);
        state.setText(text);
        state.setSeen(value);
        // What was typed is what is saved: a cap the save refuses says why (422), never saves as none.
        onChange({ ...draft, [FIELD[unit]]: value });
    };

    const creditCap = units.includes('credits') ? draft.max_run_credits ?? null : null;
    const usdCap = units.includes('usd') ? draft.max_run_usd ?? null : null;
    const warnings: string[] = [];
    if (estimate && creditCap) {
        const said = capVersus(creditCap, estimate.credits, creditsText);
        if (said) warnings.push(said);
    }
    if (estimate && usdCap) {
        const said = capVersus(usdCap, modelSpan(estimate), (n) => `${aboutUsd(n)} of models`);
        if (said) warnings.push(said);
        if (estimate.models.unpriced.length) {
            warnings.push(`The cap can’t count ${estimate.models.unpriced.join(', ')}: no price is known for it.`);
        }
    }

    return (
        <fieldset disabled={readOnly} className="space-y-2.5" aria-label="Cap per run">
            <div className="grid grid-cols-2 gap-3">
                {units.map((unit) => (
                    <Field
                        key={unit}
                        label={unit === 'credits' ? 'Cap in credits' : 'Cap in model dollars'}
                        error={capError(unit, texts[unit].text) || undefined}
                    >
                        <Input
                            id={unit === 'credits' ? 'orch-cap-credits' : 'orch-cap-usd'}
                            type="number"
                            size="sm"
                            min={unit === 'credits' ? 1 : 0}
                            step="any"
                            inputMode="decimal"
                            placeholder="No cap"
                            value={texts[unit].text}
                            onChange={(e) => edit(unit, e.target.value)}
                        />
                    </Field>
                ))}
            </div>
            {(creditCap || usdCap) ? (
                <p className="m-0 text-xs leading-relaxed text-text-muted">
                    A run stops before the step that would take it past its cap. A step already working finishes.
                </p>
            ) : (
                <p className="m-0 text-xs leading-relaxed text-text-faint">
                    Empty is no cap. A run that reaches its cap stops before its next step and says so.
                </p>
            )}
            {warnings.map((warning) => (
                <p key={warning} className="m-0 flex items-start gap-1.5 text-xs leading-relaxed text-text">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
                    <span>{warning}</span>
                </p>
            ))}
        </fieldset>
    );
}

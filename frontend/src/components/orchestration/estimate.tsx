'use client';
/**
 * What a run will cost, before it starts (CE1, D261).
 *
 * The product's estimate route prices a definition: OSS the model half from the
 * rate card, cloud credits too and the model wallet's side. Every figure is a
 * range from the cheapest path to the dearest, worded "about", and the sides
 * that pay are never added together (D87): what the wallet will be charged is
 * not the rate card's guess at someone else's bill. What the definition cannot
 * say (a for-each loop's length, a route back, a nested workflow) reads as
 * "or more", and a model nothing prices is named rather than counted as free.
 *
 * Synced into the cloud app; nothing here calls an API. Each product passes its
 * own `load` to `useRunEstimate`.
 */
import { useEffect, useState } from 'react';

import { creditsText, money } from '@/components/run/model';
import { cn } from '@/lib/cn';

export type CostSpan = { low: number; high: number; open?: string[] };

/** The estimate route's answer, in both products (cloud adds credits and the wallet). */
export type RunEstimate = {
    /** Cloud: the steps' credits, the path range widened by finished runs. */
    credits?: CostSpan | null;
    models: {
        /** By who pays: `wallet` (cloud's platform models), `own_keys`, `local`. */
        sides: Record<string, CostSpan>;
        /** Models nothing prices: their calls are in no figure. */
        unpriced: string[];
        models: string[];
    };
    /** What the definition cannot say: `per_item`, `repeat`, `nested`. */
    open: string[];
    /** The model figure from the last finished runs since the last edit, which replaces the prior. */
    runs: (CostSpan & { count: number }) | null;
    basis: 'steps' | 'runs';
    /** Cloud: what the model wallet holds now, in USD. */
    wallet_usd?: number | null;
    /** A step whose time is its own (an agent's turns, a tool's call): where minutes are charged too, it can cost more. */
    slow_steps?: boolean;
};

export type EstimateLoader = (
    body: { definition: unknown; orchestration_id?: string | null },
    signal: AbortSignal,
) => Promise<RunEstimate>;

const SIDE_WORDS: Record<string, string> = {
    wallet: 'from your model wallet',
    own_keys: 'on your own keys',
};

/** A dollar figure said roughly: "$0.13", "$0.002", "$1.40". */
export function aboutUsd(usd: number): string {
    if (usd <= 0) return '$0';
    return usd >= 0.01 ? `$${usd.toFixed(2)}` : `$${usd.toPrecision(1)}`;
}

function range(span: CostSpan, say: (n: number) => string, most: boolean): string {
    const lo = say(span.low);
    const hi = say(span.high);
    if (most || lo === hi) return hi;
    // "2.1–3.1 credits", "$0.02–$0.13": the unit once.
    const unit = /credits?$/.exec(hi)?.[0];
    return unit ? `${lo.replace(/ credits?$/, '')}–${hi}` : `${lo}–${hi}`;
}

/**
 * The estimate as words: a sentence ("Each run costs about 2.1–3.1 credits and
 * $0.02–$0.13 from your model wallet.") and the lines under it.
 *
 * `ceiling` is for a plan (the builder's card), which lists every step once and
 * no branch: its one figure is a most, said "up to about".
 */
export function estimateWords(estimate: RunEstimate, opts: { ceiling?: boolean } = {}): { headline: string; notes: string[] } {
    const ceiling = Boolean(opts.ceiling);
    const parts: string[] = [];
    if (estimate.credits) parts.push(range(estimate.credits, creditsText, ceiling));
    const paying = Object.keys(estimate.models.sides).filter((side) => side !== 'local');
    // A run's model figure is one number (F108), so its side is named only when one side pays.
    const modelSides: [string, CostSpan][] = estimate.basis === 'runs' && estimate.runs
        ? [[paying.length === 1 ? paying[0] : 'models', estimate.runs]]
        : paying.map((side) => [side, estimate.models.sides[side]]);
    for (const [side, span] of modelSides) {
        if (span.high <= 0 && span.low <= 0) continue;
        parts.push(`${range(span, aboutUsd, ceiling)} ${SIDE_WORDS[side] ?? 'of models'}`);
    }
    const local = 'local' in estimate.models.sides;
    // A most is only a most when nothing is open: a plan with a loop is "about …, or more".
    const most = ceiling && !estimate.open.length;
    const list = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
    let headline = '';
    if (list) {
        headline = (most ? 'A run of this plan costs up to about ' : ceiling ? 'A run of this plan costs about ' : 'Each run costs about ') + list;
        headline += estimate.open.length ? ', or more.' : '.';
    } else if (local) {
        headline = 'Its models run on this machine: no model charges.';
    }

    const notes: string[] = [];
    if (estimate.open.includes('per_item')) notes.push('A loop over a list costs this for each item.');
    if (estimate.open.includes('repeat')) notes.push('A step that sends the run back costs more each time round.');
    if (estimate.open.includes('nested')) notes.push('The workflows it runs cost extra.');
    // Credits are the greater of a run's steps and its minutes: a slow agent can pass its steps (Naveen, CE1).
    if (estimate.credits && estimate.slow_steps) notes.push('A step that works for minutes is charged a credit a minute.');
    if (estimate.models.unpriced.length) {
        notes.push(`No price is known for ${estimate.models.unpriced.join(', ')}: its calls are not in the figure.`);
    }
    if (estimate.basis === 'runs' && estimate.runs) {
        notes.push(`Models from its last ${estimate.runs.count === 1 ? 'run' : `${estimate.runs.count} runs`}.`);
    } else if (paying.length) {
        notes.push('Models from typical steps until it has run.');
    }
    return { headline, notes };
}

/** True when the model wallet cannot cover even the cheapest run (B4: it runs out first). */
export function walletShort(estimate: RunEstimate): boolean {
    const wallet = estimate.basis === 'runs' && estimate.runs && 'wallet' in estimate.models.sides
        ? estimate.runs : estimate.models.sides.wallet;
    return Boolean(wallet && estimate.wallet_usd != null && estimate.wallet_usd < wallet.low);
}

/** A workflow's cap per run, as it sets it (CAP1): the two fields, either unset. */
export type RunCapSetting = { max_run_credits?: number | null; max_run_usd?: number | null };

/** What a product can cap: cloud both, self-hosted dollars only (it has no credits). */
export type CapUnit = 'credits' | 'usd';

/** "Capped at 5 credits a run.", "Capped at 5 credits and $0.50 of models a run.", or '' with no cap. */
export function capText(cap: RunCapSetting | null | undefined, units: readonly CapUnit[] = ['credits', 'usd']): string {
    const parts: string[] = [];
    if (units.includes('credits') && cap?.max_run_credits) parts.push(creditsText(cap.max_run_credits));
    if (units.includes('usd') && cap?.max_run_usd) parts.push(`${money(cap.max_run_usd)} of models`);
    return parts.length ? `Capped at ${parts.join(' and ')} a run.` : '';
}

/**
 * One estimate, as a line or a short block. `compact` is the single line for a
 * dialog's footer; `topUp` is the product's way to the model wallet, shown when
 * the wallet holds less than the cheapest run. `cap`, the workflow's cap per run
 * (CAP1), is said after the figure, in the units `capUnits` this product caps.
 */
export function EstimateLine({ estimate, busy, error, compact, ceiling, topUp, cap, capUnits, className }: {
    estimate: RunEstimate | null;
    busy?: boolean;
    error?: string;
    compact?: boolean;
    ceiling?: boolean;
    topUp?: React.ReactNode;
    cap?: RunCapSetting | null;
    capUnits?: readonly CapUnit[];
    className?: string;
}) {
    if (error) return <p className={cn('text-xs text-text-muted', className)}>No estimate: {error}</p>;
    if (!estimate) {
        return <p className={cn('text-xs text-text-faint', className)} aria-live="polite">{busy ? 'Pricing…' : ' '}</p>;
    }
    const { headline, notes } = estimateWords(estimate, { ceiling });
    if (!headline) return null;
    const short = walletShort(estimate);
    const capped = capText(cap, capUnits);
    return (
        <div className={cn('text-xs leading-relaxed text-text-muted', className)} aria-live="polite">
            <p className="text-text">{headline}{capped ? ` ${capped}` : ''}</p>
            {!compact && notes.map((note) => <p key={note}>{note}</p>)}
            {short && (
                <p className="text-text">
                    Your model wallet holds {aboutUsd(estimate.wallet_usd ?? 0)}.{topUp ? <> {topUp}</> : null}
                </p>
            )}
        </div>
    );
}

/** What a price never reads: moving a step on the canvas, or changing the cap, does not price it again. */
const UNPRICED_KEYS = new Set(['position_x', 'position_y', 'max_run_credits', 'max_run_usd']);

/** The priced parts of a definition. */
function pricedKey(definition: unknown): string {
    return JSON.stringify(definition, (key, value) => (UNPRICED_KEYS.has(key) ? undefined : value));
}

/** An estimate that follows the definition as it is edited (300 ms after the last change). */
export function useRunEstimate(load: EstimateLoader | undefined, definition: unknown, workflowId?: string | null) {
    const [estimate, setEstimate] = useState<RunEstimate | null>(null);
    const [error, setError] = useState('');
    // The key the shown estimate (or error) answers: pricing until it is the current one.
    const [settled, setSettled] = useState('');
    const key = definition ? pricedKey(definition) : '';
    const asked = `${workflowId || ''}|${key}`;
    useEffect(() => {
        if (!load || !key) return;
        const ctrl = new AbortController();
        const timer = setTimeout(() => {
            load({ definition: JSON.parse(key), orchestration_id: workflowId || null }, ctrl.signal)
                .then((e) => { setEstimate(e); setError(''); setSettled(asked); })
                .catch((e) => {
                    if (ctrl.signal.aborted) return;
                    setEstimate(null);
                    setError(e instanceof Error ? e.message : String(e));
                    setSettled(asked);
                });
        }, 300);
        return () => { clearTimeout(timer); ctrl.abort(); };
    }, [load, key, workflowId, asked]);
    return { estimate, error, busy: Boolean(load && key) && settled !== asked };
}

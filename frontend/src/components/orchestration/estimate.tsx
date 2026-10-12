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

import { creditsText } from '@/components/run/model';
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
 * The estimate as words: a headline and the lines under it.
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
    if (!parts.length && local) parts.push('no model charges: its models run on this machine');
    // A most is only a most when nothing is open: a plan with a loop is "about …, or more".
    const most = ceiling && !estimate.open.length;
    let headline = parts.length ? (most ? 'Up to about ' : 'About ') + parts.join(' · ') : '';
    if (headline && estimate.open.length) headline += ', or more';
    if (headline) headline += ' a run';

    const notes: string[] = [];
    if (estimate.open.includes('per_item')) notes.push('A loop over a list costs this for each item.');
    if (estimate.open.includes('repeat')) notes.push('A step that sends the run back costs more each time round.');
    if (estimate.open.includes('nested')) notes.push('The workflows it runs cost extra.');
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

/**
 * One estimate, as a line or a short block. `compact` is the single line for a
 * dialog's footer; `topUp` is the product's way to the model wallet, shown when
 * the wallet holds less than the cheapest run.
 */
export function EstimateLine({ estimate, busy, error, compact, ceiling, topUp, className }: {
    estimate: RunEstimate | null;
    busy?: boolean;
    error?: string;
    compact?: boolean;
    ceiling?: boolean;
    topUp?: React.ReactNode;
    className?: string;
}) {
    if (error) return <p className={cn('text-xs text-text-muted', className)}>No estimate: {error}</p>;
    if (!estimate) {
        return <p className={cn('text-xs text-text-faint', className)} aria-live="polite">{busy ? 'Pricing…' : ' '}</p>;
    }
    const { headline, notes } = estimateWords(estimate, { ceiling });
    if (!headline) return null;
    const short = walletShort(estimate);
    return (
        <div className={cn('text-xs leading-relaxed text-text-muted', className)} aria-live="polite">
            <p><span className="text-text">{headline}</span>{compact && notes.length ? '.' : ''}</p>
            {!compact && notes.map((note) => <p key={note}>{note}</p>)}
            {short && (
                <p className="text-text">
                    Your model wallet holds {aboutUsd(estimate.wallet_usd ?? 0)}.{topUp ? <> {topUp}</> : null}
                </p>
            )}
        </div>
    );
}

/** The priced parts of a definition: moving a step on the canvas does not price it again. */
function pricedKey(definition: unknown): string {
    return JSON.stringify(definition, (key, value) => (key === 'position_x' || key === 'position_y' ? undefined : value));
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

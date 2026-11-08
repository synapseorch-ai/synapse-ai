/*
 * A run's path at a glance (UX1, D211): one bar per step of its workflow, in
 * the order it reads, coloured by who does the work — cyan a model, grey logic,
 * amber a person. The step a failed run stopped at is red; the step a waiting
 * run waits at is ringed. Colour is never the only signal: the label says it.
 */
import { cn } from '@/lib/cn';

import { GLYPH_MAX, type GlyphBar, type StepRole } from './model';

const FILL: Record<StepRole, string> = {
    model: 'bg-machine',
    logic: 'bg-border-strong',
    person: 'bg-person',
};

export function PathGlyph({ bars, done, className, max = GLYPH_MAX }: {
    bars: GlyphBar[];
    /** Steps recorded so far, when the product reports it; said, not drawn. */
    done?: number | null;
    className?: string;
    /** The most bars drawn; past it, one fewer and the count of the rest (a fixed column's width, F482). */
    max?: number;
}) {
    if (!bars.length) return <span className={cn('text-xs text-text-faint', className)}>—</span>;
    const shown = bars.length > max ? bars.slice(0, max - 1) : bars;
    const more = bars.length - shown.length;
    const count = (r: StepRole) => bars.filter((b) => b.role === r).length;
    const failed = bars.find((b) => b.mark === 'failed');
    const parts = [
        `${bars.length} ${bars.length === 1 ? 'step' : 'steps'}`,
        count('model') && `${count('model')} calling a model`,
        count('person') && `${count('person')} for a person`,
        count('logic') && `${count('logic')} logic`,
    ].filter(Boolean);
    const label = `${parts.join(', ')}${typeof done === 'number' ? `; ${done} recorded` : ''}${failed ? '; stopped at a failed step' : ''}`;
    return (
        <span role="img" aria-label={label} title={label} className={cn('inline-flex items-center gap-[3px]', className)}>
            {shown.map((bar) => (
                <i
                    key={bar.id}
                    className={cn(
                        'block h-[5px] w-3.5 rounded-[3px]',
                        bar.mark === 'failed' ? 'bg-danger' : FILL[bar.role],
                        bar.mark === 'waiting' && 'outline outline-1 outline-offset-1 outline-person-ink',
                    )}
                />
            ))}
            {more > 0 && <span className="ml-0.5 text-2xs tabular-nums text-text-faint">+{more}</span>}
        </span>
    );
}

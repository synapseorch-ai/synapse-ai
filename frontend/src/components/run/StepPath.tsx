/*
 * A workflow's path before it has ever run (M2f, D211): its steps in reading
 * order as small icon tiles joined by a hairline, coloured by who does the
 * work — cyan a model, grey logic, a filled amber tile for a person. Template
 * cards show it instead of a wall of text. A run's path is `PathGlyph`.
 */
import { cn } from '@/lib/cn';

import { pathLabel, roleOf, type StepRole } from './model';
import { StepIcon } from './StepIcon';

/** The most tiles a path draws; the rest are counted. */
export const STEP_PATH_MAX = 10;

export { pathLabel, templatePath } from './model';

export function StepPath({ types, roles, max = STEP_PATH_MAX, variant = 'boxed', className }: {
    types: string[];
    /** Each step's role from its config (`templatePath`, F1657); without them, each type's. */
    roles?: StepRole[];
    max?: number;
    /**
     * `boxed` (a template card): the path on its own grey ground, tiles on rings.
     * `inline` (a row of the Workflows list, M2k): no ground; solid tinted tiles
     * with no ring, which broke up at a fractional display scale (Naveen).
     */
    variant?: 'boxed' | 'inline';
    className?: string;
}) {
    const inline = variant === 'inline';
    if (!types.length) return null;
    const shown = types.length > max ? types.slice(0, max - 1) : types;
    const more = types.length - shown.length;
    const label = pathLabel(types, roles);
    return (
        <div
            role="img"
            aria-label={label}
            title={label}
            data-step-path=""
            className={cn(
                'flex items-center overflow-hidden',
                inline ? 'w-auto shrink-0' : 'rounded-lg bg-surface-2 p-3',
                className,
            )}
        >
            {shown.map((type, i) => {
                const role = roles?.[i] ?? roleOf(type);
                return (
                <span key={i} className="contents">
                    {i > 0 && <span aria-hidden className="h-px min-w-1.5 flex-1 bg-border-strong" />}
                    <StepIcon
                        type={type}
                        className={cn(
                            'size-[22px] rounded-md [&>svg]:size-3',
                            inline ? 'bg-surface-2' : 'bg-bg shadow-[0_0_0_1px_var(--border)]',
                            role === 'model' && 'text-machine',
                            role === 'logic' && 'text-text-muted',
                            role === 'person' && 'bg-person text-person-fg shadow-none',
                        )}
                        role={role}
                    />
                </span>
                );
            })}
            {more > 0 && (
                <>
                    <span aria-hidden className="h-px min-w-1.5 flex-1 bg-border-strong" />
                    <span aria-hidden className="shrink-0 pl-1 text-2xs font-medium tabular-nums text-text-faint">+{more}</span>
                </>
            )}
        </div>
    );
}

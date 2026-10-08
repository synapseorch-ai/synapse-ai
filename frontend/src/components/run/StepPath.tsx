/*
 * A workflow's path before it has ever run (M2f, D211): its steps in reading
 * order as small icon tiles joined by a hairline, coloured by who does the
 * work — cyan a model, grey logic, a filled amber tile for a person. Template
 * cards show it instead of a wall of text. A run's path is `PathGlyph`.
 */
import { cn } from '@/lib/cn';

import { pathOrder, roleOf, type StepRole } from './model';
import { StepIcon } from './StepIcon';

/** The most tiles a path draws; the rest are counted. */
export const STEP_PATH_MAX = 10;

type PathStep = { id: string; type?: string | null };
type PathWorkflow = { steps: PathStep[]; entry_step_id?: string | null };

/**
 * A workflow's step types in reading order, without its End steps, and whether
 * a person takes part (a Human step anywhere, a container's body included).
 */
export function templatePath(workflow: PathWorkflow | null | undefined): { types: string[]; hasPerson: boolean } {
    if (!workflow?.steps?.length) return { types: [], hasPerson: false };
    const byId = new Map(workflow.steps.map((s) => [s.id, s]));
    // pathOrder reads only ids, links and entry_step_id; the cast widens the step shape.
    const order = pathOrder(workflow as Parameters<typeof pathOrder>[0]);
    const types = order.map((id) => byId.get(id)?.type ?? '').filter((t) => t && t !== 'end');
    return { types, hasPerson: types.some((t) => roleOf(t) === 'person') };
}

/** "8 steps, 5 calling a model, 1 for a person, 2 logic". */
export function pathLabel(types: string[]): string {
    const count = (r: StepRole) => types.filter((t) => roleOf(t) === r).length;
    return [
        `${types.length} ${types.length === 1 ? 'step' : 'steps'}`,
        count('model') && `${count('model')} calling a model`,
        count('person') && `${count('person')} for a person`,
        count('logic') && `${count('logic')} logic`,
    ].filter(Boolean).join(', ');
}

export function StepPath({ types, max = STEP_PATH_MAX, className }: {
    types: string[];
    max?: number;
    className?: string;
}) {
    if (!types.length) return null;
    const shown = types.length > max ? types.slice(0, max - 1) : types;
    const more = types.length - shown.length;
    const label = pathLabel(types);
    return (
        <div
            role="img"
            aria-label={label}
            title={label}
            data-step-path=""
            className={cn('flex items-center overflow-hidden rounded-lg bg-surface-2 p-3', className)}
        >
            {shown.map((type, i) => (
                <span key={i} className="contents">
                    {i > 0 && <span aria-hidden className="h-px min-w-1.5 flex-1 bg-border-strong" />}
                    <StepIcon
                        type={type}
                        className={cn(
                            'size-[22px] rounded-md bg-bg shadow-[0_0_0_1px_var(--border)] [&>svg]:size-3',
                            roleOf(type) === 'model' && 'text-machine',
                            roleOf(type) === 'logic' && 'text-text-muted',
                            roleOf(type) === 'person' && 'bg-person text-person-fg shadow-none',
                        )}
                    />
                </span>
            ))}
            {more > 0 && (
                <>
                    <span aria-hidden className="h-px min-w-1.5 flex-1 bg-border-strong" />
                    <span aria-hidden className="shrink-0 pl-1 text-2xs font-medium tabular-nums text-text-faint">+{more}</span>
                </>
            )}
        </div>
    );
}

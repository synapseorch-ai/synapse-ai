/*
 * What a change does, field by field (F1262; TRI1's one shape, 10-30).
 *
 * An act's card, a change's review and a setting's card all show a change the
 * same way: each field as it stands and as it will be. `before` empty is
 * something new; `after` empty is something that goes. The rows come from the
 * server (`core/native_builder/rows.py`) as display text: this only lays them
 * out. A long value keeps its own lines, and its changed words are already
 * marked with brackets.
 */
import { ArrowRight } from 'lucide-react';

import { cn } from '@/lib/cn';
import type { ChangeRow } from '@/lib/builderEvents';

export function ChangeRows({ rows, className }: { rows: ChangeRow[]; className?: string }) {
    if (!rows.length) return null;
    return (
        <dl className={cn('grid gap-x-4 gap-y-2 text-[13px] sm:grid-cols-[minmax(7rem,auto)_1fr]', className)}>
            {rows.map((row, i) => (
                <div key={`${row.label}-${i}`} className="contents">
                    <dt className="text-xs font-medium text-text-faint sm:pt-px">{row.label}</dt>
                    <dd className="-mt-1.5 min-w-0 sm:mt-0">
                        <span className="flex flex-wrap items-baseline gap-x-1.5 [overflow-wrap:anywhere]">
                            {row.before != null && (
                                <span className={cn('whitespace-pre-line text-text-muted', row.after == null && 'line-through')}>
                                    {row.before}
                                </span>
                            )}
                            {row.before != null && row.after != null && (
                                <ArrowRight className="size-3 shrink-0 self-center text-text-faint" aria-label="becomes" />
                            )}
                            {row.after != null ? (
                                <span className="whitespace-pre-line font-medium text-text">{row.after}</span>
                            ) : (
                                <span className="text-text-faint">(none)</span>
                            )}
                        </span>
                        {row.note && <span className="mt-0.5 block text-xs text-text-faint">{row.note}</span>}
                    </dd>
                </div>
            ))}
        </dl>
    );
}

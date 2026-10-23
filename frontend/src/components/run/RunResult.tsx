'use client';

/*
 * A finished run's answer, first on its page (F901). Naveen: "sometimes under
 * runs it's hard to find the final answer on completed run". It was one row of
 * "What it produced", below the whole path, as a preview, and only when the
 * workflow named a result key. Now it is the card under the run's facts: the
 * value whole, by the engine's own rule (`resultOf`, `results.py`), with Copy.
 * The path and every output stay below it.
 */
import { CopyButton } from '@/components/ui';
import type { Orchestration } from '@/types/orchestration';

import { resultOf, type RunRecord } from './model';
import { OutputValue } from './StepDetail';

export function RunResult({ run, workflow }: { run: RunRecord; workflow: Orchestration | null | undefined }) {
    const result = resultOf(run, workflow);
    if (!result) return null;
    const { value, from } = result;
    if (value === null || value === undefined || (typeof value === 'string' && !value.trim())) return null;
    const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    return (
        <section aria-labelledby="run-result" className="grid gap-2 rounded-xl border border-border bg-surface px-5 py-4">
            <div className="flex items-center gap-3">
                <h2 id="run-result" className="flex-1 text-[15px] font-semibold text-text">The result</h2>
                <CopyButton text={text} variant="ghost" />
            </div>
            {from && <p className="-mt-1.5 text-xs text-text-faint">From {from}</p>}
            <div className="max-h-[32rem] min-w-0 overflow-y-auto text-[15px] leading-relaxed text-text">
                <OutputValue value={value} />
            </div>
        </section>
    );
}

'use client';

/*
 * One step's detail, opened from its row on the run page: what it wrote, why it
 * failed, and what it spent (M2b; cloud's `StepHistory` rows, moved upstream).
 *
 * A step's output is not on its history entry. The engine records the step and
 * the `output_key` it wrote; the value is in `shared_state` under that key. So
 * the row and the Shared state section are two views of one document.
 *
 * A string is markdown (plain text renders unchanged); anything else is
 * pretty-printed JSON. A step's error is shown whole: the card above leads with
 * the plain sentence, and this is where the step's own words are read.
 */
import { Markdown } from '@/components/ui';

import { money, type StepExecution, type StepUsage } from './model';

export function OutputValue({ value }: { value: unknown }) {
    if (typeof value === 'string') {
        return value.trim()
            ? <Markdown>{value}</Markdown>
            : <p className="text-sm italic text-text-faint">empty string</p>;
    }
    if (typeof value === 'number' || typeof value === 'boolean' || value === null) {
        return <p className="font-code text-sm text-text-muted">{String(value)}</p>;
    }
    return (
        <pre className="scroll-x overflow-x-auto rounded-md border border-border bg-bg px-4 py-3 font-code text-xs leading-relaxed text-text-muted">
            {JSON.stringify(value, null, 2)}
        </pre>
    );
}

/** What this step wrote, looked up in the run's shared state. */
export function outputOf(step: StepExecution, state: Record<string, unknown>): unknown {
    return step.output_key ? state[step.output_key] : undefined;
}

/** Nothing to open for a step that produced neither output nor an error, nor spent anything. */
export function hasDetail(step: StepExecution, state: Record<string, unknown>, usage?: StepUsage): boolean {
    if (step.error || usage) return true;
    const output = outputOf(step, state);
    if (output === null || output === undefined) return false;
    if (typeof output === 'string') return output.trim().length > 0;
    if (Array.isArray(output)) return output.length > 0;
    if (typeof output === 'object') return Object.keys(output).length > 0;
    return true;
}

/** The models, turns, tokens, context and tools a step used (cloud's `step_usage`). */
export function UsageLine({ usage }: { usage: StepUsage }) {
    return (
        <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs text-text-faint">
            {usage.models.map((model) => (
                <span key={model} className="font-code" translate="no">{model}</span>
            ))}
            <span className="tabular-nums">{usage.turns} {usage.turns === 1 ? 'turn' : 'turns'}</span>
            <span className="tabular-nums">{usage.tokens.toLocaleString()} tokens</span>
            <span className="tabular-nums" title="The largest prompt this step sent">
                {Math.round(usage.context_peak_chars / 1000).toLocaleString()}k context
            </span>
            {usage.cost_usd !== null && <span className="tabular-nums">{money(usage.cost_usd)}</span>}
            {usage.tools.map((tool) => (
                <span key={tool} className="rounded bg-surface-2 px-1.5 py-0.5 font-code text-text-muted" translate="no">{tool}</span>
            ))}
        </span>
    );
}

export function StepDetail({ step, sharedState, usage, id }: {
    step: StepExecution;
    sharedState: Record<string, unknown>;
    usage?: StepUsage;
    id?: string;
}) {
    const output = outputOf(step, sharedState);
    return (
        <div id={id} className="grid gap-3 rounded-md bg-surface-2 px-4 py-3">
            {usage && <UsageLine usage={usage} />}
            {step.error && (
                <pre className="whitespace-pre-wrap break-words font-code text-xs leading-relaxed text-danger">{step.error}</pre>
            )}
            {output !== undefined && output !== null ? (
                <div>
                    {step.output_key && (
                        <p className="mb-1.5 font-code text-2xs uppercase tracking-wider text-text-faint">{step.output_key}</p>
                    )}
                    <OutputValue value={output} />
                </div>
            ) : !step.error && (
                <p className="text-sm text-text-muted">This step recorded no output.</p>
            )}
        </div>
    );
}

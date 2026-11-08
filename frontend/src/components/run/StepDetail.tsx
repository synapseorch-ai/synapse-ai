'use client';

/*
 * One step's detail, opened from its row on the run page: what it wrote, why it
 * failed, and what it spent (M2b; cloud's `StepHistory` rows, moved upstream).
 *
 * A step's output is not on its history entry. The engine records the step and
 * the `output_key` it wrote; the value is in `shared_state` under that key. So
 * the row and the Shared state section are two views of one document.
 *
 * A string is markdown (plain text renders unchanged); a step output saved to
 * the Vault is its note and a link to the file (D339 P4); anything else is
 * pretty-printed JSON. A step's error is shown whole: the card above leads with
 * the plain sentence, and this is where the step's own words are read.
 */
import Link from 'next/link';

import { Markdown } from '@/components/ui';

import { useRunHost } from './host';
import { isSavedFile, money, outputOf, REMOVED_NOTE, type SavedFile, type StepExecution, type StepUsage } from './model';

/** A step's output saved to the Vault (D339 P4): the note it finished with, and the file, which opens in the Vault. */
function SavedFileValue({ file }: { file: SavedFile }) {
    const { links } = useRunHost();
    const href = links.vault ? links.vault(file.$vault) : `/vault?path=${encodeURIComponent(file.$vault)}`;
    return (
        <div className="grid gap-2">
            {file.text?.trim() && <Markdown>{file.text}</Markdown>}
            <p className="text-sm text-text-muted">
                Saved to the Vault:{' '}
                <Link href={href} className="font-code text-xs text-accent underline-offset-2 hover:underline" translate="no">
                    {file.$vault}
                </Link>
                {typeof file.chars === 'number' && (
                    <span className="tabular-nums text-text-faint"> · {file.chars.toLocaleString()} characters</span>
                )}
            </p>
        </div>
    );
}

export function OutputValue({ value }: { value: unknown }) {
    if (isSavedFile(value)) return <SavedFileValue file={value} />;
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

// In `model.ts` since F902 (the canvas reads them too, with no React); said here for the run screens.
export { hasDetail, outputOf } from './model';

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

export function StepDetail({ step, sharedState, usage, id, removed }: {
    step: StepExecution;
    sharedState: Record<string, unknown>;
    usage?: StepUsage;
    id?: string;
    /** Retention removed the run's step data (F428): its output is gone, not unrecorded. */
    removed?: boolean;
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
                <p className="text-sm text-text-muted">{removed ? REMOVED_NOTE : 'This step recorded no output.'}</p>
            )}
        </div>
    );
}

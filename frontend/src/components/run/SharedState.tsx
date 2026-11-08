'use client';

/*
 * Shared state, one key at a time (cloud's run page, moved upstream in M2b).
 *
 * Shared state is where a step's output ends up, so the interesting values are
 * paragraphs of model prose — and inside a JSON dump a paragraph arrives as one
 * long line with `\n` escapes, unreadable exactly when someone is working out
 * what an agent said. Each key gets its name and its value rendered for what it
 * is: a string as markdown, anything else as pretty-printed JSON.
 */
import { OutputValue } from './StepDetail';

export function SharedState({ state }: { state: Record<string, unknown> }) {
    return (
        <dl className="space-y-4">
            {Object.entries(state).map(([key, value]) => (
                <div key={key}>
                    <dt className="mb-1.5 font-code text-2xs uppercase tracking-wider text-text-faint">{key}</dt>
                    <dd><OutputValue value={value} /></dd>
                </div>
            ))}
        </dl>
    );
}

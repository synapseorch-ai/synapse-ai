'use client';

/*
 * A run waiting on a person, answered in place (UX1, M2b).
 *
 * The question, what it is about, and the answer — the step's own fields when
 * it declared any, else free text — on the run page itself, above the steps.
 * Every control of the two pages it replaces is here: cloud's card ("Your
 * answer", the step's fields, Send answer, Cancel run) and OSS's modal (the
 * fields or a text answer, and the context the question is about).
 *
 * The answer is a mapping of the fields keyed by name when the step declared
 * any, a bare string when it did not — what the engine's `resume()` takes
 * (F75). An empty answer can be sent, as on cloud's card before it. A refusal
 * (someone answered first) is said in the card.
 */
import { useState } from 'react';

import { Badge, Button, ErrorNote, Field, Markdown, Textarea } from '@/components/ui';

import { HumanFieldsForm } from '../orchestration/HumanFieldsForm';
import { humanAnswer, initialHumanValues, type HumanValues } from '../orchestration/humanFields';
import { useRunHost } from './host';
import type { RunRecord } from './model';
import { Took } from './When';

export function ApprovalCard({ run, onDone, onCancel, busy }: {
    run: RunRecord;
    /** After an answer was taken: the message to show. */
    onDone: (message: string) => void;
    onCancel: () => void;
    busy: boolean;
}) {
    const host = useRunHost();
    const fields = Array.isArray(run.humanFields) ? run.humanFields : [];
    const [values, setValues] = useState<HumanValues>(() => initialHumanValues(fields));
    const [text, setText] = useState('');
    const [sending, setSending] = useState(false);
    const [error, setError] = useState('');
    const asking = [...run.history].reverse().find((h) => h.status === 'paused');
    const stepName = asking?.step_name || run.currentStepId;

    async function send() {
        setError('');
        setSending(true);
        try {
            const response = fields.length ? humanAnswer(fields, values) : text;
            onDone(await host.answer(run.id, response));
        } catch (e) {
            setError(e instanceof Error ? e.message : 'The answer was not taken.');
        } finally {
            setSending(false);
        }
    }

    return (
        <section aria-label="Waiting for a person" className="grid gap-3 rounded-[14px] border border-person/40 bg-person-subtle px-[22px] py-5">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-text-muted">
                <Badge tone="person">Waiting for a person</Badge>
                {stepName && <span title={run.currentStepId ?? undefined}>Step {stepName}</span>}
                {asking?.started_at && <span>· waiting <Took start={asking.started_at} end={null} /></span>}
            </div>
            {run.humanContext && (
                <figure className="m-0">
                    <figcaption className="mb-1.5 text-xs text-text-muted">From this run</figcaption>
                    <div className="max-h-64 overflow-y-auto rounded-md border border-border bg-surface p-4">
                        <Markdown>{run.humanContext}</Markdown>
                    </div>
                </figure>
            )}
            <div className="max-w-[62ch] text-[17px] leading-normal tracking-[-0.012em] text-text">
                <Markdown>{run.humanPrompt ?? ''}</Markdown>
            </div>
            <div className="grid max-w-xl gap-3">
                {fields.length ? (
                    <HumanFieldsForm fields={fields} values={values} onChange={setValues} idPrefix={`run-${run.id}-field`} />
                ) : (
                    <Field label="Your answer">
                        <Textarea
                            rows={3}
                            value={text}
                            onChange={(e) => setText(e.target.value)}
                            placeholder="Type the answer this step is waiting for"
                        />
                    </Field>
                )}
            </div>
            {error && <ErrorNote>{error}</ErrorNote>}
            <div className="flex flex-wrap items-center gap-2">
                <Button variant="person" onClick={send} loading={sending} disabled={busy || sending}>
                    Send answer
                </Button>
                <Button
                    variant="ghost"
                    className="text-danger hover:bg-danger-subtle hover:text-danger"
                    disabled={busy || sending}
                    onClick={onCancel}
                >
                    Cancel run
                </Button>
            </div>
        </section>
    );
}

'use client';

/*
 * A run waiting on a person, answered in place (UX1, M2b; Home in M2a).
 *
 * The question, what it is about, and the answer — the step's own fields when
 * it declared any, else free text — on the run page itself, above the steps,
 * and on Home under "Needs you". Every control of the two pages it replaces is
 * here: cloud's card ("Your answer", the step's fields, Send answer, Cancel
 * run) and OSS's modal (the fields or a text answer, and the context the
 * question is about).
 *
 * A step whose fields hold exactly one Yes / No is an approval: its flag is two
 * buttons, the field's own label and Decline, and pressing one sends the
 * answer with the other fields as they stand (Naveen, M2a). The answer is the
 * same mapping the form sends — `{approved: true, comment: "…"}` — so a later
 * step reads it as before. Any other shape keeps the form and Send answer.
 *
 * The answer is a mapping of the fields keyed by name when the step declared
 * any, a bare string when it did not — what the engine's `resume()` takes
 * (F75). An empty answer can be sent, as on cloud's card before it. A refusal
 * (someone answered first) is said in the card.
 *
 * The workflow editor answers in its setup card (M3a-2) with its own rules:
 * `requireAnswer` (an empty free-text answer is not sent, by button or by key —
 * Naveen's item 7), `submitOnEnter` (Enter sends, Shift+Enter is a new line, the
 * box grows with its lines) and `draft` (the editor keeps what was typed, so an
 * answer left half-written is still there when the card opens again).
 */
import Link from 'next/link';
import { useState } from 'react';
import { Check, ChevronRight, X } from 'lucide-react';

import { Badge, Button, ErrorNote, Field, Markdown, Textarea } from '@/components/ui';

import { HumanFieldsForm } from '../orchestration/HumanFieldsForm';
import { humanAnswer, humanFieldLabel, initialHumanValues, type HumanValues } from '../orchestration/humanFields';
import { useRunHost } from './host';
import { decisionField, type RunRecord } from './model';
import { Took } from './When';

export type ApprovalDraft = {
    text: string;
    onText: (text: string) => void;
    values: HumanValues;
    onValues: (values: HumanValues) => void;
};

export function ApprovalCard({
    run, onDone, onCancel, busy, openHref, showWorkflow = false, showStep = true, requireAnswer = false,
    submitOnEnter = false, draft,
}: {
    run: RunRecord;
    /** After an answer was taken: the message to show. */
    onDone: (message: string) => void;
    onCancel: () => void;
    busy: boolean;
    /** Where Open run goes, on a page that is not the run's own (Home). */
    openHref?: string;
    /** Name the workflow in the card's top line, where the page does not (Home). */
    showWorkflow?: boolean;
    /** Name the step in the card's top line; off where the step's own header already does (the setup card). */
    showStep?: boolean;
    /** An empty free-text answer cannot be sent (the workflow editor's rule). */
    requireAnswer?: boolean;
    /** Enter sends the free-text answer; Shift+Enter is a new line. */
    submitOnEnter?: boolean;
    /** The answer as typed, held by the page, so it outlives the card. */
    draft?: ApprovalDraft;
}) {
    const host = useRunHost();
    const fields = Array.isArray(run.humanFields) ? run.humanFields : [];
    const decision = decisionField(fields);
    const [ownValues, setOwnValues] = useState<HumanValues>(() => initialHumanValues(fields));
    const [ownText, setOwnText] = useState('');
    const values = draft ? draft.values : ownValues;
    const setValues = draft ? draft.onValues : setOwnValues;
    const text = draft ? draft.text : ownText;
    const setText = draft ? draft.onText : setOwnText;
    const [sending, setSending] = useState<null | 'send' | 'yes' | 'no'>(null);
    const [error, setError] = useState('');
    const asking = [...run.history].reverse().find((h) => h.status === 'paused');
    const stepName = asking?.step_name || run.currentStepId;

    async function send(choice: 'send' | 'yes' | 'no') {
        setError('');
        setSending(choice);
        try {
            const answered = decision && choice !== 'send' ? { ...values, [decision.name]: choice === 'yes' } : values;
            const response = fields.length ? humanAnswer(fields, answered) : text;
            onDone(await host.answer(run.id, response));
        } catch (e) {
            setError(e instanceof Error ? e.message : 'The answer was not taken.');
        } finally {
            setSending(null);
        }
    }

    const locked = busy || sending !== null;
    // Nothing to send: free text, and none typed, where the page asks for some.
    const empty = requireAnswer && !fields.length && !text.trim();
    // The flag's label as a button: "Approve payment", not "Approve payment?".
    const yesLabel = decision ? humanFieldLabel(decision).replace(/\s*\?+\s*$/, '') : '';
    const others = decision ? fields.filter((f) => f !== decision) : fields;

    return (
        // One column that may shrink: a grid item is as wide as its longest word,
        // so a context of unbroken text (a model's `Q8\u202fC7…`) widened the
        // whole card past its frame and cut the prompt and its buttons (M3b).
        <section aria-label="Waiting for a person" className="grid grid-cols-[minmax(0,1fr)] gap-3 rounded-[14px] border border-person/40 bg-person-subtle px-[22px] py-5">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-text-muted">
                <Badge tone="person">Waiting for a person</Badge>
                {showWorkflow && <span className="font-medium text-text">{run.workflowName ?? 'Deleted workflow'}</span>}
                {showStep && stepName && <span title={run.currentStepId ?? undefined}>Step {stepName}</span>}
                {asking?.started_at && <span>· waiting <Took start={asking.started_at} end={null} /></span>}
            </div>
            {run.humanContext && (
                <figure className="m-0">
                    {/* Captioned with the state key it came from, when the run says (U65). */}
                    <figcaption className="mb-1.5 text-xs text-text-muted">
                        {run.humanContextKey
                            ? <>From <span className="font-code text-text">{run.humanContextKey}</span></>
                            : 'From this run'}
                    </figcaption>
                    <div className="max-h-64 overflow-y-auto rounded-md border border-border bg-surface p-4 [overflow-wrap:anywhere]">
                        <Markdown>{run.humanContext}</Markdown>
                    </div>
                </figure>
            )}
            <div className="max-w-[62ch] text-[17px] leading-normal tracking-[-0.012em] text-text [overflow-wrap:anywhere]">
                <Markdown>{run.humanPrompt ?? ''}</Markdown>
            </div>
            {(others.length > 0 || !fields.length) && (
                <div className="grid max-w-xl gap-3">
                    {fields.length ? (
                        <HumanFieldsForm fields={others} values={values} onChange={setValues} idPrefix={`run-${run.id}-field`} />
                    ) : (
                        <Field
                            label="Your answer"
                            hint={submitOnEnter ? (
                                <>
                                    <kbd className="rounded border border-border px-1 font-mono text-2xs">Enter</kbd> to send,{' '}
                                    <kbd className="rounded border border-border px-1 font-mono text-2xs">Shift Enter</kbd> for a new line
                                </>
                            ) : undefined}
                        >
                            <Textarea
                                rows={submitOnEnter ? 2 : 3}
                                value={text}
                                onChange={(e) => setText(e.target.value)}
                                placeholder="Type the answer this step is waiting for"
                                className={submitOnEnter ? 'field-sizing-content max-h-48 min-h-[3.25rem] resize-none' : undefined}
                                onKeyDown={submitOnEnter ? (e) => {
                                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                                        e.preventDefault();
                                        if (!locked && !empty) void send('send');
                                    }
                                } : undefined}
                            />
                        </Field>
                    )}
                </div>
            )}
            {error && <ErrorNote>{error}</ErrorNote>}
            <div className="flex flex-wrap items-center gap-2">
                {decision ? (
                    <>
                        <Button variant="person" onClick={() => send('yes')} loading={sending === 'yes'} disabled={locked}>
                            <Check className="size-4" aria-hidden />
                            {yesLabel}
                        </Button>
                        <Button variant="secondary" onClick={() => send('no')} loading={sending === 'no'} disabled={locked}>
                            <X className="size-4" aria-hidden />
                            Decline
                        </Button>
                    </>
                ) : (
                    <Button variant="person" onClick={() => send('send')} loading={sending === 'send'} disabled={locked || empty}>
                        Send answer
                    </Button>
                )}
                <Button
                    variant="ghost"
                    className="text-danger hover:bg-danger-subtle hover:text-danger"
                    disabled={locked}
                    onClick={onCancel}
                >
                    Cancel run
                </Button>
                {openHref && (
                    <Link
                        href={openHref}
                        className="ml-auto inline-flex h-9 items-center gap-1 rounded-lg px-2 text-sm font-medium text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
                    >
                        Open run
                        <ChevronRight className="size-4" aria-hidden />
                    </Link>
                )}
            </div>
        </section>
    );
}

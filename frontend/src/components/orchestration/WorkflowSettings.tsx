'use client';
/**
 * Workflow settings, in the open canvas's card (D204, M3a).
 *
 * The panels under the old canvas — State Schema and Guardrails — and the
 * description and run input that sat above and below it, as sections of one
 * card. Every field is the one it was: the same label, type, default and
 * coercion. Built from parts so each product composes its own: cloud adds its
 * Trigger control and keeps `StateSchemaEditor readOnly={!canEdit}` in its own
 * source, where its viewer guard reads it (M3b).
 */
import { CalendarClock, X } from 'lucide-react';
import { Field, IconButton, Input, controlStyles } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

/** The card's frame for the settings: a header with close, and a scrolling body of sections. */
export function WorkflowSettingsPanel({ name, onClose, children }: {
    name: string;
    onClose: () => void;
    children: React.ReactNode;
}) {
    return (
        <section aria-label="Workflow settings" className="flex h-full min-h-0 w-full flex-col">
            <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
                <span className="inline-flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-2 text-text-muted" aria-hidden>
                    <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                        <circle cx="3.5" cy="8" r="1.75" /><circle cx="12.5" cy="3.5" r="1.75" /><circle cx="12.5" cy="12.5" r="1.75" />
                        <path d="M5.2 7.2 10.8 4.3M5.2 8.8l5.6 2.9" />
                    </svg>
                </span>
                <div className="min-w-0 flex-1">
                    <h3 className="truncate text-[15px] font-semibold tracking-[-0.01em] text-text">Workflow settings</h3>
                    <p className="truncate text-xs text-text-faint" translate="no">{name || 'Untitled workflow'}</p>
                </div>
                <IconButton label="Close the workflow settings" onClick={onClose} size="sm" icon={X} />
            </div>
            <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">{children}</div>
        </section>
    );
}

export function SettingsSection({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <section className="space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-text-faint">{title}</h4>
            {children}
        </section>
    );
}

export function DescriptionField({ draft, onChange, readOnly }: {
    draft: Orchestration;
    onChange: (next: Orchestration) => void;
    readOnly?: boolean;
}) {
    return (
        <Input
            aria-label="Workflow description"
            value={draft.description}
            onChange={(e) => onChange({ ...draft, description: e.target.value })}
            placeholder="Description..."
            disabled={readOnly}
            size="sm"
        />
    );
}

/** Whether MCP clients see this workflow as a tool of its own (F466). Off by default: a workspace's
 *  dozens of workflows, each a tool, filled a client's context on every turn. Off, it still runs by name. */
export function McpOfferField({ draft, onChange, readOnly }: {
    draft: Orchestration;
    onChange: (next: Orchestration) => void;
    readOnly?: boolean;
}) {
    return (
        <label className="flex cursor-pointer items-start gap-2.5">
            <input
                type="checkbox"
                className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
                checked={!!draft.offer_to_mcp}
                disabled={readOnly}
                onChange={(e) => onChange({ ...draft, offer_to_mcp: e.target.checked })}
            />
            <span>
                <span className="block text-sm font-medium text-text">Offer to MCP clients</span>
                <span className="mt-0.5 block text-xs leading-relaxed text-text-faint">
                    Claude and other apps connected over MCP see this workflow as a tool of its own, with its
                    inputs. Off, they can still run it by its name; it is only not one more tool in their list.
                </span>
            </span>
        </label>
    );
}

/** The keys a run's result can name: each written by a step, labelled with the
 *  steps that write it. A saved key no step writes stays listed, so the select
 *  shows what is stored rather than silently reading as the default (F249).
 *  Moved here from `OrchestrationTab` (M3a). */
export function resultKeyOptions(orch: Orchestration): { key: string; label: string }[] {
    const writers = new Map<string, string[]>();
    for (const step of orch.steps) {
        if (!step.output_key) continue;
        writers.set(step.output_key, [...(writers.get(step.output_key) ?? []), step.name || step.id]);
    }
    const options = [...writers].map(([key, names]) => ({ key, label: `${key} — ${names.join(', ')}` }));
    if (orch.result_key && !writers.has(orch.result_key)) {
        options.push({ key: orch.result_key, label: `${orch.result_key} — written by no step` });
    }
    return options;
}

/** Max Total Turns, Timeout, strict state types, Result, Score runs against: exactly as the Guardrails tab had them. */
export function GuardrailsFields({ draft, onChange, rubrics, readOnly }: {
    draft: Orchestration;
    onChange: (next: Orchestration) => void;
    rubrics: { id: string; name: string }[];
    readOnly?: boolean;
}) {
    const select = cn(controlStyles({ size: 'sm' }), 'appearance-auto');
    return (
        <fieldset disabled={readOnly} className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
                <Field label="Max Total Turns">
                    <Input
                        id="orch-max-turns"
                        type="number"
                        size="sm"
                        value={draft.max_total_turns}
                        onChange={(e) => onChange({ ...draft, max_total_turns: parseInt(e.target.value) || 100 })}
                    />
                </Field>
                <Field label="Timeout (minutes)">
                    <Input
                        id="orch-timeout"
                        type="number"
                        size="sm"
                        value={draft.timeout_minutes}
                        onChange={(e) => onChange({ ...draft, timeout_minutes: parseInt(e.target.value) || 30 })}
                    />
                </Field>
            </div>
            {/* No "Max Cost (USD)": nothing ever enforced it, and batch 1C deleted it
                from the engine. A cap that does not cap is worse than none (F76). The cap per
                run that the engine enforces is under Cost per run (`RunCapFields`, CAP1). */}
            <label className="flex cursor-pointer items-start gap-2.5">
                <input
                    type="checkbox"
                    className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
                    checked={!!draft.strict_state_types}
                    onChange={(e) => onChange({ ...draft, strict_state_types: e.target.checked })}
                />
                <span>
                    <span className="block text-sm font-medium text-text">Stop on a state type mismatch</span>
                    <span className="mt-0.5 block text-xs leading-relaxed text-text-faint">
                        A step&apos;s output is read as the type its state key declares — JSON text becomes a list
                        or an object. Off, a step whose text will not convert leaves the text in place and warns.
                        On, the run stops there instead of carrying on with the wrong shape.
                    </span>
                </span>
            </label>
            <Field
                label="Result"
                hint={
                    'What a run makes: the answer when this is chatted with as an agent, what the API returns, and ' +
                    'what a parent workflow gets back when it runs this one. Left on the last step, a person’s ' +
                    'answer, an evaluator’s route and End never count.'
                }
            >
                <select
                    id="orch-result"
                    className={select}
                    value={draft.result_key ?? ''}
                    onChange={(e) => onChange({ ...draft, result_key: e.target.value })}
                >
                    <option value="">Last step that produces content</option>
                    {resultKeyOptions(draft).map(({ key, label }) => (
                        <option key={key} value={key}>{label}</option>
                    ))}
                </select>
            </Field>
            <Field
                label="Score runs against"
                hint={draft.rubric_id
                    ? 'Every run is judged when it finishes. The judge is a model call, billed like any other.'
                    : 'Pick a rubric to have an LLM judge score every run and flag regressions. Rubrics are written on the Evals screen.'}
            >
                <select
                    id="orch-rubric"
                    className={select}
                    value={draft.rubric_id ?? ''}
                    onChange={(e) => onChange({ ...draft, rubric_id: e.target.value })}
                >
                    <option value="">Not evaluated</option>
                    {rubrics.map((r) => (
                        <option key={r.id} value={r.id}>{r.name}</option>
                    ))}
                </select>
            </Field>
        </fieldset>
    );
}

/** A schedule as the editor already has it (`/api/schedules`); only what this list shows. */
export type WorkflowSchedule = {
    id: string;
    name: string;
    enabled?: boolean;
    target_type?: string;
    target_id?: string;
    schedule_type?: 'interval' | 'cron';
    interval_value?: number | null;
    interval_unit?: string | null;
    cron_expression?: string | null;
};

/** The schedules that start this workflow, read-only, with the way to change them. */
export function SchedulesList({ schedules, workflowId, href }: {
    schedules: WorkflowSchedule[];
    workflowId: string;
    /** Where schedules are made and changed. */
    href: string;
}) {
    const mine = schedules.filter((s) => s.target_type === 'orchestration' && s.target_id === workflowId);
    return (
        <div className="space-y-2">
            {mine.length === 0 ? (
                <p className="text-sm text-text-muted">Only when started: no schedule runs this workflow.</p>
            ) : (
                <ul className="divide-y divide-border rounded-lg border border-border">
                    {mine.map((s) => (
                        <li key={s.id} className="flex items-center gap-2.5 px-3 py-2 text-sm">
                            <CalendarClock className="size-4 shrink-0 text-text-faint" aria-hidden />
                            <span className="min-w-0 flex-1">
                                <span className="block truncate text-text" translate="no">{s.name}</span>
                                <span className="block truncate font-mono text-xs text-text-faint">
                                    {s.schedule_type === 'cron'
                                        ? s.cron_expression
                                        : `every ${s.interval_value ?? '?'} ${s.interval_unit ?? ''}`.trim()}
                                </span>
                            </span>
                            {s.enabled === false && <span className="text-xs text-text-faint">Off</span>}
                        </li>
                    ))}
                </ul>
            )}
            <a href={href} className="inline-block text-sm text-accent-ink hover:underline">
                {mine.length === 0 ? 'Add a schedule in Schedules' : 'Change them in Schedules'}
            </a>
        </div>
    );
}

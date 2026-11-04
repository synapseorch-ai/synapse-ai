'use client';

/*
 * "Needs you", Home's first section (M2a; UX1: "the approval as Home's hero").
 *
 * Everything that waits on a person, answered where it is read:
 * - a run paused at a human step is its own `ApprovalCard` — the run page's
 *   card, so the answer, its fields and its refusals are the run page's, sent
 *   through the same `RunHost.answer`
 * - a scheduled agent's tool call waiting for approval (F268) is a card around
 *   the product's own Approve / Refuse control, the bell's
 *
 * Several stack; past `MAX_CARDS` (`home.ts`) the rest are rows that open their run. With
 * nothing waiting there is one calm line. An answered card says so in place
 * and stays until the page is left, so the reader sees their answer was taken.
 */
import Link from 'next/link';
import { useState, type ReactNode } from 'react';
import { CheckCircle2, ChevronRight } from 'lucide-react';

import { Badge, ErrorNote } from '@/components/ui';
import { cn } from '@/lib/cn';

import { ApprovalCard } from './ApprovalCard';
import { useRunHost } from './host';
import { DELETED_WORKFLOW, type RunRecord } from './model';
import { Took, useClock, When } from './When';


/** A waiting run shown as a row: past the cards, or when its record could not be read. */
export type WaitingBrief = { id: string; workflowName: string | null; prompt: string | null; since: string | null };

/** A tool call waiting for approval (a scheduled agent's, or a phone call's), with the product's own control. */
export type ToolCall = {
    id: string; title: string; body?: string | null; at?: string | null; actions: ReactNode;
    /** Who asked, in a few words; a scheduled agent's when absent. */
    source?: string;
};

/** Which question of a run a card answers: a run can ask again later. */
const questionOf = (run: RunRecord) =>
    `${run.id}:${[...run.history].reverse().find((h) => h.status === 'paused')?.started_at ?? run.currentStepId ?? ''}`;

type Done = { key: string; message: string; name: string | null };

export function NeedsYou({ runs, more = [], tools = [], hrefOf, onChanged }: {
    /** Runs to answer in place (up to `MAX_CARDS`), oldest wait first. */
    runs: RunRecord[];
    more?: WaitingBrief[];
    tools?: ToolCall[];
    /** A run's page. */
    hrefOf: (runId: string) => string;
    /** Read Home again after an answer or a cancel. */
    onChanged: () => void;
}) {
    const host = useRunHost();
    const clock = useClock();
    const [done, setDone] = useState<Record<string, Done>>({});
    const [busy, setBusy] = useState<Record<string, boolean>>({});
    const [errors, setErrors] = useState<Record<string, string>>({});

    const open = runs.filter((r) => done[r.id]?.key !== questionOf(r));
    const count = open.length + more.length + tools.length;
    const settled = Object.entries(done).filter(([id, d]) => !open.some((r) => r.id === id) && d);

    const finish = (run: RunRecord, message: string) => {
        setDone((cur) => ({ ...cur, [run.id]: { key: questionOf(run), message, name: run.workflowName } }));
        onChanged();
    };
    const cancel = async (run: RunRecord) => {
        setBusy((b) => ({ ...b, [run.id]: true }));
        setErrors((e) => ({ ...e, [run.id]: '' }));
        try {
            finish(run, await host.cancel(run.id));
        } catch (e) {
            setErrors((cur) => ({ ...cur, [run.id]: e instanceof Error ? e.message : 'The run was not cancelled.' }));
        } finally {
            setBusy((b) => ({ ...b, [run.id]: false }));
        }
    };

    return (
        <section aria-labelledby="home-needs-you" className="grid gap-2.5">
            <h2 id="home-needs-you" className="flex items-center gap-2 text-xs font-semibold tracking-[0.02em] text-text-faint">
                <span aria-hidden className={cn('size-2 rounded-full bg-person', count > 0 && 'motion-safe:animate-pulse')} />
                Needs you · {count}
            </h2>
            {count === 0 && !settled.length && <p className="text-sm text-text-muted">Nothing is waiting on you.</p>}
            <div className="grid gap-3 empty:hidden">
                {open.map((run) => (
                    <div key={run.id} className="grid gap-2">
                        <ApprovalCard
                            run={run}
                            busy={Boolean(busy[run.id])}
                            onDone={(message) => finish(run, message)}
                            onCancel={() => void cancel(run)}
                            openHref={hrefOf(run.id)}
                            showWorkflow
                        />
                        {errors[run.id] && <ErrorNote>{errors[run.id]}</ErrorNote>}
                    </div>
                ))}
                {tools.map((t) => (
                    <section key={t.id} aria-label="Waiting for an approval" className="grid gap-2 rounded-[14px] border border-person/40 bg-person-subtle px-[22px] py-4">
                        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-text-muted">
                            <Badge tone="person">Needs approval</Badge>
                            <span>{t.source ?? 'A scheduled agent’s tool call'}</span>
                            {t.at && <span>· <When iso={t.at} clock={clock} /></span>}
                        </div>
                        <p className="m-0 text-[15px] font-medium text-text">{t.title}</p>
                        {t.body && <p className="m-0 max-w-[70ch] text-sm text-text-muted">{t.body}</p>}
                        {t.actions}
                    </section>
                ))}
                {settled.map(([id, d]) => (
                    <div key={id} role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-[14px] border border-border bg-surface px-[22px] py-3.5 text-sm">
                        <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden />
                        {d.name && <span className="font-medium text-text">{d.name}</span>}
                        <span className="text-text-muted">{d.message}</span>
                        <OpenRun href={hrefOf(id)} />
                    </div>
                ))}
            </div>
            {more.length > 0 && (
                <ul aria-label="Also waiting" className="m-0 grid list-none border-t border-border p-0">
                    {more.map((m) => (
                        <li key={m.id} className="grid items-center gap-x-3.5 border-b border-border px-2.5 py-2.5 [grid-template-columns:14px_minmax(0,1fr)_auto] sm:[grid-template-columns:14px_minmax(0,14rem)_minmax(0,1fr)_auto_auto]">
                            <span aria-hidden className="size-2 rounded-full bg-person" />
                            <span className="truncate text-sm font-medium text-text">{m.workflowName ?? DELETED_WORKFLOW}</span>
                            <span className="hidden truncate text-sm text-text-muted sm:block">{m.prompt || 'Input required'}</span>
                            <span className="hidden whitespace-nowrap text-xs tabular-nums text-text-faint sm:block">
                                {m.since ? <>waiting <Took start={m.since} end={null} /></> : null}
                            </span>
                            <OpenRun href={hrefOf(m.id)} />
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}

function OpenRun({ href }: { href: string }) {
    return (
        <Link href={href} className="ml-auto inline-flex items-center gap-1 whitespace-nowrap text-sm font-medium text-accent-ink hover:underline">
            Open run
            <ChevronRight className="size-4" aria-hidden />
        </Link>
    );
}

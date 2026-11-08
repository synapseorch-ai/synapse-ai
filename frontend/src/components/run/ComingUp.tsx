'use client';

/*
 * What runs next (M2a; UX1's Home): the next scheduled runs, soonest first —
 * a day box, the schedule's name, the time and what it runs.
 *
 * With nothing scheduled it says how a workflow can start without anyone
 * pressing Run — a schedule, a channel, or the workspace's own systems — each
 * with where to set it up, and one tip from the week when there is one to give
 * (Naveen: "if schedules are empty can we show instructions and how it can help
 * and different options to choose").
 */
import Link from 'next/link';
import type { ReactNode } from 'react';
import { CalendarClock, MessageSquare, Sparkles, Webhook } from 'lucide-react';

import { buttonStyles } from '@/components/ui';
import { cn } from '@/lib/cn';

import { dayBox } from './home';
import { timeOfDay, type Clock } from './model';

export type Upcoming = {
    id: string;
    /** The schedule's name. */
    name: string;
    /** Its next run (ISO). */
    at: string;
    /** What it runs: the workflow's or the agent's name. */
    target?: string | null;
};

/** Where each product sets up the three ways a workflow starts on its own. */
export type StartWays = { schedules: string; channels: string; api: string };

export function ComingUp({ items, clock, ways, tip }: {
    items: Upcoming[];
    clock: Clock;
    /** With nothing scheduled: the ways a workflow can start on its own. */
    ways?: StartWays;
    /** With nothing scheduled: one suggestion from the week, with its action. */
    tip?: { text: string; action: ReactNode } | null;
}) {
    if (!items.length) {
        if (!ways) return <p className="text-sm text-text-faint">Nothing scheduled.</p>;
        const rows = [
            { icon: CalendarClock, title: 'On a schedule', detail: 'Every weekday at 9, every hour, or a cron of your own.', href: ways.schedules, label: 'Add a schedule' },
            { icon: MessageSquare, title: 'From a message', detail: 'When someone writes in Slack, Telegram, Discord or a chat widget.', href: ways.channels, label: 'Channels' },
            { icon: Webhook, title: 'From your systems', detail: 'An API call, a webhook, or a command line, with a key of its own.', href: ways.api, label: 'API keys' },
        ];
        return (
            <div className="grid gap-3">
                <p className="m-0 text-sm text-text-muted">Nothing runs on its own yet. A workflow can start:</p>
                <ul aria-label="Ways a workflow starts on its own" className="m-0 grid list-none border-t border-border p-0">
                    {rows.map((r) => (
                        <li key={r.title} className="grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3 border-b border-border py-3">
                            <span className="grid size-8 place-items-center rounded-lg bg-surface-2 text-text-muted" aria-hidden>
                                <r.icon className="size-4" />
                            </span>
                            <span className="min-w-0">
                                <span className="block text-sm font-medium text-text">{r.title}</span>
                                <span className="block text-[12.5px] text-text-faint">{r.detail}</span>
                            </span>
                            <Link href={r.href} className={cn(buttonStyles({ variant: 'secondary', size: 'sm' }), 'whitespace-nowrap')}>
                                {r.label}
                            </Link>
                        </li>
                    ))}
                </ul>
                {tip && (
                    <div className="grid grid-cols-[20px_minmax(0,1fr)] gap-x-2.5 gap-y-2 rounded-xl bg-accent-subtle px-3.5 py-3">
                        <Sparkles className="mt-0.5 size-4 text-accent-ink" aria-hidden />
                        <p className="m-0 text-sm text-text">{tip.text}</p>
                        <div className="col-start-2">{tip.action}</div>
                    </div>
                )}
            </div>
        );
    }
    return (
        <ul aria-label="Coming up" className="m-0 grid list-none border-t border-border p-0">
            {items.map((u) => {
                const box = dayBox(u.at, clock);
                return (
                    <li key={u.id} className="grid grid-cols-[40px_minmax(0,1fr)] items-center gap-3 border-b border-border py-3">
                        <span className="text-center text-[11px] font-semibold uppercase leading-tight text-text-faint" suppressHydrationWarning>
                            {box.weekday}
                            <b className="block text-lg font-semibold leading-tight text-text" suppressHydrationWarning>{box.day}</b>
                        </span>
                        <span className="min-w-0">
                            <span className="block truncate text-sm font-semibold text-text">{u.name}</span>
                            <span className="block truncate text-[12.5px] text-text-faint" suppressHydrationWarning>
                                {timeOfDay(u.at, clock)}{u.target ? ` · ${u.target}` : ''}
                            </span>
                        </span>
                    </li>
                );
            })}
        </ul>
    );
}

/** The next `n` enabled schedules by their next run. */
export function upcoming<S extends { id: string; name?: string; enabled?: boolean; next_run_at?: string | null; target_type?: string; target_id?: string }>(
    schedules: S[],
    names: (s: S) => string | null,
    n = 4,
): Upcoming[] {
    return schedules
        .filter((s) => s.enabled !== false && s.next_run_at)
        .sort((a, b) => Date.parse(a.next_run_at as string) - Date.parse(b.next_run_at as string))
        .slice(0, n)
        .map((s) => ({ id: s.id, name: s.name || 'Unnamed schedule', at: s.next_run_at as string, target: names(s) }));
}

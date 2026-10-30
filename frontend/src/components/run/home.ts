/*
 * Home's numbers and words (M2a; UX1's approval-first Home), pure, so both
 * products' Homes and a test derive the same answer from the same rows.
 *
 * Like the rest of `components/run/`, nothing here reads an API: OSS counts its
 * run list in the browser, cloud reads its summary on the server, and each
 * hands the figures in.
 */
import { bucketOf, type Clock, type RunBucket, type RunRow } from './model';

const DAY_MS = 86_400_000;

/**
 * How many waiting runs Home answers in place before the rest become rows. Here, not in
 * `NeedsYou`, so a server page can read it: a client module's exports reach a server
 * component as references, not values.
 */
export const MAX_CARDS = 5;

/** How many runs the week's bars draw at most. */
export const WEEK_MAX = 60;

const startMs = (r: Pick<RunRow, 'startedAt' | 'createdAt'>) => {
    const iso = r.startedAt || r.createdAt;
    const ms = iso ? Date.parse(iso) : NaN;
    return Number.isNaN(ms) ? null : ms;
};

/** The runs that started in the seven days before `now`, oldest first. */
export function weekRuns<R extends Pick<RunRow, 'startedAt' | 'createdAt'>>(rows: R[], now: number): R[] {
    return rows
        .filter((r) => {
            const ms = startMs(r);
            return ms !== null && ms > now - 7 * DAY_MS && ms <= now + 60_000;
        })
        .sort((a, b) => (startMs(a) ?? 0) - (startMs(b) ?? 0));
}

export type WeekStats = {
    runs: number;
    completed: number;
    failed: number;
    /** The newest failure of the week, for the strip's link. */
    lastFailure: RunRow | null;
};

/** Counts over a week's runs (from `weekRuns`). */
export function weekStats(week: RunRow[]): WeekStats {
    let completed = 0, failed = 0;
    let lastFailure: RunRow | null = null;
    for (const r of week) {
        const b = bucketOf(r.status, r.waitingForHuman);
        if (b === 'completed') completed += 1;
        if (b === 'failed') { failed += 1; lastFailure = r; }
    }
    return { runs: week.length, completed, failed, lastFailure };
}

/** "91.7%", as cloud's summary rounds it; `—` when nothing ran. */
export function rate(completed: number, runs: number): string {
    if (!runs) return '—';
    const r = Math.round((completed / runs) * 1000) / 10;
    return `${r}%`;
}

const SMALL = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine'];
const counted = (n: number) => (n < SMALL.length ? SMALL[n] : String(n));

/**
 * Home's one status line, under the greeting: what needs the reader, then how
 * the rest of the week went.
 */
export function statusLine({ waiting, runs, failed, starting = false }: {
    waiting: number; runs: number; failed: number;
    /** A new workspace on its first-workflow path. */
    starting?: boolean;
}): string {
    if (starting && waiting === 0 && runs === 0) return 'Let’s get your first workflow running. It takes about ten minutes.';
    const first = waiting === 0
        ? 'Nothing needs you.'
        : `${counted(waiting)} ${waiting === 1 ? 'thing needs' : 'things need'} you.`;
    let second: string;
    if (failed > 0) second = `${counted(failed)} ${failed === 1 ? 'run' : 'runs'} failed this week.`;
    else if (runs === 0) second = waiting ? '' : 'Nothing has run this week.';
    else second = waiting ? 'Everything else ran as planned.' : 'Everything ran as planned.';
    return second ? `${first} ${second}` : first;
}

/** "Good evening, Riley" in the reader's own time; `null` until the page has mounted. */
export function greeting(clock: Clock, name: string | null | undefined): string | null {
    if (!Number.isFinite(clock.now)) return null;
    const hour = Number(new Intl.DateTimeFormat('en-US', { timeZone: clock.timeZone, hour: 'numeric', hourCycle: 'h23' }).format(new Date(clock.now)));
    const part = hour < 5 ? 'evening' : hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
    const who = name?.trim();
    return who ? `Good ${part}, ${who}` : `Good ${part}`;
}

/** "Tuesday 6 October", the reader's today; `null` until mounted. */
export function today(clock: Clock): string | null {
    if (!Number.isFinite(clock.now)) return null;
    return new Intl.DateTimeFormat('en-GB', { timeZone: clock.timeZone, weekday: 'long', day: 'numeric', month: 'long' }).format(new Date(clock.now));
}

/** A person's first name from one free-text name ("Riley Morgan" → "Riley"). */
export function firstName(name: string | null | undefined): string | null {
    const first = (name ?? '').trim().split(/\s+/)[0];
    return first || null;
}

/** "Mon" and "12": a day's box in Coming up, in the reader's zone (UTC until mounted). */
export function dayBox(iso: string, clock: Clock): { weekday: string; day: string } {
    const zone = Number.isFinite(clock.now) ? clock.timeZone : 'UTC';
    const d = new Date(iso);
    return {
        weekday: new Intl.DateTimeFormat('en-GB', { timeZone: zone, weekday: 'short' }).format(d),
        day: new Intl.DateTimeFormat('en-GB', { timeZone: zone, day: 'numeric' }).format(d),
    };
}

/** What a week bar is: the outcome's colour class, and whether its height says how long it took. */
export const WEEK_BAR: Record<RunBucket, string> = {
    completed: 'bg-[color-mix(in_oklab,var(--success)_45%,var(--surface-2))]',
    failed: 'bg-danger',
    needs: 'bg-person',
    running: 'bg-machine',
    queued: 'bg-border-strong',
    cancelled: 'bg-border-strong',
};

/** A bar's height in percent: a finished run by how long it took (square root, so one slow run does not flatten the rest). */
export function barHeights(rows: RunRow[]): number[] {
    const secs = rows.map((r) => {
        const a = r.startedAt ? Date.parse(r.startedAt) : NaN;
        const b = r.endedAt ? Date.parse(r.endedAt) : NaN;
        return Number.isNaN(a) || Number.isNaN(b) ? null : Math.max(0, (b - a) / 1000);
    });
    const max = Math.max(1, ...secs.map((s) => (s === null ? 0 : Math.sqrt(s))));
    return rows.map((r, i) => {
        const b = bucketOf(r.status, r.waitingForHuman);
        const s = secs[i];
        if (b !== 'completed' || s === null) return 100;
        return Math.round(30 + 70 * (Math.sqrt(s) / max));
    });
}

// ── Getting started (M2a, Naveen's ask): a new workspace's first workflow as a path ──

export type StartStepId = 'model' | 'build' | 'run' | 'approve' | 'own' | 'connect';

export type StartStep = {
    id: StartStepId;
    title: string;
    done: boolean;
    /** Not needed to finish: the path is done without it. */
    optional?: boolean;
    /** What was done, on a done step ("Invoice triage, built today"). */
    doneText?: string;
    /** What to do, on a step not done yet. */
    detail: string;
    /** One thing worth knowing at this step. */
    tip?: string;
};

/** What each product knows about a workspace, for its first-workflow path. */
export type StartFacts = {
    /** A model the workspace can call: a provider key or address set, managed models, or a run that completed. */
    modelReady: boolean;
    /** What "done" says about the model ("Using gemini-2.5-flash", "Managed models are included"). */
    modelText?: string;
    /** The newest workflow of the workspace's own, if any. */
    workflow: { id: string; name: string } | null;
    /** The newest run, if any. */
    lastRun: { at: string | null; name: string | null } | null;
    /** A workflow with a Human step, if any. */
    humanWorkflow: { id: string; name: string } | null;
    /** A run of a workflow with a Human step completed: its question was answered. */
    answered: boolean;
    /** Something waits on the reader right now. */
    waiting: boolean;
    /** An enabled schedule (or another way it starts itself). */
    runsOnItsOwn: boolean;
    /** Cloud: the connections the onboarding answers named, and which are made. */
    connect?: { label: string; connected: boolean }[];
};

/** The path's steps, in order (one list for both products, Naveen's call). */
export function startSteps(f: StartFacts): StartStep[] {
    const steps: StartStep[] = [
        {
            id: 'model', title: 'Connect a model', done: f.modelReady, doneText: f.modelText,
            detail: 'Add a provider’s key, or point the workspace at a model you run yourself.',
        },
        {
            id: 'build', title: 'Build a workflow', done: Boolean(f.workflow),
            doneText: f.workflow ? `“${f.workflow.name}”` : undefined,
            detail: 'Start from a template, or describe what you want and Synapse AI drafts it.',
            tip: 'A template is a finished workflow: run it as it is, then change it.',
        },
        {
            id: 'run', title: 'Run it once', done: Boolean(f.lastRun),
            doneText: f.lastRun?.name ? `“${f.lastRun.name}” ran` : 'It ran',
            detail: 'Open it, give it an input and press Run.',
            tip: 'Each step lights up as it works, and the run’s page shows the path it took and what it cost.',
        },
        {
            id: 'approve', title: 'Answer its first question', done: f.answered,
            // Without a Human step there is nothing to answer: shown, not required.
            optional: !f.humanWorkflow && !f.waiting,
            doneText: 'Answered, and the run carried on',
            detail: f.waiting
                ? 'A run is waiting on you: answer it under Needs you.'
                : f.humanWorkflow
                    ? `When “${f.humanWorkflow.name}” reaches its Human step, it waits on Home until you answer.`
                    : 'Add a Human step where a person should approve, and the run asks you here before it goes on.',
            tip: 'An approval is answered in place, here or on the run’s page; the run carries on with your answer.',
        },
        {
            id: 'own', title: 'Let it run on its own', done: f.runsOnItsOwn,
            doneText: 'It starts on its own',
            detail: 'Put it on a schedule, connect a channel, or start it from your own systems.',
            tip: 'Ask Synapse AI: “run it every weekday at 9”. It sets the schedule up for you.',
        },
    ];
    if (f.connect && f.connect.length) {
        const pending = f.connect.filter((c) => !c.connected).map((c) => c.label);
        steps.push({
            id: 'connect', title: 'Connect what you told us about', done: pending.length === 0, optional: true,
            doneText: `${f.connect.map((c) => c.label).join(', ')} connected`,
            detail: `Connect ${pending.join(', ')} so its steps can use ${pending.length === 1 ? 'it' : 'them'}.`,
        });
    }
    return steps;
}

/** The path is done once every step it needs is. */
export const startDone = (steps: StartStep[]) => steps.every((s) => s.done || s.optional);

/** The workflow the reader ran most by hand this week, at least twice: worth putting on a schedule. */
export function byHandFavourite(week: Pick<RunRow, 'workflowId' | 'workflowName' | 'trigger'>[]): { workflowId: string; name: string; count: number } | null {
    const counts = new Map<string, { name: string; count: number }>();
    for (const r of week) {
        if (r.trigger !== 'Manual' || !r.workflowName) continue;
        const c = counts.get(r.workflowId) ?? { name: r.workflowName, count: 0 };
        c.count += 1;
        counts.set(r.workflowId, c);
    }
    let best: { workflowId: string; name: string; count: number } | null = null;
    for (const [workflowId, c] of counts) if (c.count >= 2 && (!best || c.count > best.count)) best = { workflowId, ...c };
    return best;
}

/** What Home's ask line knows of the workspace: what waits, the newest failure today, whether something runs now. */
export type AskFacts = {
    waiting: number;
    /** The workflow of the newest run that failed in the last day, if one did. */
    failedWorkflow?: string | null;
    running: number;
    /** No workflow yet: Getting started's build suggestions lead instead. */
    empty: boolean;
    /** The hour of the day where the person is, for "overnight" before noon. */
    hour: number | null;
};

/**
 * The ask line's suggestions (ASSIST1, D291; Naveen: "a few short suggestions to ask it"), at most four, in the
 * order they matter: what waits on the person, then why something failed, then the day so far. A new workspace has
 * none: its Getting started path suggests what to build.
 */
export function askSuggestions(f: AskFacts): string[] {
    if (f.empty) return [];
    const out: string[] = [];
    if (f.waiting > 0) out.push('What needs me?');
    if (f.failedWorkflow) out.push(`Why did “${f.failedWorkflow}” fail?`);
    if (f.running > 0) out.push('What’s running right now?');
    out.push('Summarise today');
    if (f.hour !== null && f.hour < 12) out.push('What ran overnight?');
    else out.push('What did my agents do today?');
    return out.slice(0, 4);
}

/** A part of the product Home can point at until the workspace has used it (GUIDE1, F914/F917). */
export type TryKey = 'evals' | 'knowledge' | 'channels';

/**
 * Home's "More you can do" (GUIDE1, D318, F914; Naveen: "evals usage all are great features but not visible to
 * users"). Each says what it is for, in plain words, and where it is; the same routes in both products.
 */
export const MORE_TO_TRY: Record<TryKey, { title: string; line: string; href: string }> = {
    evals: {
        title: 'Check your workflows keep doing well',
        line: 'Say what a good result looks like, and each judged run gets a score. You hear when quality drops.',
        href: '/evals',
    },
    knowledge: {
        title: 'Answer from your own documents',
        line: 'Give agents a folder of files to look things up in. They say where each answer came from.',
        href: '/knowledge',
    },
    channels: {
        title: 'Reach your agents from Slack or Telegram',
        line: 'People talk to an agent where they already chat.',
        href: '/channels',
    },
};

/**
 * Which of `MORE_TO_TRY` to show: the ones this product offers here (`offered`, in order) that the workspace hasn't
 * used. A key not yet known (still loading, or unreadable) is left out, so nothing is suggested and then pulled.
 */
export function moreToTry(offered: readonly TryKey[], used: Partial<Record<TryKey, boolean>>): TryKey[] {
    return offered.filter(key => used[key] === false).slice(0, 4);
}

'use client';
/*
 * The table of workflow runs: status, workflow, step, trigger, progress, cost,
 * score, start and duration, newest first.
 *
 * Lifted out of the Workflows landing (`OrchestrationTab`) when Runs became a
 * page of its own (D209, M1a), so the two show one table and not two copies.
 * The markup is the landing's, unchanged: its look is M2's to redo. What a row
 * opens is the caller's — the landing restores the run onto its canvas in place,
 * the Runs page links to it.
 */

export type RunSummary = {
    run_id: string;
    orchestration_id: string;
    status: string;
    started_at?: string | null;
    ended_at?: string | null;
    current_step_id?: string | null;
    steps_completed?: number;
    last_step_name?: string | null;
    waiting_for_human?: boolean;
    total_cost_usd?: number | null;
    session_id?: string | null;
    /** The judge's latest verdict, when the orchestration has a rubric
     *  bound. `null` is "not scored", which is not the same as zero. */
    score?: number | null;
    score_regression?: boolean;
};

/** What the table needs of a workflow: its name, and its steps' names for the Step column. */
export type RunsTableWorkflow = { id: string; name: string; steps: { id: string; name: string }[] };

export const isLiveRun = (run: RunSummary) => run.status === 'running' || run.status === 'paused';

export function runStatusMeta(run: RunSummary): { dot: string; label: string; text: string } {
    switch (run.status) {
        case 'running': return { dot: 'bg-blue-400 animate-pulse', label: 'Running', text: 'text-blue-300' };
        case 'paused': return run.waiting_for_human
            ? { dot: 'bg-yellow-400', label: 'Needs input', text: 'text-yellow-300' }
            : { dot: 'bg-yellow-400', label: 'Paused', text: 'text-yellow-300' };
        case 'completed': return { dot: 'bg-green-500', label: 'Completed', text: 'text-zinc-400' };
        case 'cancelled': return { dot: 'bg-zinc-500', label: 'Cancelled', text: 'text-text-faint' };
        default: return { dot: 'bg-red-500', label: 'Failed', text: 'text-red-300' };
    }
}

// Colour band for a judge score. A flagged run is red whatever it scored:
// the point of the column is to be scannable, and "this one regressed" is
// the fact worth seeing from across the table.
function runScoreMeta(run: RunSummary): { text: string; title: string } {
    if (run.score === null || run.score === undefined) {
        return { text: 'text-text-faint', title: 'Not scored' };
    }
    if (run.score_regression) {
        return { text: 'text-red-300', title: 'Scored below expectation' };
    }
    if (run.score >= 7.5) return { text: 'text-green-400', title: 'Scored well' };
    if (run.score >= 5) return { text: 'text-yellow-300', title: 'Scored moderately' };
    return { text: 'text-red-300', title: 'Scored poorly' };
}

function fmtDuration(start?: string | null, end?: string | null): string {
    if (!start) return '—';
    const s = new Date(start).getTime();
    const e = end ? new Date(end).getTime() : Date.now();
    const sec = Math.max(0, Math.round((e - s) / 1000));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m ${sec % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
}

const thCls = 'px-4 py-2 text-[10px] font-medium uppercase tracking-wider text-text-faint whitespace-nowrap';
const tdCls = 'px-4 py-2.5 text-xs';

export function RunsTable({
    rows, empty, workflows, schedules, onOpen,
}: {
    rows: RunSummary[];
    empty: string;
    workflows: RunsTableWorkflow[];
    /** Schedule names, for labelling schedule-triggered runs. */
    schedules: { id: string; name: string }[];
    onOpen: (run: RunSummary) => void;
}) {
    const orchNameOf = (id: string) => workflows.find(o => o.id === id)?.name ?? id;
    const currentStepNameOf = (run: RunSummary): string | null => {
        const orch = workflows.find(o => o.id === run.orchestration_id);
        const byId = run.current_step_id
            ? orch?.steps.find(s => s.id === run.current_step_id)?.name
            : null;
        return byId || run.last_step_name || null;
    };
    // What kicked this run off. Derived from session_id, which the scheduler
    // sets to `schedule_<schedule_id>` and the orchestrator-agent path sets to
    // the chat session id; manual UI and API runs carry none.
    const triggerOf = (run: RunSummary): string => {
        const session = run.session_id || '';
        if (session.startsWith('schedule_')) {
            const schedId = session.slice('schedule_'.length);
            const sched = schedules.find(s => s.id === schedId);
            return sched ? `Schedule · ${sched.name}` : 'Schedule';
        }
        if (session) return 'Agent chat';
        return 'Manual';
    };

    return (
        <div className="border border-border bg-zinc-900/60 rounded-md">
            <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                    <thead>
                        <tr className="border-b border-border bg-zinc-950/40">
                            <th className={thCls}>Status</th>
                            <th className={thCls}>Orchestration</th>
                            <th className={thCls}>Step</th>
                            <th className={thCls}>Trigger</th>
                            <th className={`${thCls} text-right`}>Progress</th>
                            <th className={`${thCls} text-right`}>Cost</th>
                            <th className={`${thCls} text-right`}>Score</th>
                            <th className={`${thCls} text-right`}>Started</th>
                            <th className={`${thCls} text-right`}>Duration</th>
                            <th className={thCls}></th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.length === 0 ? (
                            <tr>
                                <td colSpan={10} className="px-4 py-10 text-center text-xs text-text-faint italic">
                                    {empty}
                                </td>
                            </tr>
                        ) : rows.map(run => {
                            const meta = runStatusMeta(run);
                            const stepName = currentStepNameOf(run);
                            const isLive = isLiveRun(run);
                            return (
                                <tr
                                    key={run.run_id}
                                    onClick={() => onOpen(run)}
                                    className="border-b border-border last:border-b-0 hover:bg-row-hover cursor-pointer transition-colors group"
                                >
                                    <td className={`${tdCls} whitespace-nowrap`}>
                                        <span className="inline-flex items-center gap-2">
                                            <span className={`w-2 h-2 rounded-full shrink-0 ${meta.dot}`} />
                                            <span className={meta.text}>{meta.label}</span>
                                        </span>
                                    </td>
                                    <td className={`${tdCls} text-zinc-200 max-w-[220px]`}>
                                        <span className="block truncate">{orchNameOf(run.orchestration_id)}</span>
                                        <span className="block text-[10px] text-text-faint truncate">{run.run_id}</span>
                                    </td>
                                    <td className={`${tdCls} max-w-[200px]`}>
                                        {stepName ? (
                                            <span className={`block truncate ${isLive ? 'text-zinc-300' : 'text-text-faint'}`}>
                                                {stepName}
                                            </span>
                                        ) : <span className="text-text-faint">—</span>}
                                    </td>
                                    <td className={`${tdCls} text-text-faint max-w-[160px]`}>
                                        <span className="block truncate">{triggerOf(run)}</span>
                                    </td>
                                    <td className={`${tdCls} text-right text-zinc-400 whitespace-nowrap`}>
                                        {run.steps_completed ?? 0} step{(run.steps_completed ?? 0) === 1 ? '' : 's'}
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {run.total_cost_usd ? `$${run.total_cost_usd.toFixed(4)}` : '—'}
                                    </td>
                                    <td className={`${tdCls} text-right whitespace-nowrap`}>
                                        <span
                                            className={runScoreMeta(run).text}
                                            title={runScoreMeta(run).title}
                                        >
                                            {run.score === null || run.score === undefined
                                                ? '—'
                                                : run.score.toFixed(1)}
                                        </span>
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {fmtDuration(run.started_at, run.ended_at)}
                                    </td>
                                    <td className={`${tdCls} text-right whitespace-nowrap`}>
                                        {/* The row opens on a click; this is the same for a keyboard (F219). */}
                                        <button
                                            type="button"
                                            onClick={(e) => { e.stopPropagation(); onOpen(run); }}
                                            aria-label={`View run ${run.run_id}`}
                                            className="text-[11px] text-text-faint group-hover:text-zinc-300 hover:text-zinc-100 transition-colors rounded-sm"
                                        >
                                            View →
                                        </button>
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

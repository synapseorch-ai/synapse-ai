/*
 * A row of `GET /api/orchestrations/runs`, and the two things the Workflows
 * landing reads off one: is it live, and how its status reads.
 *
 * The table these sat beside (`RunsTable`, lifted from the landing in M1a) is
 * the shared `components/run/RunList` now (M2b); OSS's rows are mapped onto it
 * by `lib/runHost.tsx`.
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
    /** Why a failed or cancelled run stopped (F257). */
    error?: string | null;
};

export const isLiveRun = (run: RunSummary) => run.status === 'running' || run.status === 'paused';

/** A run's status as a dot and a word, on the colour rule: cyan working, amber a person, quiet success. */
export function runStatusMeta(run: RunSummary): { dot: string; label: string; text: string } {
    switch (run.status) {
        case 'running': return { dot: 'bg-machine motion-safe:animate-pulse', label: 'Running', text: 'text-accent-ink' };
        case 'paused': return run.waiting_for_human
            ? { dot: 'bg-person', label: 'Needs input', text: 'font-medium text-person-ink' }
            : { dot: 'bg-person', label: 'Paused', text: 'font-medium text-person-ink' };
        case 'completed': return { dot: 'bg-success', label: 'Completed', text: 'text-text-muted' };
        case 'cancelled': return { dot: 'bg-text-faint', label: 'Cancelled', text: 'text-text-faint' };
        default: return { dot: 'bg-danger', label: 'Failed', text: 'text-danger' };
    }
}

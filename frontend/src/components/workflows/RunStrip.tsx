'use client';
/**
 * The open canvas's run strip (M3a): where the run stands, at the canvas's
 * bottom left. It holds what the Run Log tab under the old canvas held around
 * the log: the waiting question's Answer, Resume from failure, the run's own
 * page, and the buttons that open the log and the recent runs above it. Other
 * runs still going are a menu here; the list keeps its banner.
 *
 * Answer opens the question where it is answered now, the setup card of the
 * step that asked (M3a-2); the dialog it used to open is gone.
 */
import { ExternalLink, FileText, History, Radio, RotateCcw } from 'lucide-react';
import { Button, Menu, MenuItem, Spinner, buttonStyles } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { RunSummary } from '@/components/runs/summary';

export type RunDrawerView = 'run' | 'recent';

const FLOAT = 'rounded-[12px] border border-border bg-surface shadow-elevated';

export function RunStrip({
    runStatus, liveActivity, runId, humanPrompt, onAnswer,
    onResumeRun, drawer, onDrawer, pastRunCount, activeRuns, workflowName, onRestoreRun,
}: {
    runStatus: string;
    liveActivity: string | null;
    runId: string | null;
    humanPrompt: string | null;
    /** Take the person to the question: the waiting step's setup card. */
    onAnswer: () => void;
    onResumeRun: () => void;
    /** Which view the drawer shows, or null when it is shut. */
    drawer: RunDrawerView | null;
    onDrawer: (view: RunDrawerView | null) => void;
    pastRunCount: number;
    /** Runs still going, other than the one on screen. */
    activeRuns: RunSummary[];
    workflowName: (id: string) => string;
    onRestoreRun: (run: RunSummary) => void;
}) {
    // Paused as well as asking (F141): a cancelled run kept its prompt, and the
    // Answer it offered was refused with a 409.
    const waiting = !!humanPrompt && runStatus === 'paused';
    // The rule only separates the run's state from the two buttons: with no run
    // and nothing else going, there is nothing on its left (Naveen, M3a-1 review).
    const lead = waiting || runStatus !== 'idle' || !!runId || activeRuns.length > 0;
    const toggle = (view: RunDrawerView) => onDrawer(drawer === view ? null : view);

    return (
        <div className={cn(FLOAT, 'flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1 py-1 pl-3 pr-1.5')}>
            {waiting ? (
                <button
                    type="button"
                    onClick={onAnswer}
                    className="group -ml-1.5 flex min-w-0 flex-[1_1_18rem] items-center gap-2 overflow-hidden rounded-lg border border-person/50 bg-person-subtle py-1.5 pl-2.5 pr-1.5 text-left transition-colors hover:border-person"
                >
                    <span className="relative flex size-2 shrink-0">
                        <span className="absolute inline-flex size-full rounded-full bg-person opacity-60 motion-safe:animate-ping" aria-hidden />
                        <span className="relative inline-flex size-2 rounded-full bg-person" aria-hidden />
                    </span>
                    <span className="shrink-0 text-xs font-semibold text-person-ink">Waiting for your input</span>
                    <span className="min-w-0 flex-1 truncate text-xs text-text-muted">{humanPrompt}</span>
                    <span className="shrink-0 rounded-md bg-person px-2.5 py-1 text-xs font-medium text-person-fg">
                        Answer
                    </span>
                </button>
            ) : (
                <RunState runStatus={runStatus} liveActivity={liveActivity} />
            )}

            {(runStatus === 'failed' || runStatus === 'cancelled') && runId && (
                <Button size="sm" variant="secondary" onClick={onResumeRun}>
                    <RotateCcw className="size-3.5" aria-hidden /> Resume from failure
                </Button>
            )}
            {/* The run's own page (M2b). A plain link, so unsaved edits still get the
                browser's leave prompt (the draft's `beforeunload` guard). */}
            {runId && (
                <a
                    href={`/runs/${encodeURIComponent(runId)}`}
                    className="inline-flex items-center gap-1 rounded-sm px-1 text-xs text-accent-ink hover:underline"
                >
                    <ExternalLink className="size-3" aria-hidden /> Open run page
                </a>
            )}

            {activeRuns.length > 0 && (
                <Menu
                    side="top"
                    trigger={
                        <button type="button" className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), 'gap-1.5')}>
                            <Radio className="size-3.5 text-machine" aria-hidden /> {activeRuns.length} active
                        </button>
                    }
                >
                    {activeRuns.map((run) => (
                        <MenuItem key={run.run_id} icon={Radio} onSelect={() => onRestoreRun(run)}>
                            {workflowName(run.orchestration_id)}
                            <span className="ml-1 text-text-faint">{run.status === 'paused' ? '· waiting for input' : '· running'}</span>
                        </MenuItem>
                    ))}
                </Menu>
            )}

            {lead && <span className="mx-0.5 h-5 w-px bg-border" aria-hidden />}
            <button
                type="button"
                aria-pressed={drawer === 'run'}
                onClick={() => toggle('run')}
                className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), drawer === 'run' && 'bg-surface-2 text-text')}
            >
                <FileText className="size-3.5" aria-hidden /> Run Log
                {runStatus !== 'idle' && <span className={cn('size-2 rounded-full', DOT[runStatus] ?? 'bg-danger')} aria-hidden />}
            </button>
            {/* An icon only (Naveen, M3a-1 review); its name and tooltip keep the words and the count. */}
            <button
                type="button"
                aria-pressed={drawer === 'recent'}
                onClick={() => toggle('recent')}
                aria-label={`Recent Runs${pastRunCount > 0 ? ` (${pastRunCount})` : ''}`}
                title={`Recent Runs${pastRunCount > 0 ? ` (${pastRunCount})` : ''}`}
                className={cn(buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true }), drawer === 'recent' && 'bg-surface-2 text-text')}
            >
                <History className="size-4" aria-hidden />
            </button>

        </div>
    );
}

const DOT: Record<string, string> = {
    running: 'bg-machine motion-safe:animate-pulse',
    completed: 'bg-success',
    paused: 'bg-person',
    cancelled: 'bg-text-faint',
};

function RunState({ runStatus, liveActivity }: { runStatus: string; liveActivity: string | null }) {
    if (runStatus === 'idle') return null;
    const words: Record<string, string> = {
        running: 'Running',
        paused: 'Paused',
        completed: 'Completed',
        failed: 'Failed',
        cancelled: 'Cancelled',
    };
    return (
        // An auto basis, so a narrow strip (the setup card open beside it) wraps
        // the buttons to a second row instead of shrinking this under its words.
        <span className="flex min-w-0 flex-[1_1_auto] items-center gap-2 text-sm" role="status">
            {runStatus === 'running'
                ? <Spinner size={16} className="shrink-0 text-machine" />
                : <span className={cn('size-2 shrink-0 rounded-full', DOT[runStatus] ?? 'bg-danger')} aria-hidden />}
            <span className={cn('shrink-0 font-medium', runStatus === 'failed' ? 'text-danger' : 'text-text')}>
                {words[runStatus] ?? runStatus}
            </span>
            {/* What the model is doing right now (the Run Log's live line). */}
            {runStatus === 'running' && liveActivity && (
                <span className="min-w-0 truncate text-xs text-text-faint">{liveActivity}</span>
            )}
        </span>
    );
}

'use client';
/**
 * The open canvas's run strip (M3a): where the run stands, at the canvas's
 * bottom left. It holds what the Run Log tab under the old canvas held around
 * the log: the waiting question's Answer and its dialog, Resume from failure,
 * the run's own page, and the buttons that open the log and the recent runs
 * above it. Other runs still going are a menu here; the list keeps its banner.
 */
import { useState } from 'react';
import { ExternalLink, FileText, History, Radio, RotateCcw } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button, Markdown, Menu, MenuItem, Modal, Spinner, buttonStyles } from '@/components/ui';
import { cn } from '@/lib/cn';
import { HumanFieldsForm } from '../orchestration/HumanFieldsForm';
import type { HumanField, HumanValues } from '../orchestration/humanFields';
import type { RunSummary } from '@/components/runs/summary';

export type RunDrawerView = 'run' | 'recent';

const FLOAT = 'rounded-[12px] border border-border bg-surface shadow-elevated';

export function RunStrip({
    runStatus, liveActivity, runId,
    humanPrompt, humanContext, humanResponse, setHumanResponse, humanFields, humanValues, setHumanValues, onSubmitHuman,
    onResumeRun, drawer, onDrawer, pastRunCount, activeRuns, workflowName, onRestoreRun,
}: {
    runStatus: string;
    liveActivity: string | null;
    runId: string | null;
    humanPrompt: string | null;
    humanContext: { text: string; key: string | null } | null;
    humanResponse: string;
    setHumanResponse: (v: string) => void;
    humanFields: HumanField[];
    humanValues: HumanValues;
    setHumanValues: (v: HumanValues) => void;
    onSubmitHuman: () => void;
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
    // A fresh question closes a stale dialog: adjusted while rendering, as the
    // bottom panel did, rather than in an effect.
    const [humanModalOpen, setHumanModalOpen] = useState(false);
    const [seenPrompt, setSeenPrompt] = useState(humanPrompt);
    if (seenPrompt !== humanPrompt) {
        setSeenPrompt(humanPrompt);
        setHumanModalOpen(false);
    }

    // Paused as well as asking (F141): a cancelled run kept its prompt, and the
    // Answer it offered was refused with a 409.
    const waiting = !!humanPrompt && runStatus === 'paused';
    const toggle = (view: RunDrawerView) => onDrawer(drawer === view ? null : view);

    return (
        <div className={cn(FLOAT, 'flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1 py-1 pl-3 pr-1.5')}>
            {waiting ? (
                <button
                    type="button"
                    onClick={() => setHumanModalOpen(true)}
                    className="group -ml-1.5 flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-person/50 bg-person-subtle py-1.5 pl-2.5 pr-1.5 text-left transition-colors hover:border-person"
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

            <span className="mx-0.5 h-5 w-px bg-border" aria-hidden />
            <button
                type="button"
                aria-pressed={drawer === 'run'}
                onClick={() => toggle('run')}
                className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), drawer === 'run' && 'bg-surface-2 text-text')}
            >
                <FileText className="size-3.5" aria-hidden /> Run Log
                {runStatus !== 'idle' && <span className={cn('size-2 rounded-full', DOT[runStatus] ?? 'bg-danger')} aria-hidden />}
            </button>
            <button
                type="button"
                aria-pressed={drawer === 'recent'}
                onClick={() => toggle('recent')}
                className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), drawer === 'recent' && 'bg-surface-2 text-text')}
            >
                <History className="size-3.5" aria-hidden /> Recent Runs
                {pastRunCount > 0 && (
                    <span className="rounded-md bg-surface-2 px-1.5 py-0.5 text-2xs text-text-muted">{pastRunCount}</span>
                )}
            </button>

            <Modal
                open={waiting && humanModalOpen}
                onClose={() => setHumanModalOpen(false)}
                title="Human input required"
                size="lg"
                footer={
                    <>
                        <Button variant="secondary" onClick={() => setHumanModalOpen(false)}>Later</Button>
                        <Button onClick={onSubmitHuman} disabled={!humanFields.length && !humanResponse.trim()}>Submit</Button>
                    </>
                }
            >
                <div className="space-y-3">
                    {humanContext && (
                        // Labelled with the state key it came from: a bare
                        // "pass" under the title said nothing (U65).
                        <figure>
                            <figcaption className="mb-1.5 text-xs text-text-muted">
                                {humanContext.key
                                    ? <>From <span className="font-code text-text">{humanContext.key}</span></>
                                    : 'From this run'}
                            </figcaption>
                            <div className="max-h-[45vh] overflow-y-auto rounded-md border border-border bg-surface-2 p-4">
                                <Markdown>{humanContext.text}</Markdown>
                            </div>
                        </figure>
                    )}
                    <div className="text-sm leading-relaxed text-amber-300">
                        <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                            components={{
                                p: ({ children }) => <p className="mb-0">{children}</p>,
                                a: ({ href, children }) => <a href={href} className="text-amber-200 underline" target="_blank" rel="noreferrer">{children}</a>,
                                strong: ({ children }) => <strong className="font-semibold text-amber-200">{children}</strong>,
                            }}
                        >{humanPrompt ?? ''}</ReactMarkdown>
                    </div>
                    {humanFields.length > 0 ? (
                        <HumanFieldsForm fields={humanFields} values={humanValues} onChange={setHumanValues} idPrefix="orch-answer" />
                    ) : (
                        <input
                            autoFocus
                            aria-label="Your answer"
                            className="w-full rounded-md border border-amber-700/40 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-500 focus:border-amber-500"
                            value={humanResponse}
                            onChange={(e) => setHumanResponse(e.target.value)}
                            placeholder="Your response…"
                            onKeyDown={(e) => { if (e.key === 'Enter') onSubmitHuman(); }}
                        />
                    )}
                </div>
            </Modal>
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
        <span className="flex min-w-0 flex-1 items-center gap-2 text-sm" role="status">
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

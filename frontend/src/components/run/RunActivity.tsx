'use client';

/*
 * What the run did, in the builder's own vocabulary: the run log (M2b).
 *
 * The timeline above answers "which steps ran, when, and for how long". It
 * cannot answer "what is it doing right now", because a step's history entry
 * is written when the step ends — so a run whose one agent step has been
 * working for four minutes has an empty history and a very full event stream.
 * This is that stream, folded through `RunLogView`'s reducer and drawn with its
 * renderer, the same as cloud's builder panel.
 *
 * Moved upstream from cloud's run page (`RunActivity.tsx`) with the run screens.
 * Where the events come from is the product's (`RunHost.events`): OSS replays
 * its journal and follows it live; cloud follows a live run's stream and reads
 * a finished one's archive. Loaded on mount, not behind a click — someone
 * opening a running job wants to see how it is going.
 */
import * as RCollapsible from '@radix-ui/react-collapsible';
import { ArrowDown, ChevronRight } from 'lucide-react';
import * as React from 'react';

import { Button, ErrorNote, Markdown, Modal, Spinner } from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';

import { TeamView } from '@/components/team';

import { useRunHost } from './host';
import { isLive } from './model';
import { applyRunEvent, emptyRunLog, LogLine, type RunLogState, type StepResultEntry } from './RunLogView';

/** A `step_error` with the step's own words from the history (F151, F195): the stream's `error` is sanitised. */
function withStepError(event: unknown, errors: Record<string, string>): unknown {
    const e = event as { type?: string; orch_step_id?: string; reason?: string };
    if (e?.type !== 'step_error' || !e.orch_step_id || e.reason) return event;
    const real = errors[e.orch_step_id];
    return real ? { ...(event as object), error: real } : event;
}

export function RunActivity({
    runId,
    status,
    stepErrors = {},
    emptyNote = 'No events were recorded for this run.',
}: {
    runId: string;
    status: string;
    /** Each failed step's own error, by step id, from the history. */
    stepErrors?: Record<string, string>;
    /** Said when there are no events (cloud: the live events expire, an archive may be missing). */
    emptyNote?: string;
}) {
    const host = useRunHost();
    const [open, setOpen] = React.useState(true);
    const [state, setState] = React.useState<RunLogState>(emptyRunLog);
    const [loading, setLoading] = React.useState(true);
    const [error, setError] = React.useState('');
    const [responseModal, setResponseModal] = React.useState<StepResultEntry | null>(null);
    const [pinned, setPinned] = React.useState(true);
    // The stream closing is the only signal here of how the run really went; the
    // status prop is the page's snapshot.
    const [ended, setEnded] = React.useState(false);
    const boxRef = React.useRef<HTMLDivElement | null>(null);
    const following = isLive(status) && !ended;

    // A ref, so a new object from a re-render does not restart the stream.
    const errorsRef = React.useRef(stepErrors);
    React.useEffect(() => { errorsRef.current = stepErrors; }, [stepErrors]);

    React.useEffect(() => {
        const controller = new AbortController();
        let cancelled = false;
        const fold = (event: unknown) => {
            if (!cancelled) setState((prev) => applyRunEvent(prev, withStepError(event, errorsRef.current)));
        };
        (async () => {
            try {
                await host.events(runId, isLive(status), fold, controller.signal);
                if (!cancelled) setEnded(true);
            } catch (e) {
                if (!cancelled && (e as Error)?.name !== 'AbortError') {
                    setError(e instanceof Error ? e.message : 'The log could not be read.');
                }
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => { cancelled = true; controller.abort(); };
    }, [host, runId, status]);

    // Follow the newest entry only while the reader is at the bottom.
    React.useEffect(() => {
        const el = boxRef.current;
        if (el && pinned) el.scrollTop = el.scrollHeight;
    }, [state.entries, pinned]);

    const onScroll = () => {
        const el = boxRef.current;
        if (el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
    };

    const summary = following
        ? state.liveActivity ?? 'Following live'
        : `${state.entries.length} ${state.entries.length === 1 ? 'event' : 'events'}`;

    let content: React.ReactNode;
    if (loading && state.entries.length === 0) {
        content = (
            <p className="flex items-center gap-2 text-sm text-text-muted">
                <Spinner size={14} />
                Reading the log…
            </p>
        );
    } else if (error && state.entries.length === 0) {
        content = <ErrorNote>{error}</ErrorNote>;
    } else if (state.entries.length === 0) {
        content = <p className="text-sm text-text-muted">{emptyNote}</p>;
    } else {
        content = (
            <div className="relative">
                <div
                    ref={boxRef}
                    onScroll={onScroll}
                    className="max-h-[32rem] space-y-0.5 overflow-y-auto rounded-md border border-border bg-bg px-4 py-3 font-code text-[11px] leading-relaxed text-text-muted"
                >
                    {state.entries.map((entry, i) => (
                        <LogLine key={i} entry={entry} onOpen={setResponseModal} />
                    ))}
                </div>
                <ErrorNote>{error}</ErrorNote>
                {!pinned && (
                    <button
                        type="button"
                        onClick={() => setPinned(true)}
                        className="absolute bottom-3 right-3 z-20 inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2.5 py-1 text-[11px] text-text-muted shadow-elevated transition-colors hover:text-text"
                    >
                        <ArrowDown size={11} aria-hidden /> Jump to latest
                    </button>
                )}
            </div>
        );
    }

    return (
        <>
            <RCollapsible.Root open={open} onOpenChange={setOpen} className="grid gap-2.5" id="activity">
                <RCollapsible.Trigger
                    className={cn('-mx-2 flex items-center gap-2 rounded-md px-2 py-1 text-left transition-colors hover:bg-surface-2', focusRing)}
                >
                    <ChevronRight className={cn('size-4 shrink-0 text-text-faint transition-transform', open && 'rotate-90')} aria-hidden />
                    <span className="text-section text-text">Activity</span>
                    <span className="ml-auto flex min-w-0 items-center gap-1.5 text-xs text-text-faint">
                        {following && <Spinner size={12} className="shrink-0 text-machine" />}
                        <span className="truncate" translate="no">{summary}</span>
                    </span>
                </RCollapsible.Trigger>
                <RCollapsible.Content>
                    {/* A Team agent step's task list and members' lanes (TEAM1b; fed once a step runs a team, TEAM1e). */}
                    <TeamView state={state.team} kept={!following} className="mb-3" />
                    {content}
                </RCollapsible.Content>
            </RCollapsible.Root>

            <Modal
                open={responseModal !== null}
                onClose={() => setResponseModal(null)}
                title={responseModal ? `${responseModal.step_name} — full response` : ''}
                size="lg"
                footer={
                    <Button
                        variant="secondary"
                        onClick={() => { if (responseModal) navigator.clipboard.writeText(responseModal.content); }}
                    >
                        Copy
                    </Button>
                }
            >
                {responseModal && (
                    <div className="max-h-[60vh] overflow-y-auto">
                        <Markdown>{responseModal.content}</Markdown>
                    </div>
                )}
            </Modal>
        </>
    );
}

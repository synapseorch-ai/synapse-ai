'use client';
/*
 * OSS's run page, `/runs/<id>` (M2b). Until now a run's detail lived only in the
 * workflow editor's bottom panel and the log viewer.
 *
 * The page is the shared `RunDetail` (components/run), fed from this API: the
 * run's record, its own copy of its workflow (F443) and the workflow as saved
 * today, the runs its steps started, and the schedules (to name what started it). While the run is live its record is
 * read again — every 3 s running, every 10 s waiting on a person — so the facts
 * and the path move with it, and an answer given elsewhere shows; the log follows
 * the journal on its own stream. The editor's live view stays where it was, and
 * "Open in editor" goes there.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { useHelpTopic } from '@/components/help';
import { isLive, RunDetail, RunHostProvider, type NestedRun, type RunRecord } from '@/components/run';
import { PageFrame, Section, Spinner, TextLink } from '@/components/ui';
import { OSS_RUN_HOST, ossRunRecord } from '@/lib/runHost';
import type { Orchestration } from '@/types/orchestration';

/** How often a live run's record is read again: a running one moves by the second, a waiting one by the person. */
const POLL_MS = { running: 3000, waiting: 10_000 };

type Loaded =
    | { state: 'loading' }
    | { state: 'missing' }
    | { state: 'error'; message: string }
    | { state: 'ok'; run: RunRecord; workflow: Orchestration | null };

export function RunPage({ runId }: { runId: string }) {
    useHelpTopic('runs.detail');
    const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' });
    const [nested, setNested] = useState<NestedRun[]>([]);
    const schedulesRef = useRef<{ id: string; name: string }[] | null>(null);
    const workflowRef = useRef<{ id: string; value: Orchestration | null } | null>(null);
    const statusRef = useRef<string | null>(null);
    // The run's own copy of its workflow (F443): a whole definition, so read with
    // the record once, and again on the read after its status moved — a Replay
    // takes the workflow as saved and makes that the copy. Polls go without it.
    const copyRef = useRef<{ status: string | null; value: Orchestration | null } | null>(null);
    const lastStatusRef = useRef<string | null>(null);

    const read = useCallback(async () => {
        try {
            const withCopy = copyRef.current === null || copyRef.current.status !== lastStatusRef.current;
            const res = await fetch(`/api/orchestrations/runs/${encodeURIComponent(runId)}${withCopy ? '?definition=1' : ''}`);
            if (res.status === 404) { setLoaded({ state: 'missing' }); return; }
            if (!res.ok) throw new Error(`The run could not be read (HTTP ${res.status}).`);
            const data = await res.json();
            lastStatusRef.current = data.status ?? null;
            if (withCopy) {
                const copy = data.definition && Array.isArray(data.definition.steps) ? (data.definition as Orchestration) : null;
                copyRef.current = { status: lastStatusRef.current, value: copy };
            }
            if (schedulesRef.current === null) {
                schedulesRef.current = await fetch('/api/schedules')
                    .then((r) => (r.ok ? r.json() : []))
                    .then((d) => (Array.isArray(d) ? d : []))
                    .catch(() => []);
            }
            const orchId = String(data.orchestration_id ?? '');
            if (workflowRef.current?.id !== orchId) {
                const wf = orchId
                    ? await fetch(`/api/orchestrations/${encodeURIComponent(orchId)}`).then((r) => (r.ok ? r.json() : null)).catch(() => null)
                    : null;
                workflowRef.current = { id: orchId, value: wf && wf.id ? (wf as Orchestration) : null };
            }
            const run = { ...ossRunRecord(data, workflowRef.current.value, schedulesRef.current ?? []), definition: copyRef.current?.value ?? null };
            // The runs its steps started: read again whenever the run moves on.
            if (statusRef.current !== `${run.status}:${run.history.length}`) {
                statusRef.current = `${run.status}:${run.history.length}`;
                OSS_RUN_HOST.children(runId).then(setNested).catch(() => {});
            }
            setLoaded({ state: 'ok', run, workflow: workflowRef.current.value });
        } catch (e) {
            setLoaded((cur) => (cur.state === 'ok' ? cur : { state: 'error', message: e instanceof Error ? e.message : 'The run could not be read.' }));
        }
    }, [runId]);

    useEffect(() => { void read(); }, [read]);

    const live = loaded.state === 'ok' && isLive(loaded.run.status);
    const every = loaded.state === 'ok' && loaded.run.status === 'paused' ? POLL_MS.waiting : POLL_MS.running;
    useEffect(() => {
        if (!live) return;
        const timer = window.setInterval(() => { void read(); }, every);
        return () => window.clearInterval(timer);
    }, [live, every, read]);

    if (loaded.state !== 'ok') {
        return (
            <PageFrame
                breadcrumb={<TextLink href="/runs" variant="quiet">Runs</TextLink>}
                title={loaded.state === 'missing' ? 'Run not found' : 'Run'}
                description={<span className="font-code text-text-faint">{runId}</span>}
                width="wide"
            >
                {loaded.state === 'loading' && (
                    <p className="flex items-center gap-2 text-sm text-text-muted"><Spinner size={14} /> Reading the run…</p>
                )}
                {loaded.state === 'missing' && (
                    <p className="text-sm text-text-muted">There is no run with this id. It may have been deleted with its log.</p>
                )}
                {loaded.state === 'error' && <p className="text-sm text-danger">{loaded.message}</p>}
            </PageFrame>
        );
    }

    return (
        <RunHostProvider value={OSS_RUN_HOST}>
            <RunDetail
                run={loaded.run}
                workflow={loaded.workflow}
                nested={nested}
                onChanged={() => { void read(); }}
                activityNote="No events were recorded for this run. A run from before the event journal has none."
                extra={<FullLog runId={runId} />}
            />
        </RunHostProvider>
    );
}

/** The run's plain-text log, as Logs shows it — what the model was sent and said at every turn. */
function FullLog({ runId }: { runId: string }) {
    return (
        <Section
            title="Full log"
            description="The run's log file, as Logs shows it: every step, prompt and answer in full."
            summary="Open to load"
        >
            <LogText runId={runId} />
        </Section>
    );
}

/** Mounted when the section opens, so the file is read only then, once. */
function LogText({ runId }: { runId: string }) {
    const [text, setText] = useState<string | null>(null);
    const [note, setNote] = useState('');
    useEffect(() => {
        let live = true;
        fetch(`/api/orchestrations/logs/${encodeURIComponent(runId)}`)
            .then(async (r) => {
                if (!live) return;
                if (r.status === 404) { setNote('There is no log file for this run.'); return; }
                if (!r.ok) { setNote(`The log could not be read (HTTP ${r.status}).`); return; }
                setText(await r.text());
            })
            .catch(() => { if (live) setNote('The log could not be read.'); });
        return () => { live = false; };
    }, [runId]);
    if (note) return <p className="text-sm text-text-muted">{note}</p>;
    if (text === null) return <p className="flex items-center gap-2 text-sm text-text-muted"><Spinner size={14} /> Reading the log…</p>;
    return (
        <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg px-4 py-3 font-code text-xs leading-relaxed text-text-muted">
            {text}
        </pre>
    );
}

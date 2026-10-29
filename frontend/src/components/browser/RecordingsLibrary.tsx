'use client';
/*
 * The Recordings list (BRW2, F1142; DESIGN v5-4, v5-9, v5.2): each task done once in the browser while Synapse
 * recorded it, for agents and workflows to repeat. A row says its name, its sites, how many steps, when it last
 * changed and that a fix is waiting, and opens its own page (Naveen 10-29). "Record a task" picks the sessions it
 * starts with and where it starts; the browser is the person's alone, and Stop keeps it and opens it. Synced to cloud:
 * the routes are the host's, and a plan without recordings says where they come with (`locked`).
 */
import { useEffect, useState } from 'react';
import { Film } from 'lucide-react';

import { useHelpTopic } from '@/components/help';
import { Button, EmptyState, ErrorNote, Field, Input, Modal } from '@/components/ui';

import { BrowserPane } from './BrowserPane';
import type { BrowserHost } from './model';
import { listRecordings, openRecordBrowser, stepCount, stopRecording, type RecordBrowser, type RecordingSummary,
         type RecordingsHost } from './recordings';
import { SessionPicker } from './SessionPicker';
import { lastUsed, listSessions, type SessionsHost, type SiteSession } from './sessions';

export function RecordingsLibrary({ host, sessionsHost, browserHost, onOpen }: {
  host: RecordingsHost;
  /** The sessions a recording browser can start with. */
  sessionsHost: SessionsHost;
  browserHost: BrowserHost;
  /** Open a recording's own page. */
  onOpen: (id: string) => void;
}) {
  useHelpTopic('browser.recordings');
  const [rows, setRows] = useState<RecordingSummary[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  // Set only when it answers, never after the list has gone (react-hooks' set-state-in-effect).
  useEffect(() => {
    if (host.locked) return;
    let live = true;
    listRecordings(host).then(
      found => { if (live) { setRows(found); setFailed(null); } },
      e => { if (live) { setFailed(`The recordings could not be read (${e instanceof Error ? e.message : String(e)}).`); setRows([]); } },
    );
    return () => { live = false; };
  }, [host]);

  if (host.locked) {
    return (
      <EmptyState title="Recordings come with Solo and up.">
        Do a task once in the browser while {host.product} records it, and your agents and workflows can repeat it.{' '}
        <a href={host.locked.upgradeHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">See plans</a>
      </EmptyState>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="min-w-0 max-w-prose grow basis-64 text-sm text-text-muted">
          Do a task once in the browser while {host.product} records it, and your agents and workflows can repeat it. Each
          thing you do is a step with its picture. Passwords, card details and codes are never kept: those steps
          stay yours to do.
        </p>
        <RecordTask host={host} sessionsHost={sessionsHost} browserHost={browserHost} onKept={onOpen} />
      </div>
      {failed && <ErrorNote>{failed}</ErrorNote>}
      {rows === null ? (
        <p className="py-4 text-sm text-text-faint">Loading…</p>
      ) : rows.length === 0 ? (
        <EmptyState title="No recordings yet.">
          Do a task once in the browser while {host.product} records it, and your agents and workflows can repeat it.
        </EmptyState>
      ) : (
        <ul aria-label="Recordings" className="divide-y divide-border rounded-md border border-border">
          {rows.map(r => (
            <li key={r.id} className="flex flex-wrap items-center gap-3 px-4 py-3 hover:bg-row-hover">
              <Film className="size-4 shrink-0 text-text-faint" aria-hidden />
              <div className="min-w-0 grow basis-56">
                <a href={host.recordingHref(r.id)} className="text-sm font-medium text-text underline-offset-2 hover:underline">
                  {r.name}
                </a>
                <div className="mt-0.5 text-xs text-text-faint">
                  {[r.sites.join(', '), stepCount(r.steps), changed(r.updated_at ?? r.created_at)].filter(Boolean).join(' · ')}
                </div>
              </div>
              {r.pending_edits > 0 && (
                <span className="ml-auto shrink-0 rounded-full bg-person-subtle px-2 py-0.5 text-xs font-medium text-text">
                  {r.pending_edits === 1 ? 'A fix is waiting' : `${r.pending_edits} fixes are waiting`}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** "Changed 2 hours ago", from the sessions' own wording. */
function changed(at: number | null): string {
  return at ? lastUsed(at).replace(/^Last used/, 'Changed') : '';
}

/**
 * "Record a task": the sessions it starts with and where it starts, then a browser for the person alone. Stop and
 * keep makes the recording; Discard drops it. From a workflow's Browser step (F1146) it starts with the step's
 * sessions, and a kept one is that workflow's, deleted with it unless something else uses it.
 */
export function RecordTask({ host, sessionsHost, browserHost, onKept, label = 'Record a task', initialSessions,
                             workflowId }: {
  host: RecordingsHost;
  sessionsHost: SessionsHost;
  browserHost: BrowserHost;
  onKept: (id: string) => void;
  label?: string;
  initialSessions?: string[];
  /** The workflow the recording becomes part of; a workflow never saved has none, and its recording is the library's. */
  workflowId?: string;
}) {
  const [asking, setAsking] = useState(false);
  const [sessions, setSessions] = useState<SiteSession[] | null>(null);
  const [picked, setPicked] = useState<string[]>(initialSessions ?? []);
  const [start, setStart] = useState('');
  const [opening, setOpening] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [open, setOpen] = useState<RecordBrowser | null>(null);
  const [stopping, setStopping] = useState(false);
  const [stopFailed, setStopFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!asking || sessions !== null || sessionsHost.locked) return;
    listSessions(sessionsHost).then(setSessions, () => setSessions([]));
  }, [asking, sessions, sessionsHost]);

  const begin = async () => {
    setOpening(true);
    setRefused(null);
    const body: Record<string, unknown> = { sessions: picked };
    if (start.trim()) body.start_url = start.trim();
    const done = await openRecordBrowser(host, body);
    setOpening(false);
    if ('refused' in done) { setRefused(done.refused); return; }
    setAsking(false);
    setStopFailed(null);
    setOpen(done.opened);
  };

  const stop = async (keep: boolean) => {
    if (!open) return;
    setStopping(true);
    const done = await stopRecording(host, open.browserSessionId, { keep, ...(keep && workflowId ? { workflow_id: workflowId } : {}) });
    setStopping(false);
    if ('refused' in done) { setStopFailed(done.refused); return; }
    setOpen(null);
    if ('recordingId' in done && done.recordingId) onKept(done.recordingId);
  };

  return (
    <>
      <Button size="sm" onClick={() => { setAsking(true); setRefused(null); }}>{label}</Button>
      <Modal
        open={asking}
        onClose={() => setAsking(false)}
        title="Record a task"
        description="A browser opens here for you alone. Do the task once as you normally would; each thing you do is a step. Stop when it's done."
        footer={(
          <>
            <Button variant="secondary" onClick={() => setAsking(false)}>Cancel</Button>
            <Button onClick={begin} loading={opening}>Open the browser</Button>
          </>
        )}
      >
        <div className="grid gap-4">
          <SessionPicker
            label="Sessions it starts with"
            sessions={sessions}
            value={picked}
            onChange={setPicked}
            libraryHref={sessionsHost.libraryHref}
            locked={sessionsHost.locked}
            hint="Signed in to these sites from the start. One per site."
          />
          <Field label="Start at" hint="The page it starts on, such as portal.saltmarsh.example/orders. Blank: a new tab."
                 error={refused ?? undefined}>
            <Input value={start} onChange={e => setStart(e.target.value)} spellCheck={false} translate="no"
                   placeholder="portal.saltmarsh.example/orders"
                   onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); begin(); } }} />
          </Field>
        </div>
      </Modal>
      {open && (
        <div role="dialog" aria-modal="true" aria-label="Record a task" className="fixed inset-0 z-50 bg-scrim/60 p-3 md:p-8">
          <BrowserPane
            host={browserHost}
            sessionId={open.browserSessionId}
            agentName="You"
            actions={[]}
            record={{ onStop: stop, busy: stopping, failed: stopFailed }}
            onClosed={() => setOpen(null)}
            className="h-full overflow-hidden rounded-xl border border-border shadow-elevated"
          />
        </div>
      )}
    </>
  );
}

'use client';
/*
 * A recording's own page (BRW2, F1142; DESIGN v5-6, v5.2; Naveen 10-29: its own page). Inputs and outputs above the
 * steps; each step with its picture and its sentence (a rename shown instead). A typed value is an input or fixed;
 * the person's own sign-in, card or code reads as theirs and has nothing to change. Rename, Details, Delete and
 * Re-record from here on a step. Each change is a PATCH from the version the page has, so a change made elsewhere
 * meanwhile is said, with Reload, rather than overwritten. Versions are listed; Delete recording asks first.
 * Synced to cloud: routes are the host's, and the page's frame is the product's (`frame`).
 */
import { useCallback, useEffect, useState } from 'react';
import { MoreHorizontal, RotateCcw, Trash2 } from 'lucide-react';

import { Button, buttonStyles, ConfirmDialog, ErrorNote, Input, Menu, MenuItem, Modal } from '@/components/ui';

import { BrowserPane } from './BrowserPane';
import type { BrowserHost } from './model';
import {
  carryOn, changeRecording, deleteRecording, fieldOf, getRecording, inputName, listVersions, openRecordBrowser,
  outputsOf, proposedInputs, stepCount, stepWords, stopRecording,
  type Change, type RecordBrowser, type Recording, type RecordingStep, type RecordingVersion, type RecordingsHost,
  type ReplayStop,
} from './recordings';
import { SessionPicker } from './SessionPicker';
import { lastUsed, listSessions, type SessionsHost, type SiteSession } from './sessions';

export type EditorFrame = (parts: { title: React.ReactNode; description?: React.ReactNode; actions?: React.ReactNode;
                                   back: { href: string; label: string }; children: React.ReactNode }) => React.ReactNode;

const PERSON_NOTE: Record<string, string> = {
  password: 'Yours to do: a password is never kept.',
  card: 'Yours to do: card details are never kept.',
  code: 'Yours to do: a code is never kept.',
};

export function RecordingEditor({ host, sessionsHost, browserHost, id, frame, onDeleted }: {
  host: RecordingsHost;
  sessionsHost: SessionsHost;
  browserHost: BrowserHost;
  id: string;
  frame: EditorFrame;
  /** Gone: back to the list. */
  onDeleted: () => void;
}) {
  const [doc, setDoc] = useState<Recording | null | undefined>(undefined);
  const [readFailed, setReadFailed] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<{ words: string; stale: boolean } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [fixedChosen, setFixedChosen] = useState(false);
  const [versions, setVersions] = useState<RecordingVersion[] | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [deleting, setDeleting] = useState<RecordingStep | null>(null);
  const [deletingAll, setDeletingAll] = useState(false);
  const [redoFrom, setRedoFrom] = useState<RecordingStep | null>(null);

  const load = useCallback(async () => {
    try {
      setDoc(await getRecording(host, id));
      setReadFailed(null);
      setProblem(null);
    } catch (e) {
      setReadFailed(`The recording could not be read (${e instanceof Error ? e.message : String(e)}).`);
      setDoc(prev => prev ?? null);
    }
  }, [host, id]);

  // Set only when it answers, never after the page has gone (react-hooks' set-state-in-effect); Reload is `load`.
  useEffect(() => {
    let live = true;
    getRecording(host, id).then(
      found => { if (live) setDoc(found); },
      e => { if (live) { setReadFailed(`The recording could not be read (${e instanceof Error ? e.message : String(e)}).`); setDoc(null); } },
    );
    return () => { live = false; };
  }, [host, id]);

  const save = async (change: Change): Promise<boolean> => {
    if (!doc) return false;
    setSaving(true);
    const done = await changeRecording(host, doc, change);
    setSaving(false);
    if ('doc' in done) {
      setDoc(done.doc);
      setProblem(null);
      return true;
    }
    setProblem('stale' in done ? { words: done.stale, stale: true } : { words: done.failed, stale: false });
    return false;
  };

  const back = { href: host.libraryHref, label: 'Recordings' };
  if (doc === undefined) return frame({ title: 'Recording', back, children: <p className="py-4 text-sm text-text-faint">Loading…</p> });
  if (doc === null) {
    return frame({
      title: 'Recording', back,
      children: readFailed ? <ErrorNote>{readFailed}</ErrorNote> : <p className="py-4 text-sm text-text-muted">That recording is gone.</p>,
    });
  }

  const proposals = doc.inputs.length || fixedChosen ? [] : proposedInputs(doc);
  const outputs = outputsOf(doc);
  const replace = (step: RecordingStep, next: Partial<RecordingStep>) => doc.steps.map(s => (s.id === step.id ? { ...s, ...next } : s));

  const makeInputs = () => {
    const inputs = [...doc.inputs, ...proposals.map(p => p.input)];
    const named = new Map(proposals.map(p => [p.step.id, p.input.name]));
    void save({ steps: doc.steps.map(s => (named.has(s.id) ? { ...s, value: { input: named.get(s.id)! } } : s)), inputs });
  };

  const toInput = (step: RecordingStep) => {
    const label = fieldOf(step);
    const name = inputName(label, doc.inputs.map(i => i.name));
    void save({ steps: replace(step, { value: { input: name } }), inputs: [...doc.inputs, { name, label, step_id: step.id }] });
  };

  const toFixed = (step: RecordingStep, fixed: string) => {
    void save({ steps: replace(step, { value: { fixed } }), inputs: doc.inputs.filter(i => i.step_id !== step.id) });
  };

  const removeStep = async (step: RecordingStep) => {
    await save({
      steps: doc.steps.filter(s => s.id !== step.id),
      inputs: doc.inputs.filter(i => i.step_id !== step.id),
      outputs: doc.outputs.filter(o => o.step_id !== step.id),
    });
    setDeleting(null);
  };

  const openVersions = async () => {
    setShowVersions(true);
    setVersions(null);
    setVersions(await listVersions(host, doc.id).catch(() => []));
  };

  const removeAll = async () => {
    if (await deleteRecording(host, doc.id)) onDeleted();
    else setProblem({ words: 'The recording could not be deleted. Try again.', stale: false });
    setDeletingAll(false);
  };

  const actions = (
    <>
      <Button variant="secondary" size="sm" onClick={openVersions}>Versions</Button>
      <Button variant="ghost" size="sm" className="text-text-muted hover:text-danger" onClick={() => setDeletingAll(true)}>Delete recording</Button>
    </>
  );
  const description = [doc.sites.join(', '), stepCount(doc.steps.length), `Version ${doc.version}`,
                       doc.updated_at ? lastUsed(doc.updated_at).replace(/^Last used/, 'changed') : ''].filter(Boolean).join(' · ');

  return frame({
    title: doc.name, description, actions, back,
    children: (
      <div className="space-y-6">
        {problem && (
          <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-text">
            <span className="min-w-0 grow basis-64">{problem.words}</span>
            {problem.stale && <Button size="sm" variant="secondary" onClick={load}>Reload</Button>}
          </div>
        )}
        {notice && <p role="status" className="rounded-md bg-surface-2 px-3 py-2 text-sm text-text">{notice}</p>}

        {proposals.length > 0 && (
          <section aria-label="Make typed values inputs" className="space-y-2 rounded-md border border-border bg-surface px-4 py-3">
            <h2 className="text-sm font-medium text-text">Make typed values inputs?</h2>
            <p className="text-sm text-text-muted">
              An input is asked for each time the recording runs; a fixed value is typed as recorded.
            </p>
            <ul className="space-y-1 text-sm text-text">
              {proposals.map(p => (
                <li key={p.step.id}>
                  <span className="font-medium">{p.input.label}</span>
                  <span className="text-text-muted"> · typed “{p.step.value && 'fixed' in p.step.value ? p.step.value.fixed : ''}”</span>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={makeInputs} loading={saving}>Make them inputs</Button>
              <Button size="sm" variant="secondary" onClick={() => setFixedChosen(true)}>Keep them fixed</Button>
            </div>
          </section>
        )}

        <div className="grid gap-4 md:grid-cols-2">
          <section aria-label="Inputs" className="min-w-0 rounded-md border border-border px-4 py-3">
            <h2 className="text-sm font-medium text-text">Inputs</h2>
            {doc.inputs.length ? (
              <ul className="mt-1 space-y-0.5 text-sm text-text">
                {doc.inputs.map(i => <li key={i.name}>{i.label} <span className="text-text-faint" translate="no">· {'{'}{i.name}{'}'}</span></li>)}
              </ul>
            ) : <p className="mt-1 text-sm text-text-muted">None: every value is typed as recorded.</p>}
          </section>
          <section aria-label="Outputs" className="min-w-0 rounded-md border border-border px-4 py-3">
            <h2 className="text-sm font-medium text-text">Outputs</h2>
            {outputs.length ? (
              <ul className="mt-1 space-y-0.5 text-sm text-text">
                {outputs.map(o => <li key={o.name} translate="no">{o.name}</li>)}
              </ul>
            ) : <p className="mt-1 text-sm text-text-muted">None: it reads nothing back.</p>}
          </section>
        </div>

        <ol aria-label="Steps" className="divide-y divide-border rounded-md border border-border">
          {doc.steps.map(step => (
            <StepRow key={step.id} host={host} recordingId={doc.id} step={step} busy={saving}
                     onRename={title => save({ steps: replace(step, { title: title || undefined }) })}
                     onInput={() => toInput(step)} onFixed={value => toFixed(step, value)}
                     onDelete={() => setDeleting(step)} onRedo={() => setRedoFrom(step)} />
          ))}
        </ol>

        <Modal open={showVersions} onClose={() => setShowVersions(false)} title="Versions"
               description="Each saved change is a version.">
          {versions === null ? (
            <p className="text-sm text-text-faint">Loading…</p>
          ) : (
            <ol aria-label="Versions" className="divide-y divide-border text-sm">
              {versions.map(v => (
                <li key={v.version} className="flex flex-wrap gap-x-2 py-2">
                  <span className="font-medium text-text">Version {v.version}</span>
                  <span className="text-text-muted">
                    {[v.at ? lastUsed(v.at).replace(/^Last used /, '') : '', v.by ?? '', stepCount(v.steps)].filter(Boolean).join(' · ')}
                  </span>
                  {v.current && <span className="text-text-faint">(this one)</span>}
                </li>
              ))}
            </ol>
          )}
        </Modal>

        <ConfirmDialog
          open={deleting !== null}
          onClose={() => setDeleting(null)}
          onConfirm={() => deleting && removeStep(deleting)}
          busy={saving}
          title="Delete this step?"
          description={deleting && <>“{stepWords(deleting)}” is left out when it runs. The version before stays in Versions.</>}
          confirmLabel="Delete"
        />
        <ConfirmDialog
          open={deletingAll}
          onClose={() => setDeletingAll(false)}
          onConfirm={removeAll}
          title="Delete this recording?"
          description={<>“{doc.name}”, its steps and their pictures are deleted at once. Agents and workflows that repeat it can’t any more.</>}
          confirmLabel="Delete"
        />
        {redoFrom && (
          <Redo host={host} sessionsHost={sessionsHost} browserHost={browserHost} doc={doc} from={redoFrom}
                onClose={() => setRedoFrom(null)}
                onKept={words => { setRedoFrom(null); setNotice(words); void load(); }}
                onPickAgain={step => setRedoFrom(step)} />
        )}
      </div>
    ),
  });
}

function StepRow({ host, recordingId, step, busy, onRename, onInput, onFixed, onDelete, onRedo }: {
  host: RecordingsHost;
  recordingId: string;
  step: RecordingStep;
  busy: boolean;
  onRename: (title: string) => Promise<boolean>;
  onInput: () => void;
  onFixed: (value: string) => void;
  onDelete: () => void;
  onRedo: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState('');
  const [fixing, setFixing] = useState(false);
  const [fixed, setFixed] = useState('');
  const [details, setDetails] = useState(false);
  const [pictureGone, setPictureGone] = useState(false);
  const value = step.value ?? null;
  const person = value && 'person' in value ? value.person : null;
  const typed = step.action === 'type' && !person;

  // Kept or not, the field closes: a refused rename is said above the steps, with Reload when it was stale.
  const rename = async () => {
    await onRename(title.trim());
    setRenaming(false);
  };

  return (
    <li className="flex flex-wrap gap-3 px-4 py-3">
      {step.after_shot && !pictureGone ? (
        // A picture this person may not see (cloud: its maker's and Builders', 403) leaves the step's line alone.
        <img src={host.shotUrl(recordingId, step.after_shot)} alt={`After step ${step.n}`} loading="lazy"
             onError={() => setPictureGone(true)}
             className="h-20 w-32 shrink-0 rounded border border-border bg-surface-2 object-cover object-top" />
      ) : (
        <div aria-hidden className="h-20 w-32 shrink-0 rounded border border-dashed border-border bg-surface-2" />
      )}
      <div className="min-w-0 grow basis-60 space-y-1.5">
        <div className="flex items-baseline gap-2">
          <span className="text-xs tabular-nums text-text-faint">{step.n}</span>
          {renaming ? (
            <Input size="sm" autoFocus value={title} aria-label="Step name" className="min-w-0 flex-1"
                   onChange={e => setTitle(e.target.value)}
                   onKeyDown={e => {
                     if (e.key === 'Enter') { e.preventDefault(); void rename(); }
                     if (e.key === 'Escape') setRenaming(false);
                   }} />
          ) : (
            <span className="text-sm text-text [overflow-wrap:anywhere]">{stepWords(step)}</span>
          )}
        </div>
        {person && <p className="text-xs text-text-muted">{PERSON_NOTE[person] ?? 'Yours to do: it is never kept.'}</p>}
        {fixing && (
          <Input size="sm" autoFocus value={fixed} aria-label="Fixed value" placeholder="What to type each time"
                 onChange={e => setFixed(e.target.value)}
                 onKeyDown={e => {
                   if (e.key === 'Enter' && fixed.trim()) { e.preventDefault(); onFixed(fixed); setFixing(false); }
                   if (e.key === 'Escape') setFixing(false);
                 }} />
        )}
        <div className="flex flex-wrap gap-1">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setTitle(stepWords(step)); setRenaming(true); }}>Rename</Button>
          {typed && value && 'fixed' in value && <Button variant="ghost" size="sm" disabled={busy} onClick={onInput}>Make it an input</Button>}
          {typed && value && 'input' in value && <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setFixed(''); setFixing(true); }}>Keep it fixed</Button>}
          <Button variant="ghost" size="sm" aria-expanded={details} onClick={() => setDetails(d => !d)}>Details</Button>
          {/* The rest of a step's actions, a click away (Naveen 10-29): one line per step on a phone. */}
          <Menu
            align="end"
            trigger={
              <button type="button" aria-label={`More for step ${step.n}`} title="More" disabled={busy}
                      className={buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true })}>
                <MoreHorizontal className="size-4" aria-hidden />
              </button>
            }
          >
            <MenuItem icon={RotateCcw} onSelect={onRedo}>Re-record from here</MenuItem>
            <MenuItem icon={Trash2} tone="danger" onSelect={onDelete}>Delete step</MenuItem>
          </Menu>
        </div>
        {details && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-text-muted">
            {step.target && (
              <>
                <dt>Target</dt>
                <dd className="min-w-0 [overflow-wrap:anywhere]" translate="no">
                  {[step.target.role, step.target.name || step.target.label, step.target.css].filter(Boolean).join(' · ')}
                </dd>
              </>
            )}
            {step.url && (<><dt>Address</dt><dd className="min-w-0 [overflow-wrap:anywhere]" translate="no">{step.url}</dd></>)}
            {step.wait?.url && (<><dt>Led to</dt><dd className="min-w-0 [overflow-wrap:anywhere]" translate="no">{step.wait.url}</dd></>)}
            {step.sends?.length ? (<><dt>Sends to</dt><dd className="min-w-0 [overflow-wrap:anywhere]" translate="no">{step.sends.join(', ')}</dd></>) : null}
          </dl>
        )}
      </div>
    </li>
  );
}

/**
 * Re-record from a step (BRW1 10-29): the recording's own browser, signed in with the picked sessions; the steps
 * before it replay, then the person records. Stop keeps the new steps as a fix waiting to be approved. The replay
 * can stop first: at the person's own step (the browser open, its banner showing; Carry on after it), or at an earlier
 * step that broke (offered instead).
 */
function Redo({ host, sessionsHost, browserHost, doc, from, onClose, onKept, onPickAgain }: {
  host: RecordingsHost;
  sessionsHost: SessionsHost;
  browserHost: BrowserHost;
  doc: Recording;
  from: RecordingStep;
  onClose: () => void;
  onKept: (words: string) => void;
  onPickAgain: (step: RecordingStep) => void;
}) {
  const [sessions, setSessions] = useState<SiteSession[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [opening, setOpening] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [open, setOpen] = useState<RecordBrowser | null>(null);
  const [handover, setHandover] = useState<{ reason: string } | null>(null);
  const [missed, setMissed] = useState<{ step: RecordingStep | undefined; line: string; why: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (sessionsHost.locked) return;
    listSessions(sessionsHost).then(setSessions, () => setSessions([]));
  }, [sessionsHost]);

  const begin = async (step: RecordingStep) => {
    setOpening(true);
    setRefused(null);
    const done = await openRecordBrowser(host, { recording_id: doc.id, from_step: step.id, sessions: picked });
    setOpening(false);
    if ('refused' in done) { setRefused(done.refused); return; }
    const stopped: ReplayStop | undefined = done.stopped;
    if (stopped && 'miss' in stopped) {
      // An earlier step broke: its browser is let go, and that step is offered instead.
      void stopRecording(host, done.opened.browserSessionId, { keep: false });
      const broke = doc.steps.find(s => s.id === stopped.miss.step_id);
      setMissed({ step: broke, line: stopped.miss.line ?? (broke ? stepWords(broke) : ''), why: stopped.miss.why ?? '' });
      return;
    }
    setMissed(null);
    setHandover(stopped && 'handover' in stopped ? { reason: stopped.handover.reason } : null);
    setOpen(done.opened);
  };

  const stop = async (keep: boolean) => {
    if (!open) return;
    setBusy(true);
    const done = await stopRecording(host, open.browserSessionId, { keep, from_step: from.id });
    setBusy(false);
    if ('refused' in done) { setFailed(done.refused); return; }
    setOpen(null);
    if (keep) onKept(`Your new steps from step ${from.n} are waiting for you to approve.`);
    else onClose();
  };

  const goOn = async () => {
    if (!open) return;
    setBusy(true);
    const done = await carryOn(host, open.browserSessionId);
    setBusy(false);
    if ('refused' in done) setFailed(done.refused);
    else setHandover(null);
  };

  if (open) {
    return (
      <div role="dialog" aria-modal="true" aria-label="Re-record from here" className="fixed inset-0 z-50 bg-scrim/60 p-3 md:p-8">
        <BrowserPane
          host={browserHost}
          sessionId={open.browserSessionId}
          agentName="You"
          actions={[]}
          record={{ onStop: stop, busy, handover, onCarryOn: goOn, failed }}
          onClosed={() => setOpen(null)}
          className="h-full overflow-hidden rounded-xl border border-border shadow-elevated"
        />
      </div>
    );
  }

  return (
    <>
      {missed && (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-text">
          <span className="min-w-0 grow basis-64">
            The steps before it stopped at step {missed.step?.n ?? '?'}, “{missed.line}”{missed.why ? `: ${missed.why}` : ''}.
          </span>
          {missed.step && (
            <Button size="sm" variant="secondary" onClick={() => { setMissed(null); onPickAgain(missed.step!); }}>
              Re-record from step {missed.step.n}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
        </div>
      )}
      <Modal
        open={!missed}
        onClose={onClose}
        title="Re-record from here"
        description={`The steps before step ${from.n} replay in the browser, then you do it again from “${stepWords(from)}”. Your new steps wait for you to approve them.`}
        footer={(
          <>
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button onClick={() => begin(from)} loading={opening}>Open the browser</Button>
          </>
        )}
      >
        <div className="grid gap-3">
          <SessionPicker
            label="Sessions it starts with"
            sessions={sessions}
            value={picked}
            onChange={setPicked}
            libraryHref={sessionsHost.libraryHref}
            locked={sessionsHost.locked}
            hint={doc.sites.length ? `It goes to ${doc.sites.join(', ')}. One per site.` : 'One per site.'}
          />
          {refused && <ErrorNote>{refused}</ErrorNote>}
        </div>
      </Modal>
    </>
  );
}

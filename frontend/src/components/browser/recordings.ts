/**
 * Recordings: a task done once in the browser while Synapse records it, kept for agents and workflows to repeat
 * (BRW2, F1142; DESIGN v5-4, v5.2, the shapes frozen in §v5.5 and as built by BRW1 on feat/brw1-rec). A step reads as
 * its sentence (`title ?? line`); a typed value is fixed or an input; the person's own sign-in, card or code is never
 * kept, only that it is theirs to do.
 *
 * How they are reached is the product's (`RecordingsHost`): OSS through `/api/recordings…`, cloud through its own.
 * Synced to cloud with the folder.
 */
import { refusal } from './sessions';

export type StepValue = { fixed: string } | { input: string } | { person: 'password' | 'card' | 'code' } | null;

export type StepTarget = { role?: string; name?: string; label?: string; text?: string; css?: string; field?: boolean } | null;

export type RecordingStep = {
  id: string;
  n: number;
  action: 'open' | 'click' | 'type' | 'choose' | 'press' | 'upload' | 'download' | 'read' | 'back' | string;
  /** The server's sentence for it ("Click Search"). */
  line: string;
  /** The person's rename, shown instead of `line`. */
  title?: string;
  url?: string;
  key?: string;
  target?: StepTarget;
  value?: StepValue;
  /** A read step's output name. */
  output?: string;
  /** The picture after it, or none (a page something sensitive was typed on has none). */
  after_shot?: string | null;
  /** The page it led to, learned while recording (F1143). */
  wait?: { for: string; url?: string };
  /** Where it sent, never what ("POST api.example.com"; F1143). */
  sends?: string[];
};

export type RecordingInput = { name: string; label: string; step_id: string };
export type RecordingOutput = { name: string; kind: 'rows' | 'value' | 'file' | string; step_id: string };
export type PendingEdit = { edit_id: string; step_id: string; steps: RecordingStep[]; by: string; run_id?: string | null; at: number };

export type Recording = {
  id: string;
  name: string;
  sites: string[];
  inputs: RecordingInput[];
  outputs: RecordingOutput[];
  steps: RecordingStep[];
  version: number;
  pending_edits: PendingEdit[];
  changed: string[];
  created_at: number;
  updated_at: number | null;
  updated_by?: string;
};

/** A row of the Recordings list. */
export type RecordingSummary = Pick<Recording, 'id' | 'name' | 'sites' | 'version' | 'created_at' | 'updated_at'> & {
  steps: number;
  pending_edits: number;
};

export type RecordingVersion = { version: number; at: number | null; by: string | null; steps: number; current: boolean };

export type RecordingsHost = {
  listUrl: () => string;
  /** GET, PATCH (`base_version`), DELETE one recording. */
  recordingUrl: (id: string) => string;
  /** POST `{sessions, start_url?}` → a browser recording a new one; with `{recording_id, from_step}`, re-recording. */
  newUrl: () => string;
  shotUrl: (id: string, ref: string) => string;
  versionsUrl: (id: string) => string;
  recordStartUrl: (browserSessionId: string) => string;
  recordStopUrl: (browserSessionId: string) => string;
  /** A recording's own page. */
  recordingHref: (id: string) => string;
  /** The Recordings list. */
  libraryHref: string;
  locked?: { upgradeHref: string };
};

export type RecordBrowser = { recordingId: string; browserSessionId: string };

/** Why a re-record's replay stopped before recording began (BRW1, 10-29). The browser stays open on a handover. */
export type ReplayStop =
  | { handover: { kind: string; reason: string; session_id?: string }; step_id?: string }
  | { miss: { step_id: string; line?: string; why?: string } };

export async function listRecordings(host: RecordingsHost): Promise<RecordingSummary[]> {
  const res = await fetch(host.listUrl());
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json();
  return Array.isArray(body?.recordings) ? body.recordings : [];
}

export async function getRecording(host: RecordingsHost, id: string): Promise<Recording | null> {
  const res = await fetch(host.recordingUrl(id));
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export type Change = Partial<Pick<Recording, 'name' | 'steps' | 'inputs' | 'outputs'>>;

/** A change from the version the page has: the new recording, or why it wasn't kept (409: changed since). */
export async function changeRecording(host: RecordingsHost, doc: Recording, change: Change):
    Promise<{ doc: Recording } | { stale: string } | { failed: string }> {
  const res = await fetch(host.recordingUrl(doc.id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ base_version: doc.version, ...change }),
  }).catch(() => null);
  const body = await res?.json().catch(() => ({})) ?? {};
  if (res?.ok) return { doc: body as Recording };
  if (res?.status === 409) return { stale: refusal(body, 'This recording changed since you opened it. Reload it.') };
  return { failed: refusal(body, 'That change was not kept. Try again.') };
}

export async function deleteRecording(host: RecordingsHost, id: string): Promise<boolean> {
  const res = await fetch(host.recordingUrl(id), { method: 'DELETE' }).catch(() => null);
  return !!res && (res.ok || res.status === 404);
}

export async function listVersions(host: RecordingsHost, id: string): Promise<RecordingVersion[]> {
  const res = await fetch(host.versionsUrl(id));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json();
  return Array.isArray(body?.versions) ? body.versions : [];
}

/**
 * A browser recording: a new task (`{sessions, start_url}`), or re-recording one from a step
 * (`{recording_id, from_step, sessions}`). A re-record's replay can stop first (409): at the person's own step, with
 * the browser open, or at a step that broke.
 */
export async function openRecordBrowser(host: RecordingsHost, body: Record<string, unknown>):
    Promise<{ opened: RecordBrowser; stopped?: ReplayStop } | { refused: string }> {
  const res = await fetch(host.newUrl(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => null);
  const answer = await res?.json().catch(() => ({})) ?? {};
  const opened = typeof answer.browser_session_id === 'string'
    ? { recordingId: String(answer.recording_id ?? body.recording_id ?? ''), browserSessionId: answer.browser_session_id }
    : null;
  if (res?.ok && opened) return { opened };
  if (res?.status === 409 && opened && answer.replayed) return { opened, stopped: answer.replayed as ReplayStop };
  return { refused: refusal(answer, 'The browser could not be opened. Try again.') };
}

/** After the person's own step in a re-record's replay: the rest replays, then it records. */
export async function carryOn(host: RecordingsHost, browserSessionId: string): Promise<{ ok: true } | { refused: string }> {
  const res = await fetch(host.recordStartUrl(browserSessionId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ carry_on: true }),
  }).catch(() => null);
  if (res?.ok) return { ok: true };
  return { refused: refusal(await res?.json().catch(() => ({})) ?? {}, 'It could not carry on. Try again.') };
}

/** Stop recording: kept (a new recording, or a re-record's fix waiting to be approved) or dropped. */
export async function stopRecording(host: RecordingsHost, browserSessionId: string,
                                    body: { keep: boolean; from_step?: string; name?: string; workflow_id?: string }):
    Promise<{ recordingId: string; pendingEdit?: string } | { dropped: true } | { refused: string }> {
  const res = await fetch(host.recordStopUrl(browserSessionId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => null);
  const answer = await res?.json().catch(() => ({})) ?? {};
  if (!res?.ok) return { refused: refusal(answer, 'The recording could not be stopped. Try again.') };
  if (!body.keep) return { dropped: true };
  return { recordingId: String(answer.recording_id ?? ''), ...(answer.pending_edit ? { pendingEdit: String(answer.pending_edit) } : {}) };
}

/** A step as the person reads it. */
export function stepWords(step: Pick<RecordingStep, 'title' | 'line'>): string {
  return step.title?.trim() || step.line;
}

/** "Order number" → "order_number": an input's name, as runs and agents pass it. */
export function inputName(label: string, taken: Iterable<string> = []): string {
  const base = label.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'value';
  const used = new Set(taken);
  let name = base;
  for (let i = 2; used.has(name); i += 1) name = `${base}_${i}`;
  return name;
}

/** The field a step typed into, in words. */
export function fieldOf(step: Pick<RecordingStep, 'target'>): string {
  const t = step.target ?? {};
  return t.label || t.name || t.text || 'a field';
}

/**
 * Each typed, fixed value proposed as an input named from its field (DESIGN v5.2: on Stop, the editor proposes them).
 * Choices and the person's own values are left as they are.
 */
export function proposedInputs(doc: Pick<Recording, 'steps' | 'inputs'>): { step: RecordingStep; input: RecordingInput }[] {
  const taken = doc.inputs.map(i => i.name);
  const out: { step: RecordingStep; input: RecordingInput }[] = [];
  for (const step of doc.steps) {
    if (step.action !== 'type' || !step.value || !('fixed' in step.value)) continue;
    const label = fieldOf(step);
    const name = inputName(label, taken);
    taken.push(name);
    out.push({ step, input: { name, label, step_id: step.id } });
  }
  return out;
}

/** The outputs a recording gives: its list, or (until it has one) each read step's. */
export function outputsOf(doc: Pick<Recording, 'steps' | 'outputs'>): RecordingOutput[] {
  if (doc.outputs.length) return doc.outputs;
  return doc.steps.filter(s => s.action === 'read' && s.output).map(s => ({ name: s.output!, kind: 'value', step_id: s.id }));
}

export function stepCount(n: number): string {
  return `${n} step${n === 1 ? '' : 's'}`;
}

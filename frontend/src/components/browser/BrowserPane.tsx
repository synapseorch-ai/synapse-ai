'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Globe, Info, KeyRound, Maximize2, Square, X } from 'lucide-react';

import { Button, IconButton, Input, Modal, Spinner, usePersisted } from '@/components/ui';
import { cn } from '@/lib/cn';

import { BrowserActionLine } from './BrowserActionLine';
import { actionCount, hostOf, mergeActions, shortUrl, type BrowserAction, type BrowserFrame, type BrowserHost, type BrowserInput,
         type BrowserSessionEvent, type FrameRecording, type HandedBack, type Wheel } from './model';
import { DialogAsk, FileAsk, MomentStrip, SelectAsk } from './PageMoments';
import { PageScreen } from './PageScreen';
import { SessionPicker } from './SessionPicker';
import { stepCount } from './recordings';
import { onSite, shortDate, type KeptSession } from './sessions';
import { useBrowserFrames, useKeptLog, type FrameMemory } from './useBrowserFrames';

/**
 * The agent's browser, live, beside the chat or on a run's page (BRW2, D330).
 *
 * The page arrives as the component's screencast, one JPEG per repaint, on an
 * `<img>` letterboxed to the pane. A banner says who holds the wheel. Stop ends
 * the agent's turn or run and closes its browser; closing the pane only stops
 * watching. Once the browser closes its last page stays, dimmed, with what the
 * agent did under it (Naveen, 2026-10-27).
 *
 * The wheel (DESIGN §2.4, F1076): where the host lets the person drive, a strip
 * under the address says who holds it. Take over is a button; a press on the page
 * while the agent drives offers it rather than acting, since "any input takes
 * over" races the agent's next action. While the person drives the strip is
 * theirs (amber, Naveen's pick of the prototype's A), their input reaches the
 * page, the address bar takes an address, and Hand back carries a note.
 */
export function BrowserPane({
  host, sessionId, agentName, actions, memory, closedBefore = false, onClosed, onHide, onHandedBack, signIn, record, owner, signedIn, picks,
  hero = false, enlarge = false, className, style, children,
}: {
  host: BrowserHost;
  sessionId: string;
  /** Who is driving, in the banner: the agent's name. */
  agentName: string;
  /** What the agent did in this browser so far, oldest first. */
  actions: BrowserAction[];
  /** Where the last frame is kept: shown until a new one comes, and after close. */
  memory?: FrameMemory;
  /** The browser was already known to be closed: nothing is read. */
  closedBefore?: boolean;
  onClosed?: () => void;
  /** Close the pane, leaving the browser as it is. */
  onHide?: () => void;
  /** The wheel went back to the agent: a turn waiting on the person can go on. */
  onHandedBack?: (result?: HandedBack) => void;
  /**
   * "Keep me signed in to a site" (F1079, F1139): the person alone, no agent. The strip is the consent card (DESIGN
   * v5-1): it says what is kept, and Keep ends the browser with its session kept. `refused`: why a keep was refused
   * (nothing is signed in yet), said on the card while the browser stays open to sign in.
   */
  signIn?: { site: string; onFinish: (keep: boolean) => void; busy?: boolean; refused?: string | null };
  /**
   * Recording a task (F1142): the person's alone, as `signIn` is. Its strip says it is recording, how many steps and
   * the last, and that a password being typed isn't kept; Stop and keep, or Discard. A re-record's replay that stopped
   * at the person's own step (`handover`) says what to do there, then Carry on.
   */
  record?: {
    onStop: (keep: boolean) => void;
    busy?: boolean;
    handover?: { reason: string } | null;
    onCarryOn?: () => void;
    failed?: string | null;
  };
  /** Whose browser it is (`chat:<id>`, `run:<id>`): once it closes, its lines' screenshots are the kept ones. */
  owner?: string;
  /** What it was signed in with, and what it could not be (`browser_session`, F1140). */
  signedIn?: Pick<BrowserSessionEvent, 'sessions' | 'skipped'>;
  /**
   * The chat's own sessions, picked from the pane (F1140): they apply when the browser next opens. `failed`: why the
   * last pick was not taken (the server's 422 for two on one site).
   */
  picks?: Omit<React.ComponentProps<typeof SessionPicker>, 'label'> & { label?: string; failed?: string | null };
  /**
   * The picture is the pane (F1356, the run page): it fills the column at the page's own aspect, never a fixed
   * height, and what the agent did folds into a closed list under it. Off, the pane is as it always was.
   */
  hero?: boolean;
  /** An Enlarge button: the live page in a large dialog, Esc to close, Take over still in reach (F1356). */
  enlarge?: boolean;
  className?: string;
  style?: React.CSSProperties;
  /** The pane's own edge, such as its resize handle. */
  children?: React.ReactNode;
}) {
  const { frame, closed: streamClosed, log } = useBrowserFrames(host.framesUrl(sessionId), !closedBefore, memory, sessionId);
  const closed = closedBefore || streamClosed;
  const [stopping, setStopping] = useState(false);
  const [stopFailed, setStopFailed] = useState(false);

  const told = useRef(false);
  useEffect(() => {
    if (streamClosed && !told.current) {
      told.current = true;
      onClosed?.();
    }
  }, [streamClosed, onClosed]);

  const stop = async () => {
    setStopping(true);
    setStopFailed(false);
    const ok = await host.stop(sessionId).catch(() => false);
    setStopping(false);
    if (!ok) setStopFailed(true);
  };

  // What the person just did to the wheel holds until the next frame, which is the component's word on it.
  const [moved, setMoved] = useState<{ wheel: Wheel; on: BrowserFrame | null } | null>(null);
  const wheel: Wheel = moved && moved.on === frame ? moved.wheel : frame?.wheel ?? 'agent';
  const canDrive = !closed && !!host.takeWheel && !!host.handBack && !!host.input;
  const driving = canDrive && wheel === 'person';
  const [nudge, setNudge] = useState(false);
  const [enlarged, setEnlarged] = useState(false);
  const [note, setNote] = useState('');
  const [wheelFailed, setWheelFailed] = useState<string | null>(null);

  // One input at a time, in the order made: typing must arrive as typed.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const send = (input: BrowserInput) => {
    queue.current = queue.current.then(() => host.input?.(sessionId, input)).catch(() => undefined);
  };

  const takeOver = async () => {
    setNudge(false);
    setWheelFailed(null);
    const ok = await host.takeWheel?.(sessionId).catch(() => false);
    if (ok) setMoved({ wheel: 'person', on: frame });
    else setWheelFailed('The wheel could not be taken. Try again.');
  };
  // The session the person signed in again with, kept by the hand-back (F1140).
  const [keptAgain, setKeptAgain] = useState<KeptSession | null>(null);
  const [picking, setPicking] = useState(false);
  const handBack = async () => {
    setWheelFailed(null);
    let refused: string | null = null;
    const ok = await host.handBack?.(sessionId, note.trim() || undefined)
      .catch((e: unknown) => { refused = e instanceof Error ? e.message : null; return false; });
    if (refused) {
      setWheelFailed(refused);
      return;
    }
    if (ok) {
      if (typeof ok === 'object' && ok.kept) setKeptAgain(ok.kept);
      setMoved({ wheel: 'agent', on: frame });
      setNote('');
      // What came of it goes to the page: cloud follows the turn the server resumed (`resumed`).
      onHandedBack?.(ok);
    } else setWheelFailed('The wheel could not be handed back. Try again.');
  };

  // A moment the person answered stays answered until the next frame says what the page did.
  const [answeredOn, setAnsweredOn] = useState<BrowserFrame | null>(null);
  const asks = driving && frame && answeredOn !== frame ? frame : null;
  const answer = (call: Promise<boolean> | undefined) => {
    setAnsweredOn(frame);
    call?.catch(() => false);
  };
  // Letting a held send through, or allowing a site, answers a turn waiting on the person as a hand-back does.
  const resuming = (call: Promise<boolean>) => {
    setAnsweredOn(frame);
    call.then((ok) => { if (ok) onHandedBack?.(); }).catch(() => undefined);
  };
  const release = host.release ? () => resuming(host.release!(sessionId, true)) : undefined;
  const allow = host.allowSite ? (site: string) => resuming(host.allowSite!(sessionId, site)) : undefined;
  const banner = !closed && frame?.banner && answeredOn !== frame ? frame.banner : null;

  // A line's screenshot: the open browser's own; once it has closed, the copy kept with its chat or run.
  const [ownerKind, ownerId] = (owner ?? '').split(':', 2) as [string, string | undefined];
  const keptOwner = ownerId && (ownerKind === 'chat' || ownerKind === 'run') ? ownerKind : null;

  // What was done: the agent's lines and the person's own while it is open; once closed, the whole log kept with
  // its chat or run (the record of who pressed what), or what was heard when none was kept.
  const kept = useKeptLog(closed && keptOwner && host.keptLogUrl ? host.keptLogUrl(keptOwner, ownerId!) : null);
  const lines = useMemo(() => {
    const heard = mergeActions(actions, log);
    if (!kept) return heard;
    const ours = kept.filter(a => !a.session_id || a.session_id === sessionId).map(a => ({ ...a, session_id: sessionId }));
    return ours.length ? mergeActions(heard, ours) : heard;
  }, [actions, log, kept, sessionId]);
  const shotFor = (a: BrowserAction) => {
    if (!a.shot_ref) return null;
    if (closed && host.keptShotUrl && keptOwner) {
      return host.keptShotUrl(keptOwner, ownerId!, Number(a.shot_ref));
    }
    return host.shotUrl(a.session_id, a.shot_ref);
  };

  const url = frame?.url || lines[lines.length - 1]?.url || '';
  const driver = wheel === 'person' ? 'You’re driving' : wheel === 'nobody' ? 'Nobody is driving' : `${agentName} is driving`;

  // Said once to each person, in this browser, and in the terms (Naveen 2026-10-28). A sign-in browser is the person's alone.
  const [disclaimerSeen, setDisclaimerSeen] = usePersisted<'0' | '1'>(
    'browser-disclaimer-seen', raw => (raw === '1' ? '1' : '0'), '1');
  // A browser for the person alone (signing in, recording): no agent's Stop, Take over, picks or disclaimer.
  const alone = !!signIn || !!record;
  const disclaimer = !alone && !closed && disclaimerSeen === '0';

  // The newest action stays in view as the list grows.
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <section
      aria-label="Browser"
      className={cn('relative flex min-h-0 min-w-0 flex-col bg-bg', className)}
      style={style}
    >
      {children}
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Globe className="size-4 shrink-0 text-text-faint" aria-hidden />
        {driving ? (
          <form
            className="min-w-0 flex-1"
            onSubmit={(e) => {
              e.preventDefault();
              const typed = String(new FormData(e.currentTarget).get('address') ?? '').trim();
              if (typed) send({ type: 'nav', url: /^[a-z][a-z0-9+.-]*:/i.test(typed) ? typed : `https://${typed}` });
            }}
          >
            <Input key={url} name="address" size="sm" defaultValue={url} aria-label="Address" spellCheck={false}
                   translate="no" className="w-full" />
          </form>
        ) : (
          <span className="min-w-0 flex-1 truncate text-sm text-text" title={url || undefined} translate="no">
            {url ? shortUrl(url) : 'Opening the browser…'}
          </span>
        )}
        {picks && !alone && (
          <Button variant="ghost" size="sm" onClick={() => setPicking(p => !p)} aria-expanded={picking}
                  title="The sessions this chat’s browser signs in with">
            <KeyRound className="size-3.5" aria-hidden />
            Sessions
          </Button>
        )}
        {!closed && !alone && (
          <Button variant="ghost" size="sm" onClick={stop} loading={stopping}
                  title="Stop the agent and close its browser">
            {!stopping && <Square className="size-3.5" aria-hidden />}
            Stop
          </Button>
        )}
        {enlarge && frame && (
          <Button variant="ghost" size="sm" onClick={() => setEnlarged(true)} title="See the page large">
            <Maximize2 className="size-3.5" aria-hidden />
            Enlarge
          </Button>
        )}
        {onHide && <IconButton icon={X} label="Close the browser pane" size="sm" onClick={onHide} />}
      </header>

      {picks && picking && !alone && <PicksPanel picks={picks} />}
      <SignedInLine signedIn={signedIn} kept={keptAgain} host={url ? hostOf(url) : ''} />

      {signIn && !closed ? (
        <div role="status" className="flex shrink-0 flex-col gap-2 border-b border-person bg-person-subtle px-3 py-2.5 text-xs text-text">
          <div className="flex flex-wrap items-center gap-2">
            <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />
            {/* 20rem of words before the buttons: on a phone they go under the words rather than squeeze them. */}
            <p className="min-w-0 grow basis-80 leading-relaxed">
              <strong className="font-semibold">Keep me signed in to <span translate="no">{signIn.site}</span> for your agents.</strong>{' '}
              {host.product} keeps the site’s session, never your password. It expires in 30 days; remove it anytime.
              Only you drive this browser, and nothing is recorded.
            </p>
            <div className="ml-auto flex shrink-0 gap-2">
              <Button variant="secondary" size="sm" disabled={signIn.busy} onClick={() => signIn.onFinish(false)}>Don’t keep</Button>
              <Button variant="person" size="sm" loading={signIn.busy} onClick={() => signIn.onFinish(true)}>Keep</Button>
            </div>
          </div>
          {signIn.refused && <p role="alert" className="leading-relaxed text-danger">{signIn.refused}</p>}
        </div>
      ) : record && !closed ? (
        <RecordStrip record={record} recording={frame?.recording} />
      ) : banner ? (
        <MomentStrip banner={banner} agentName={agentName} onDone={driving ? handBack : undefined}
                     onRelease={release} onAllow={allow} onTakeOver={canDrive && !driving ? takeOver : undefined}
                     failed={wheelFailed} />
      ) : driving ? (
        <div role="status" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-person bg-person-subtle px-3 py-2 text-xs">
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />
          <span className="whitespace-nowrap font-medium text-text">{driver}</span>
          <Input
            size="sm"
            value={note}
            onChange={e => setNote(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handBack(); }}
            placeholder="A note for the agent (optional): I signed in, carry on"
            aria-label="Note for the agent"
            className="min-w-[10rem] flex-1"
          />
          <Button size="sm" onClick={handBack}>Hand back</Button>
          {wheelFailed && <span className="basis-full text-danger">{wheelFailed}</span>}
        </div>
      ) : (
        <div role="status" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-text-muted">
          {closed ? (
            <span>Browser closed · {actionCount(lines.length)}</span>
          ) : (
            <>
              <span aria-hidden className="relative flex size-2">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-machine opacity-60 motion-reduce:hidden" />
                <span className="relative inline-flex size-2 rounded-full bg-machine" />
              </span>
              <span className="flex-1">{driver}</span>
              {canDrive && <Button variant="secondary" size="sm" onClick={takeOver}>Take over</Button>}
              {stopFailed && <span className="basis-full text-danger">It could not be stopped. Try again.</span>}
              {wheelFailed && <span className="basis-full text-danger">{wheelFailed}</span>}
            </>
          )}
        </div>
      )}

      <div
        className={cn('relative flex items-center justify-center overflow-hidden bg-surface-2',
                      hero ? 'w-full shrink-0' : 'min-h-0 flex-1 p-3')}
        // The page's own shape (F1356): the frame's, else the browser's 16:10.
        style={hero ? { aspectRatio: `${frame?.w || 16} / ${frame?.h || 10}` } : undefined}
      >
        {frame ? (
          <PageScreen
            frame={frame}
            closed={closed}
            driving={driving}
            alt={url ? `The page the agent sees: ${shortUrl(url)}` : 'The page the agent sees'}
            send={send}
            onPressWhileWatching={() => { if (canDrive) setNudge(true); }}
          />
        ) : closed ? (
          <p className="text-sm text-text-muted">Browser closed</p>
        ) : (
          <div className="flex items-center gap-2 text-sm text-text-muted">
            <Spinner size={16} />
            Opening the browser…
          </div>
        )}
        {asks?.dialog ? (
          <DialogAsk key={asks.dialog.message} dialog={asks.dialog}
                     onAnswer={(accept, text) => answer(host.answerDialog?.(sessionId, accept, text))} />
        ) : asks?.select ? (
          <SelectAsk select={asks.select} onChoose={(value) => answer(host.choose?.(sessionId, asks.select!.target, value))} />
        ) : asks?.file ? (
          <FileAsk file={asks.file} onCancel={() => setAnsweredOn(frame)}
                   onOffer={async (picked) => {
                     const ok = (await host.offerFile?.(sessionId, picked).catch(() => false)) ?? false;
                     if (ok) setAnsweredOn(frame);
                     return ok;
                   }} />
        ) : null}
        {nudge && canDrive && !driving && (
          <div className="absolute inset-0 flex items-center justify-center bg-scrim/60">
            <div role="dialog" aria-label={`${agentName} is driving`}
                 className="flex w-80 max-w-[calc(100%-2rem)] flex-col gap-2.5 rounded-xl bg-bg p-4 shadow-elevated">
              <p className="text-sm font-semibold text-text">{agentName} is driving</p>
              <p className="text-sm text-text-muted">
                Take over to use the page yourself. The agent finishes what it is doing, then waits for you.
              </p>
              <div className="flex justify-end gap-2">
                <Button variant="secondary" size="sm" onClick={() => setNudge(false)}>Keep watching</Button>
                <Button size="sm" onClick={takeOver} autoFocus>Take over</Button>
              </div>
            </div>
          </div>
        )}
      </div>

      {disclaimer && (
        <div role="note" className="flex shrink-0 items-start gap-2 border-t border-border bg-surface px-3 py-2 text-xs text-text-muted">
          <Info className="mt-0.5 size-3.5 shrink-0 text-text-faint" aria-hidden />
          <p className="min-w-0 flex-1 leading-relaxed">
            {/* Only what is guaranteed (Naveen 10-29): a send to another site, or a page's own script, is not held. */}
            Agents can make mistakes. You always press payments yourself.
          </p>
          <Button variant="ghost" size="sm" onClick={() => setDisclaimerSeen('1')}>Got it</Button>
        </div>
      )}

      {lines.length > 0 && !hero && (
        <ol ref={list} aria-label="What the agent did" className="max-h-36 shrink-0 space-y-1.5 overflow-y-auto border-t border-border px-3 py-2 text-xs text-text-muted">
          {lines.map(a => (
            <li key={`${a.session_id}:${a.n}`}>
              <BrowserActionLine action={a} dense shot={shotFor(a)} />
            </li>
          ))}
        </ol>
      )}
      {lines.length > 0 && hero && (
        // Folded under the picture, closed until asked for (F1356).
        <details className="group shrink-0 border-t border-border text-xs text-text-muted">
          <summary className="cursor-pointer select-none px-3 py-2 font-medium text-text hover:bg-surface-2">
            What the agent did · {lines.length}
          </summary>
          <ol ref={list} aria-label="What the agent did" className="max-h-64 space-y-1.5 overflow-y-auto px-3 pb-2">
            {lines.map(a => (
              <li key={`${a.session_id}:${a.n}`}>
                <BrowserActionLine action={a} dense shot={shotFor(a)} />
              </li>
            ))}
          </ol>
        </details>
      )}
      {enlarge && (
        <Modal
          open={enlarged && !!frame}
          onClose={() => setEnlarged(false)}
          title={url ? shortUrl(url) : 'The browser'}
          size="full"
          footer={
            <div className="flex w-full flex-wrap items-center gap-2 text-xs text-text-muted">
              <span className="flex-1">{driver}</span>
              {canDrive && !driving && <Button variant="secondary" size="sm" onClick={takeOver}>Take over</Button>}
            </div>
          }
        >
          {frame && (
            <div className="flex w-full items-center justify-center" style={{ aspectRatio: `${frame.w || 16} / ${frame.h || 10}` }}>
              <PageScreen
                frame={frame}
                closed={closed}
                driving={driving}
                alt={url ? `The page the agent sees, large: ${shortUrl(url)}` : 'The page the agent sees, large'}
                send={send}
                onPressWhileWatching={() => { if (canDrive) setNudge(true); }}
              />
            </div>
          )}
        </Modal>
      )}
    </section>
  );
}

/** The chat's own sessions, under the pane's header (F1140). */
function PicksPanel({ picks }: { picks: NonNullable<React.ComponentProps<typeof BrowserPane>['picks']> }) {
  const { failed, label, ...pick } = picks;
  return (
    <div className="shrink-0 border-b border-border bg-surface px-3 py-3">
      <SessionPicker {...pick} label={label ?? 'Sessions for this chat'}
                     hint="One per site. Applies when the browser next opens." />
      {failed && <p role="alert" className="mt-2 text-xs text-danger">{failed}</p>}
    </div>
  );
}

/**
 * Which sessions the browser started signed in with, and why one wasn't loaded (F1140; DESIGN v5.1): expired, or
 * someone else's that isn't shared. After the person signs in again on an expired session's turn, until when the fresh
 * one is kept.
 */
function SignedInLine({ signedIn, kept, host }: {
  signedIn?: Pick<BrowserSessionEvent, 'sessions' | 'skipped'>;
  kept: KeptSession | null;
  /** The page in view: the line speaks only for its site (Naveen 10-29). */
  host: string;
}) {
  const sites = [...new Set((signedIn?.sessions ?? []).map(s => s.site))].filter(site => onSite(host, site));
  const skipped = (signedIn?.skipped ?? []).filter(s => onSite(host, s.site));
  if (!sites.length && !skipped.length && !kept) return null;
  return (
    <div className="flex shrink-0 flex-col gap-0.5 border-b border-border px-3 py-1.5 text-xs text-text-muted">
      {sites.length > 0 && <p>Signed in to <span translate="no">{sites.join(', ')}</span>.</p>}
      {skipped.map(s => (
        <p key={s.site}>
          Not signed in to <span translate="no">{s.site}</span>:{' '}
          {s.reason === 'expired' ? 'its session expired.' : `${s.owner_name}’s session isn’t shared with you.`}
        </p>
      ))}
      {kept && (
        <p role="status">
          Signed in again: <span translate="no">{kept.site}</span>’s session is kept until{' '}
          {shortDate(kept.expires_at)}.
        </p>
      )}
    </div>
  );
}


/** What a held field's typing is, in the strip's words: never kept, not even blanked (DESIGN v5.2). */
const HOLDING: Record<NonNullable<FrameRecording['holding']>, string> = {
  password: 'Typing a password: the password isn’t kept.',
  card: 'Typing card details: they aren’t kept.',
  code: 'Typing a code: the code isn’t kept.',
};

/** The record browser's strip (F1142): it is recording, how far, what isn't kept; or, mid-replay, the person's step. */
function RecordStrip({ record, recording }: {
  record: NonNullable<React.ComponentProps<typeof BrowserPane>['record']>;
  recording?: FrameRecording;
}) {
  return (
    <div role="status" className="flex shrink-0 flex-col gap-2 border-b border-person bg-person-subtle px-3 py-2.5 text-xs text-text">
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden className="size-2 shrink-0 rounded-full bg-danger motion-safe:animate-pulse" />
        <p className="min-w-0 grow basis-80 leading-relaxed">
          {record.handover ? (
            <>
              <strong className="font-semibold">{record.handover.reason}.</strong>{' '}
              This step is yours: do it here, then carry on. The rest replays, then recording starts.
            </>
          ) : (
            <>
              <strong className="font-semibold">Recording · {stepCount(recording?.steps ?? 0)}</strong>
              {recording?.last && <> · Last: <span>{recording.last.title}</span></>}
              {'. '}
              {recording?.holding ? HOLDING[recording.holding] : 'Each thing you do here is a step. Passwords, cards and codes are never kept.'}
            </>
          )}
        </p>
        <div className="ml-auto flex shrink-0 gap-2">
          <Button variant="secondary" size="sm" disabled={record.busy} onClick={() => record.onStop(false)}>
            {record.handover ? 'Cancel' : 'Discard'}
          </Button>
          {record.handover ? (
            <Button variant="person" size="sm" loading={record.busy} onClick={record.onCarryOn}>Carry on</Button>
          ) : (
            <Button variant="person" size="sm" loading={record.busy} onClick={() => record.onStop(true)}>Stop and keep</Button>
          )}
        </div>
      </div>
      {record.failed && <p role="alert" className="leading-relaxed text-danger">{record.failed}</p>}
    </div>
  );
}

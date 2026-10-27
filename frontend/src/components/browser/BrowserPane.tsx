'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Globe, Info, Square, X } from 'lucide-react';

import { Button, IconButton, Input, Spinner, usePersisted } from '@/components/ui';
import { cn } from '@/lib/cn';

import { BrowserActionLine } from './BrowserActionLine';
import { actionCount, mergeActions, shortUrl, type BrowserAction, type BrowserFrame, type BrowserHost, type BrowserInput,
         type Wheel } from './model';
import { DialogAsk, FileAsk, MomentStrip, SelectAsk } from './PageMoments';
import { PageScreen } from './PageScreen';
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
  host, sessionId, agentName, actions, memory, closedBefore = false, onClosed, onHide, onHandedBack, login, owner,
  className, style, children,
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
  onHandedBack?: () => void;
  /**
   * "Log in to a site" (F1079): the person alone, no agent. The strip offers keeping the login, which ends the
   * browser either way; there is no Stop, Take over or Hand back.
   */
  login?: { sites: string[]; onFinish: (keep: boolean) => void; busy?: boolean };
  /** Whose browser it is (`chat:<id>`, `run:<id>`): once it closes, its lines' screenshots are the kept ones. */
  owner?: string;
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
      setMoved({ wheel: 'agent', on: frame });
      setNote('');
      onHandedBack?.();
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

  // Said once to each person, in this browser, and in the terms (Naveen 2026-10-28). A login is the person's alone.
  const [disclaimerSeen, setDisclaimerSeen] = usePersisted<'0' | '1'>(
    'browser-disclaimer-seen', raw => (raw === '1' ? '1' : '0'), '1');
  const disclaimer = !login && !closed && disclaimerSeen === '0';

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
        {!closed && !login && (
          <Button variant="ghost" size="sm" onClick={stop} loading={stopping}
                  title="Stop the agent and close its browser">
            {!stopping && <Square className="size-3.5" aria-hidden />}
            Stop
          </Button>
        )}
        {onHide && <IconButton icon={X} label="Close the browser pane" size="sm" onClick={onHide} />}
      </header>

      {login && !closed ? (
        <div role="status" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-person bg-person-subtle px-3 py-2.5 text-xs text-person-fg">
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />
          <span className="min-w-0 flex-1 leading-relaxed">
            <strong className="font-semibold">Log in to <span translate="no">{login.sites.join(', ')}</span>.</strong>{' '}
            Only you drive this browser, and nothing is recorded. When you&apos;re in, keep the login so runs can use it.
          </span>
          <Button variant="secondary" size="sm" disabled={login.busy} onClick={() => login.onFinish(false)}>Don&apos;t keep it</Button>
          <Button variant="person" size="sm" loading={login.busy} onClick={() => login.onFinish(true)}>Keep this login</Button>
        </div>
      ) : banner ? (
        <MomentStrip banner={banner} agentName={agentName} onDone={driving ? handBack : undefined}
                     onRelease={release} onAllow={allow} onTakeOver={canDrive && !driving ? takeOver : undefined}
                     failed={wheelFailed} />
      ) : driving ? (
        <div role="status" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-person bg-person-subtle px-3 py-2 text-xs">
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />
          <span className="whitespace-nowrap font-medium text-person-fg">{driver}</span>
          <Input
            size="sm"
            value={note}
            onChange={e => setNote(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handBack(); }}
            placeholder="A note for the agent (optional): I logged in, carry on"
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

      <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-surface-2 p-3">
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
            Agents can make mistakes. You press every payment, and an agent presses Submit or Send only when
            you&apos;ve asked it to.
          </p>
          <Button variant="ghost" size="sm" onClick={() => setDisclaimerSeen('1')}>Got it</Button>
        </div>
      )}

      {lines.length > 0 && (
        <ol ref={list} aria-label="What the agent did" className="max-h-36 shrink-0 space-y-1.5 overflow-y-auto border-t border-border px-3 py-2 text-xs text-text-muted">
          {lines.map(a => (
            <li key={`${a.session_id}:${a.n}`}>
              <BrowserActionLine action={a} dense shot={shotFor(a)} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

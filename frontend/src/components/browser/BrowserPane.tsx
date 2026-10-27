'use client';

import { useEffect, useRef, useState } from 'react';
import { Globe, Square, X } from 'lucide-react';

import { Button, IconButton, Spinner } from '@/components/ui';
import { cn } from '@/lib/cn';

import { BrowserActionLine } from './BrowserActionLine';
import { actionCount, shortUrl, type BrowserAction, type BrowserHost } from './model';
import { useBrowserFrames, type FrameMemory } from './useBrowserFrames';

/**
 * The agent's browser, live, beside the chat or on a run's page (BRW2, D330).
 *
 * The page arrives as the component's screencast, one JPEG per repaint, on an
 * `<img>` letterboxed to the pane. A banner says who holds the wheel. Stop ends
 * the agent's turn or run and closes its browser; closing the pane only stops
 * watching. Once the browser closes its last page stays, dimmed, with what the
 * agent did under it (Naveen, 2026-10-27). In v1 the person watches; taking over
 * comes with the wheel (DESIGN §2.4).
 */
export function BrowserPane({
  host, sessionId, agentName, actions, memory, closedBefore = false, onClosed, onHide,
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
  className?: string;
  style?: React.CSSProperties;
  /** The pane's own edge, such as its resize handle. */
  children?: React.ReactNode;
}) {
  const { frame, closed: streamClosed } = useBrowserFrames(host.framesUrl(sessionId), !closedBefore, memory, sessionId);
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

  const url = frame?.url || actions[actions.length - 1]?.url || '';
  const driver = frame?.wheel === 'person' ? 'You’re driving'
    : frame?.wheel === 'nobody' ? 'Nobody is driving' : `${agentName} is driving`;

  // The newest action stays in view as the list grows.
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [actions.length]);

  return (
    <section
      aria-label="Browser"
      className={cn('relative flex min-h-0 min-w-0 flex-col bg-bg', className)}
      style={style}
    >
      {children}
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Globe className="size-4 shrink-0 text-text-faint" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-sm text-text" title={url || undefined} translate="no">
          {url ? shortUrl(url) : 'Opening the browser…'}
        </span>
        {!closed && (
          <Button variant="ghost" size="sm" onClick={stop} loading={stopping}
                  title="Stop the agent and close its browser">
            {!stopping && <Square className="size-3.5" aria-hidden />}
            Stop
          </Button>
        )}
        {onHide && <IconButton icon={X} label="Close the browser pane" size="sm" onClick={onHide} />}
      </header>

      <div role="status" className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-text-muted">
        {closed ? (
          <span>Browser closed · {actionCount(actions.length)}</span>
        ) : (
          <>
            <span aria-hidden className="relative flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-machine opacity-60 motion-reduce:hidden" />
              <span className="relative inline-flex size-2 rounded-full bg-machine" />
            </span>
            <span>{driver}</span>
            {stopFailed && <span className="text-danger">· It could not be stopped. Try again.</span>}
          </>
        )}
      </div>

      <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-surface-2 p-3">
        {frame ? (
          // eslint-disable-next-line @next/next/no-img-element -- a screencast frame, replaced on every repaint
          <img
            src={`data:image/jpeg;base64,${frame.jpeg_b64}`}
            alt={url ? `The page the agent sees: ${shortUrl(url)}` : 'The page the agent sees'}
            width={frame.w || undefined}
            height={frame.h || undefined}
            draggable={false}
            className={cn('h-auto max-h-full w-auto max-w-full select-none rounded-md border border-border object-contain shadow-sm',
              closed && 'opacity-50 grayscale')}
          />
        ) : closed ? (
          <p className="text-sm text-text-muted">Browser closed</p>
        ) : (
          <div className="flex items-center gap-2 text-sm text-text-muted">
            <Spinner size={16} />
            Opening the browser…
          </div>
        )}
      </div>

      {actions.length > 0 && (
        <ol ref={list} aria-label="What the agent did" className="max-h-36 shrink-0 space-y-1.5 overflow-y-auto border-t border-border px-3 py-2 text-xs text-text-muted">
          {actions.map(a => (
            <li key={`${a.session_id}:${a.n}`}>
              <BrowserActionLine action={a} dense shot={a.shot_ref ? host.shotUrl(a.session_id, a.shot_ref) : null} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

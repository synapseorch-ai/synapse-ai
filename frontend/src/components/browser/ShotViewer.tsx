'use client';

import { useState } from 'react';
import { ChevronLeft, ChevronRight, Maximize2 } from 'lucide-react';

import { Button, IconButton, Modal } from '@/components/ui';

import { actionWords, type BrowserAction } from './model';

/**
 * What the agent did in the browser, after the fact (DESIGN §2.4 "the record"; F1356 in place of the Filmstrip's small
 * cards): the screenshot kept after each of its actions, one at a time and large, at the browser's own 16:10, with its
 * line, back and next (and the arrow keys), where it is in the run ("3 of 12"), and Enlarge for the whole screen.
 * Nothing is recorded while the person drives, so their part is said, never pictured; a screenshot that is gone
 * leaves its line.
 */
export function ShotViewer({ actions, shotUrl }: {
  actions: BrowserAction[];
  shotUrl: (action: BrowserAction) => string | null;
}) {
  const [at, setAt] = useState(0);
  const [gone, setGone] = useState<Set<number>>(() => new Set());
  const [enlarged, setEnlarged] = useState(false);
  if (actions.length === 0) return null;
  const here = Math.min(at, actions.length - 1);
  const action = actions[here];
  const words = actionWords(action);
  const src = action.shot_ref && !gone.has(action.n) ? shotUrl(action) : null;
  const go = (by: number) => setAt(Math.max(0, Math.min(actions.length - 1, here + by)));
  const count = `${here + 1} of ${actions.length}`;

  const picture = (large: boolean) => src ? (
    // eslint-disable-next-line @next/next/no-img-element -- a kept JPEG from the run's record
    <img
      src={src}
      alt={`After: ${words}`}
      onError={() => setGone((prev) => new Set(prev).add(action.n))}
      className={large ? 'h-auto max-h-[78vh] w-auto max-w-full rounded-md border border-border object-contain'
                       : 'size-full object-contain'}
    />
  ) : (
    <p className="px-3 text-center text-sm text-text-faint">
      {action.actor === 'person' ? 'Nothing is recorded while you drive' : 'No screenshot for this step'}
    </p>
  );
  const controls = (
    <div className="flex items-center gap-2">
      <IconButton icon={ChevronLeft} label="Previous screenshot" size="sm" onClick={() => go(-1)} disabled={here === 0} />
      <span className="min-w-[4.5rem] text-center text-xs tabular-nums text-text-muted" aria-live="polite">{count}</span>
      <IconButton icon={ChevronRight} label="Next screenshot" size="sm" onClick={() => go(1)}
                  disabled={here === actions.length - 1} />
    </div>
  );

  return (
    <figure
      aria-label="What the agent did in the browser"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
        if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
      }}
      className="grid gap-2 overflow-hidden rounded-lg border border-border bg-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <div className="flex aspect-[16/10] w-full items-center justify-center overflow-hidden bg-surface-2">
        {picture(false)}
      </div>
      <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 pb-2">
        <span className="min-w-0 flex-1 text-xs text-text-muted [overflow-wrap:anywhere]" title={action.url || undefined}>
          <span className="text-text-faint">{action.n}.</span> {words}
        </span>
        {controls}
        {src && (
          <Button variant="ghost" size="sm" onClick={() => setEnlarged(true)} title="See it large">
            <Maximize2 className="size-3.5" aria-hidden />
            Enlarge
          </Button>
        )}
      </figcaption>
      <Modal open={enlarged} onClose={() => setEnlarged(false)} title={`${action.n}. ${words}`} size="full"
             footer={<div className="flex w-full justify-end">{controls}</div>}>
        <div className="flex w-full items-center justify-center">{picture(true)}</div>
      </Modal>
    </figure>
  );
}

'use client';

import { useState } from 'react';
import { MousePointerClick } from 'lucide-react';

import { cn } from '@/lib/cn';

import { actionWords, type BrowserAction } from './model';

/**
 * One thing the agent did in the browser, as a line with the screenshot taken
 * after it (DESIGN §2.4, "the running log"). The same line in the pane, under a
 * chat answer and in a run's log. A screenshot is kept only while its browser is
 * open (v1), so a gone one leaves the words alone.
 */
export function BrowserActionLine({ action, shot, dense = false, className }: {
  action: Pick<BrowserAction, 'action' | 'target' | 'actor' | 'url'>;
  /** The screenshot's address, or none. */
  shot?: string | null;
  /** The pane's list: a smaller thumbnail. */
  dense?: boolean;
  className?: string;
}) {
  const [gone, setGone] = useState(false);
  const words = actionWords(action);
  return (
    <div data-browser-action className={cn('flex min-w-0 items-center gap-2', className)}>
      {shot && !gone ? (
        // eslint-disable-next-line @next/next/no-img-element -- a JPEG from the component, sized here
        <img
          src={shot}
          alt=""
          loading="lazy"
          onError={() => setGone(true)}
          className={cn('shrink-0 rounded-[3px] border border-border bg-surface-2 object-cover object-top',
            dense ? 'h-7 w-11' : 'h-9 w-14')}
        />
      ) : (
        <span aria-hidden className={cn('flex shrink-0 items-center justify-center rounded-[3px] bg-surface-2 text-text-faint',
          dense ? 'h-7 w-11' : 'h-9 w-14')}>
          <MousePointerClick className="size-3.5" />
        </span>
      )}
      <span className="min-w-0 flex-1 truncate" title={action.url ? `${words}\n${action.url}` : words}>{words}</span>
    </div>
  );
}

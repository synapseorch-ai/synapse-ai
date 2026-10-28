'use client';

import { useState } from 'react';

import { actionWords, type BrowserAction } from './model';

/**
 * What the agent did in the browser, after the fact (DESIGN §2.4 "the record", F1075): the screenshot taken after
 * each of its actions, kept with the run, in order, each with its line. Nothing is recorded while the person drives,
 * so their part is a gap, never a picture. A screenshot that is gone leaves its line.
 */
export function Filmstrip({ actions, shotUrl }: {
  actions: BrowserAction[];
  shotUrl: (action: BrowserAction) => string | null;
}) {
  const [gone, setGone] = useState<Set<number>>(() => new Set());
  return (
    <ol aria-label="What the agent did in the browser" className="flex gap-3 overflow-x-auto pb-2">
      {actions.map((a) => {
        const src = a.shot_ref ? shotUrl(a) : null;
        const words = actionWords(a);
        return (
          <li key={`${a.session_id}:${a.n}`} data-browser-action className="flex w-44 shrink-0 flex-col gap-1.5">
            {src && !gone.has(a.n) ? (
              // eslint-disable-next-line @next/next/no-img-element -- a kept JPEG from the run's record
              <img
                src={src}
                alt={`After: ${words}`}
                loading="lazy"
                onError={() => setGone((prev) => new Set(prev).add(a.n))}
                className="aspect-[16/10] w-full rounded-md border border-border bg-surface-2 object-cover object-top"
              />
            ) : (
              // No `shot_ref`, nothing asked for: the person's own driving is never pictured (BRW1 keeps none).
              <div className="flex aspect-[16/10] w-full items-center justify-center rounded-md border border-dashed border-border bg-surface-2 px-2 text-center text-[11px] text-text-faint">
                {a.actor === 'agent' ? 'No picture' : 'Nothing is recorded while you drive'}
              </div>
            )}
            <span className="line-clamp-2 text-xs text-text-muted [overflow-wrap:anywhere]" title={a.url || undefined}>
              <span className="text-text-faint">{a.n}.</span> {words}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

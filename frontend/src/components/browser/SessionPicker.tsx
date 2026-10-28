'use client';

import { Checkbox } from '@/components/ui';
import { cn } from '@/lib/cn';

import { expiresIn, whose, type SiteSession } from './sessions';

/**
 * The sessions a browser starts signed in with, picked several at once but one per site (BRW2, F1138/F1140; DESIGN
 * v5-3). Grouped by the server's `site`, so the page never works out which domain a session is for: picking a second
 * session for a site replaces the first, and the server refuses two anyway (422). Used at every level a pick is made
 * (an agent, a chat, a run's start, a step); the nearer level wins on a site, which is the engine's to apply.
 */
export function SessionPicker({ label, sessions, value, onChange, libraryHref, locked, hint }: {
  label: string;
  /** The library's rows; null while they are being read. */
  sessions: SiteSession[] | null;
  value: string[];
  onChange: (ids: string[]) => void;
  libraryHref: string;
  locked?: { upgradeHref: string };
  hint?: string;
}) {
  const manage = (
    <a href={libraryHref} className="text-xs font-medium text-accent-ink underline-offset-2 hover:underline">
      Manage sessions
    </a>
  );
  if (locked) {
    return (
      <fieldset className="grid gap-1.5" disabled>
        <legend className="text-sm font-medium text-text">{label}</legend>
        <p className="text-xs text-text-muted">
          Sessions come with Solo and up.{' '}
          <a href={locked.upgradeHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">See plans</a>
        </p>
      </fieldset>
    );
  }
  const bySite = new Map<string, SiteSession[]>();
  for (const s of sessions ?? []) bySite.set(s.site, [...(bySite.get(s.site) ?? []), s]);
  const picked = new Set(value);
  const toggle = (s: SiteSession, on: boolean) => {
    // One per site: the others for this site are let go.
    const rest = value.filter(id => id !== s.id && !(bySite.get(s.site) ?? []).some(o => o.id === id));
    onChange(on ? [...rest, s.id] : rest);
  };

  return (
    <fieldset className="grid gap-2">
      <legend className="text-sm font-medium text-text">{label}</legend>
      {sessions === null ? (
        <p className="text-xs text-text-faint">Loading…</p>
      ) : bySite.size === 0 ? (
        <p className="text-xs text-text-muted">
          None kept yet. Keep a session in the Sessions library, and it can be picked here.
        </p>
      ) : (
        <div className="grid gap-2">
          {[...bySite.entries()].map(([site, rows]) => {
            const headingId = `session-site-${site.replace(/[^a-z0-9]+/gi, '-')}`;
            return (
              <div key={site} role="group" aria-labelledby={headingId}
                   className="rounded-md border border-border px-3 py-2">
                <div id={headingId} className="text-sm font-medium text-text" translate="no">{site}</div>
                <ul className="mt-1.5 grid gap-1.5">
                  {rows.map(s => {
                    const id = `session-pick-${s.id}`;
                    const words = `${whose(s)} · kept ${new Date(s.created_at * 1000).toLocaleDateString()} · ${expiresIn(s)}`;
                    return (
                      <li key={s.id} className="flex items-center gap-2">
                        <Checkbox id={id} checked={picked.has(s.id)} onChange={on => toggle(s, on)}
                                  label={`${site}: ${words}`} />
                        <label htmlFor={id} className={cn('text-xs', s.expired ? 'text-text-faint' : 'text-text-muted')}>
                          {words}
                        </label>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </div>
      )}
      {hint && <p className="text-xs text-text-muted">{hint}</p>}
      <div>{manage}</div>
    </fieldset>
  );
}

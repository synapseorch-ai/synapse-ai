'use client';

import { useState } from 'react';
import { X } from 'lucide-react';

import { Button, Field, IconButton, Input, Segmented, Select } from '@/components/ui';

import { expiresIn, mayUse, onSite, shortDate, whose, type SiteSession } from './sessions';

/**
 * What bounds a Browser agent (DESIGN §2.6, §3.1), in its own settings so no page it reads can change them: the sites
 * it may use, the sessions it starts signed in with, how many actions it may take per task, and whether it only reads or works
 * the page with the person. Stored on the agent as `browser` (the engine's model, BRW1). Synced to cloud.
 */
export type BrowserConfig = {
  sites: string[];
  /** Kept sessions it starts signed in with, one per site (DESIGN v5-3; F1138 renamed `login_ref`). */
  sessions: string[];
  action_budget: number;
  mode: 'read_only' | 'co_drive';
};

export const BROWSER_DEFAULTS: BrowserConfig = { sites: [], sessions: [], action_budget: 40, mode: 'read_only' };
export const ACTION_BUDGET = { min: 1, max: 200 } as const;

/** An agent saved before F1138 says `login_ref`: it reads as that one session, and is saved as `sessions`. */
type StoredBrowser = Partial<BrowserConfig> & { login_ref?: string | null };

export function browserConfig(value: StoredBrowser | null | undefined): BrowserConfig {
  const { login_ref, ...rest } = value ?? {};
  const sessions = rest.sessions ?? (login_ref ? [login_ref] : []);
  return { ...BROWSER_DEFAULTS, ...rest, sessions };
}

/** One site per line or comma, as a host: "https://portal.example.com/x" is "portal.example.com". */
export function parseSites(text: string): string[] {
  const hosts = text.split(/[\n,]+/).map(s => s.trim().toLowerCase()).filter(Boolean).map((s) => {
    // `*.host` covers its subdomains (BRW1's AgentBrowser); kept as written.
    if (/^\*\.[a-z0-9.-]+$/.test(s)) return s;
    try {
      return new URL(s.includes('://') ? s : `https://${s}`).hostname;
    } catch {
      return s;
    }
  });
  return [...new Set(hosts)];
}

export function BrowserSetup({ value, onChange, sessions, libraryHref, locked }: {
  value: StoredBrowser | null | undefined;
  onChange: (next: BrowserConfig) => void;
  /** The Sessions library's rows; null while they are being read. */
  sessions: SiteSession[] | null;
  libraryHref: string;
  locked?: { upgradeHref: string };
}) {
  const config = browserConfig(value);
  const [adding, setAdding] = useState('');
  const put = (edit: Partial<BrowserConfig>) => onChange({ ...config, ...edit });

  // Sessions follow the sites (Naveen 10-29): only the listed sites' sessions are kept, one per site, so a site taken
  // off takes its session, and none listed means signed in to none.
  const keepFor = (sites: string[], ids: string[]) =>
    ids.filter(id => {
      const row = (sessions ?? []).find(s => s.id === id);
      return !row || sites.some(host => onSite(host, row.site));
    });
  const setSites = (sites: string[]) => put({ sites, sessions: sites.length ? keepFor(sites, config.sessions) : [] });
  const addSites = () => {
    const more = parseSites(adding).filter(h => !config.sites.includes(h));
    if (more.length) setSites([...config.sites, ...more]);
    setAdding('');
  };
  const pick = (host: string, id: string) => {
    const others = config.sessions.filter(sid => {
      const row = (sessions ?? []).find(s => s.id === sid);
      return !row || !onSite(host, row.site);
    });
    put({ sessions: id === 'none' ? others : [...others, id] });
  };

  return (
    <div className="grid gap-5">
      <Field
        label="How it works the page"
        composite
        hint={config.mode === 'read_only'
          ? 'It reads and clicks through pages, and nothing it does can send anything: forms, orders and messages are stopped in the browser.'
          : 'It works the page with you. You type passwords, card details and codes. It presses Submit or Send only when you’ve asked it to in your own words, and every payment is yours to press.'}
      >
        <Segmented
          label="How it works the page"
          value={config.mode}
          onChange={mode => put({ mode })}
          options={[
            { value: 'read_only', label: 'Reads only' },
            { value: 'co_drive', label: 'Works with you' },
          ]}
        />
      </Field>

      <Field
        label="Sites it may use"
        composite
        hint={<>Listed, it cannot open a page anywhere else, even when a page links there, and starts signed in to each
          with the session beside it. Blank: it may read any public site, signed in to none, and asks you before typing
          on one. <a href={libraryHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">Manage sessions</a></>}
      >
        <div className="grid gap-2">
          {config.sites.length > 0 && (
            <ul aria-label="Sites it may use" className="divide-y divide-border rounded-md border border-border">
              {config.sites.map(host => (
                <SiteRow key={host} host={host} sessions={sessions} picked={config.sessions} libraryHref={libraryHref}
                         locked={locked} onPick={id => pick(host, id)}
                         onRemove={() => setSites(config.sites.filter(h => h !== host))} />
              ))}
            </ul>
          )}
          <div className="flex gap-2">
            <Input
              value={adding}
              onChange={e => setAdding(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addSites(); } }}
              placeholder="portal.saltmarsh.example, or *.example.com for its subdomains"
              aria-label="Add a site"
              spellCheck={false}
              translate="no"
              className="min-w-0 flex-1"
            />
            <Button variant="secondary" onClick={addSites} disabled={!adding.trim()}>Add</Button>
          </div>
        </div>
      </Field>

      <div className="grid gap-x-6 gap-y-5 @xl:grid-cols-2">
        <Field label="Actions per task" hint="The most it does in the page for one request. At the limit it stops and says so.">
          <Input
            type="number"
            inputMode="numeric"
            min={ACTION_BUDGET.min}
            max={ACTION_BUDGET.max}
            value={config.action_budget}
            onChange={e => {
              const n = parseInt(e.target.value, 10);
              if (Number.isFinite(n)) put({ action_budget: Math.min(ACTION_BUDGET.max, Math.max(ACTION_BUDGET.min, n)) });
            }}
          />
        </Field>
      </div>
    </div>
  );
}

/**
 * One site it may use, and how it is signed in there: a usable session picked (the person's own, or one shared with
 * them), not signed in, or, when its only session has expired, Sign in again. Never another site's session.
 */
function SiteRow({ host, sessions, picked, libraryHref, locked, onPick, onRemove }: {
  host: string;
  sessions: SiteSession[] | null;
  picked: string[];
  libraryHref: string;
  locked?: { upgradeHref: string };
  onPick: (id: string) => void;
  onRemove: () => void;
}) {
  const here = (sessions ?? []).filter(s => onSite(host, s.site));
  const usable = here.filter(s => mayUse(s) && !s.expired);
  const current = usable.find(s => picked.includes(s.id));
  const expired = here.some(s => s.mine && s.expired);
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2">
      <span className="min-w-0 grow basis-48 text-sm text-text [overflow-wrap:anywhere]" translate="no">{host}</span>
      <div className="flex min-w-0 items-center gap-2 text-xs text-text-muted">
        {locked ? (
          <span>Sessions come with Solo and up. <a href={locked.upgradeHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">See plans</a></span>
        ) : sessions === null ? (
          <span className="text-text-faint">Loading…</span>
        ) : usable.length ? (
          <Select
            size="sm"
            aria-label={`Session for ${host}`}
            value={current?.id ?? 'none'}
            onChange={onPick}
            className="w-64 max-w-full"
            options={[
              { value: 'none', label: 'Not signed in' },
              ...usable.map(s => ({ value: s.id, label: `${whose(s)} · kept ${shortDate(s.created_at)} · ${expiresIn(s)}` })),
            ]}
          />
        ) : expired ? (
          <span>Expired. <a href={libraryHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">Sign in again</a></span>
        ) : (
          <span>Not signed in. <a href={libraryHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">Keep a session</a></span>
        )}
        <IconButton icon={X} label={`Remove ${host}`} size="sm" onClick={onRemove} />
      </div>
    </li>
  );
}

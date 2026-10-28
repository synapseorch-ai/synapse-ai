'use client';

import { useState } from 'react';

import { Field, Input, Segmented, Textarea } from '@/components/ui';

import { SessionPicker } from './SessionPicker';
import type { SiteSession } from './sessions';

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
  // The text as typed, so a half-typed line is not tidied away under the cursor; saved as hosts.
  const [sitesText, setSitesText] = useState(config.sites.join('\n'));
  const put = (edit: Partial<BrowserConfig>) => onChange({ ...config, ...edit });

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
        hint="One per line, as an address or *.example.com for its subdomains. Listed, it cannot open a page anywhere else, even when a page links there. Blank: it may read any public site, and asks you before typing on one. List sites to let a scheduled run type on them."
      >
        <Textarea
          rows={3}
          value={sitesText}
          placeholder={'portal.saltmarsh.example\ntracking.carrier.example'}
          onChange={e => { setSitesText(e.target.value); put({ sites: parseSites(e.target.value) }); }}
          onBlur={() => setSitesText(config.sites.join('\n'))}
          spellCheck={false}
          translate="no"
        />
      </Field>

      <SessionPicker
        label="Sessions it starts with"
        sessions={sessions}
        value={config.sessions}
        onChange={ids => put({ sessions: ids })}
        libraryHref={libraryHref}
        locked={locked}
        hint="It starts signed in to these sites, one session per site. When one has expired, it asks you to sign in again."
      />

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

'use client';

import { useState } from 'react';

import { Field, Input, Segmented, Select, Textarea } from '@/components/ui';

/**
 * What bounds a Browser agent (DESIGN §2.6, §3.1), in its own settings so no page it reads can change them: the sites
 * it may use, the saved login it starts with, how many actions it may take per task, and whether it only reads or works
 * the page with the person. Stored on the agent as `browser` (the engine's model, BRW1). Synced to cloud.
 */
export type BrowserConfig = {
  sites: string[];
  login_ref: string | null;
  action_budget: number;
  mode: 'read_only' | 'co_drive';
};

export const BROWSER_DEFAULTS: BrowserConfig = { sites: [], login_ref: null, action_budget: 40, mode: 'read_only' };
export const ACTION_BUDGET = { min: 1, max: 200 } as const;

/** A saved login the agent may start with (Settings → Website logins). */
export type LoginChoice = { id: string; sites: string[] };

export function browserConfig(value: Partial<BrowserConfig> | null | undefined): BrowserConfig {
  return { ...BROWSER_DEFAULTS, ...(value ?? {}) };
}

/** One site per line or comma, as a host: "https://portal.example.com/x" is "portal.example.com". */
export function parseSites(text: string): string[] {
  const hosts = text.split(/[\n,]+/).map(s => s.trim().toLowerCase()).filter(Boolean).map((s) => {
    try {
      return new URL(s.includes('://') ? s : `https://${s}`).hostname;
    } catch {
      return s;
    }
  });
  return [...new Set(hosts)];
}

export function BrowserSetup({ value, onChange, logins }: {
  value: Partial<BrowserConfig> | null | undefined;
  onChange: (next: BrowserConfig) => void;
  /** The saved logins it may start with; none while they are being read. */
  logins: LoginChoice[];
}) {
  const config = browserConfig(value);
  // The text as typed, so a half-typed line is not tidied away under the cursor; saved as hosts.
  const [sitesText, setSitesText] = useState(config.sites.join('\n'));
  const put = (edit: Partial<BrowserConfig>) => onChange({ ...config, ...edit });
  const known = logins.some(l => l.id === config.login_ref);

  return (
    <div className="grid gap-5">
      <Field
        label="How it works the page"
        composite
        hint={config.mode === 'read_only'
          ? 'It reads and clicks through pages, and nothing it does can send anything: forms, orders and messages are stopped in the browser.'
          : 'It works the page with you. You type passwords, card details and codes, and you press Submit yourself: a send it tries is held and handed to you.'}
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
        hint="One per line. It cannot open a page anywhere else, even when a page links there. Blank: any public site, to read."
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

      <div className="grid gap-x-6 gap-y-5 @xl:grid-cols-2">
        <Field
          label="Saved login"
          hint={logins.length === 0
            ? 'None saved yet. Log in to a site in the browser and keep the login; it appears in Settings → Website logins.'
            : 'It starts logged in with this. When the login has ended, it asks you to log in again.'}
        >
          <Select
            value={config.login_ref && known ? config.login_ref : '__none__'}
            onChange={v => put({ login_ref: v === '__none__' ? null : v })}
            options={[
              { value: '__none__', label: 'None' },
              ...logins.map(l => ({ value: l.id, label: l.sites.join(', ') })),
            ]}
            aria-label="Saved login"
          />
        </Field>

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

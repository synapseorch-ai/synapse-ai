'use client';
/*
 * The Sessions library (BRW2, D330, F1138/F1139; DESIGN v5-1, v5-2, v5.1): each site the person's agents stay signed
 * in to, kept as the site's session and never the password. One list: the site, whose it is, when it was last used and
 * when it expires, and Remove, which says first what picks it. "Keep me signed in to a site" opens a browser for the
 * person alone, and the consent card there keeps the session; an expired one offers Sign in again for its site.
 * Someone else's session shows that it exists (v5-7) but is theirs to remove. Synced to cloud: the routes are the
 * host's, Share is cloud's (`canShare`), and a plan without sessions says where they come with (`locked`).
 */
import { useCallback, useEffect, useState } from 'react';
import { Globe } from 'lucide-react';

import { Button, ConfirmDialog, EmptyState, ErrorNote, Field, Input, Modal } from '@/components/ui';

import { BrowserPane } from './BrowserPane';
import { parseSites } from './BrowserSetup';
import type { BrowserHost } from './model';
import {
  expiresIn, finishSignIn, lastUsed, listSessions, removeSession, startSignIn, usesWords, whose,
  type SessionsHost, type SiteSession,
} from './sessions';

export function SessionsLibrary({ host, browserHost, onShare }: {
  host: SessionsHost;
  /** The sign-in browser's frames and shots. */
  browserHost: BrowserHost;
  /** Cloud's Share, for a session of the person's own; shown only with `host.canShare`. */
  onShare?: (session: SiteSession) => void;
}) {
  const [sessions, setSessions] = useState<SiteSession[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [removing, setRemoving] = useState<SiteSession | null>(null);
  const [busy, setBusy] = useState(false);
  // "Keep me signed in to a site": which site, then the browser for the person alone.
  const [asking, setAsking] = useState(false);
  const [siteText, setSiteText] = useState('');
  const [startFailed, setStartFailed] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [signing, setSigning] = useState<{ id: string; site: string } | null>(null);
  const [finishing, setFinishing] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSessions(await listSessions(host));
      setFailed(null);
    } catch (e) {
      setFailed(`The sessions could not be read (${e instanceof Error ? e.message : String(e)}).`);
      setSessions(prev => prev ?? []);
    }
  }, [host]);

  useEffect(() => { if (!host.locked) load(); }, [load, host.locked]);

  const open = async (site: string): Promise<string | null> => {
    const opened = await startSignIn(host, site);
    if ('refused' in opened) return opened.refused;
    setRefused(null);
    setSigning({ id: opened.browserSessionId, site });
    return null;
  };

  const startFromDialog = async () => {
    const [site] = parseSites(siteText);
    if (!site) { setStartFailed('Name the site to sign in to.'); return; }
    setStarting(true);
    setStartFailed(null);
    const why = await open(site);
    setStarting(false);
    if (why) { setStartFailed(why); return; }
    setAsking(false);
    setSiteText('');
  };

  const signInAgain = async (s: SiteSession) => {
    const why = await open(s.site);
    if (why) setFailed(why);
  };

  const finish = async (keep: boolean) => {
    if (!signing) return;
    setFinishing(true);
    try {
      const done = await finishSignIn(host, signing.id, keep);
      if ('refused' in done) {
        // Nothing is signed in yet: the browser stays open, and the card says so.
        setRefused(done.refused);
        return;
      }
      setSigning(null);
      load();
    } catch {
      setSigning(null);
      setFailed('The browser could not be closed. It closes by itself after 10 minutes.');
      load();
    } finally {
      setFinishing(false);
    }
  };

  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    const ok = await removeSession(host, removing.id);
    setBusy(false);
    if (ok) setSessions(prev => (prev ?? []).filter(s => s.id !== removing.id));
    else setFailed('That session could not be removed. Try again.');
    setRemoving(null);
  };

  if (host.locked) {
    return (
      <EmptyState title="Sessions come with Solo and up.">
        A session keeps your agents signed in to a site, so a run that needs it starts signed in.{' '}
        <a href={host.locked.upgradeHref} className="font-medium text-accent-ink underline-offset-2 hover:underline">See plans</a>
      </EmptyState>
    );
  }

  const uses = removing ? usesWords(removing.uses) : '';
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-prose text-sm text-text-muted">
          A session keeps your agents signed in to a site, so a run that needs the site starts signed in. Synapse
          keeps the site’s session, never your password: you type that yourself, and agents never see it. A session
          expires in 30 days at most, or sooner when the site ends it.
        </p>
        <Button size="sm" onClick={() => { setAsking(true); setStartFailed(null); }}>Keep me signed in to a site</Button>
      </div>
      {failed && <ErrorNote>{failed}</ErrorNote>}
      {sessions === null ? (
        <p className="py-4 text-sm text-text-faint">Loading…</p>
      ) : sessions.length === 0 ? (
        <EmptyState title="No sessions yet.">Keep yourself signed in to a site, and your agents can use it.</EmptyState>
      ) : (
        <ul aria-label="Sessions" className="divide-y divide-border rounded-md border border-border">
          {sessions.map(s => {
            const used = usesWords(s.uses);
            return (
              <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3 hover:bg-row-hover">
                <Globe className="size-4 shrink-0 text-text-faint" aria-hidden />
                {/* Its own 14rem before the buttons: on a phone they go under it, and the site reads whole. */}
                <div className="min-w-0 grow basis-56">
                  <div className="text-sm font-medium text-text [overflow-wrap:anywhere]" translate="no">{s.site}</div>
                  <div className="mt-0.5 text-xs text-text-faint">
                    {[whose(s), lastUsed(s.last_used_at), expiresIn(s), used].filter(Boolean).join(' · ')}
                  </div>
                </div>
                <div className="ml-auto flex shrink-0 items-center gap-1">
                {s.expired && s.mine && (
                  <Button variant="secondary" size="sm" className="shrink-0" onClick={() => signInAgain(s)}
                          aria-label={`Sign in again to ${s.site}`}>
                    Sign in again
                  </Button>
                )}
                {host.canShare && s.mine && onShare && (
                  <Button variant="ghost" size="sm" className="shrink-0" onClick={() => onShare(s)}
                          aria-label={`Share the session for ${s.site}`}>
                    Share
                  </Button>
                )}
                {s.mine && (
                  <Button variant="ghost" size="sm" className="shrink-0 text-text-muted hover:text-danger"
                          onClick={() => setRemoving(s)} aria-label={`Remove the session for ${s.site}`}>
                    Remove
                  </Button>
                )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <Modal
        open={asking}
        onClose={() => setAsking(false)}
        title="Keep me signed in to a site"
        description="A browser opens here for you alone: no agent, and nothing is recorded. Sign in as you normally do, then keep the session."
        footer={(
          <>
            <Button variant="secondary" onClick={() => setAsking(false)}>Cancel</Button>
            <Button onClick={startFromDialog} loading={starting}>Open the browser</Button>
          </>
        )}
      >
        <Field label="Site" hint="Its address, such as portal.saltmarsh.example." error={startFailed ?? undefined}>
          <Input value={siteText} onChange={e => setSiteText(e.target.value)} spellCheck={false} translate="no"
                 placeholder="portal.saltmarsh.example" autoFocus
                 onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); startFromDialog(); } }} />
        </Field>
      </Modal>

      {signing && (
        <div role="dialog" aria-modal="true" aria-label="Keep me signed in to a site" className="fixed inset-0 z-50 bg-scrim/60 p-3 md:p-8">
          <BrowserPane
            host={browserHost}
            sessionId={signing.id}
            agentName="You"
            actions={[]}
            signIn={{ site: signing.site, onFinish: finish, busy: finishing, refused }}
            onClosed={() => setSigning(null)}
            className="h-full overflow-hidden rounded-xl border border-border shadow-elevated"
          />
        </div>
      )}

      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={remove}
        busy={busy}
        title="Remove this session?"
        description={removing && (
          <>{uses ? <>{uses}: each will ask you to sign in to {removing.site} again. </> : null}The site itself
            is not told, so your session there may stay open until it ends.</>
        )}
        confirmLabel="Remove"
      />
    </div>
  );
}

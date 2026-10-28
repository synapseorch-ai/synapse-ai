"use client";
/*
 * Settings › Website logins (BRW2, D330, F1079): the sites a person logged in to in the browser inside Synapse, kept
 * so a run can start logged in. A login is the site's session, not the password (agents never see or type one), kept
 * encrypted for 30 days at most or until the site ends it (DESIGN §3.4). The page shows what each is for and when it
 * ends, and Forget; the session itself never reaches the page. "Log in to a site" opens the browser for the person
 * alone, and keeping the login there saves it here.
 */
import { useCallback, useEffect, useState } from 'react';
import { Globe } from 'lucide-react';
import { BrowserPane, parseSites } from '@/components/browser';
import { Button, ConfirmDialog, EmptyState, ErrorNote, Field, Modal, Section, Textarea } from '@/components/ui';
import { OSS_BROWSER_HOST } from '@/lib/browserHost';
import { FORM } from './form';

type WebsiteLogin = {
    id: string;
    owner: string;
    sites: string[];
    created_at: number;
    expires_at: number;
};

const DAY_MS = 86_400_000;

/** "Ends today", "Ends tomorrow", "Ends in 12 days". */
export function endsIn(expiresAt: number, now = Date.now()): string {
    const days = Math.floor((expiresAt * 1000 - now) / DAY_MS);
    return days <= 0 ? 'Ends today' : days === 1 ? 'Ends tomorrow' : `Ends in ${days} days`;
}

export const WebsiteLoginsTab = () => {
    const [logins, setLogins] = useState<WebsiteLogin[] | null>(null);
    const [failed, setFailed] = useState<string | null>(null);
    const [forgetting, setForgetting] = useState<WebsiteLogin | null>(null);
    const [busy, setBusy] = useState(false);
    // "Log in to a site": which sites, then the browser for the person alone (BRW1's login sessions).
    const [asking, setAsking] = useState(false);
    const [sitesText, setSitesText] = useState('');
    const [startFailed, setStartFailed] = useState<string | null>(null);
    const [starting, setStarting] = useState(false);
    const [session, setSession] = useState<{ id: string; sites: string[] } | null>(null);
    const [finishing, setFinishing] = useState(false);

    const startLogin = async () => {
        const sites = parseSites(sitesText);
        if (!sites.length) { setStartFailed('Name the site to log in to.'); return; }
        setStarting(true);
        setStartFailed(null);
        const opened = await OSS_BROWSER_HOST.startLogin!(sites);
        setStarting(false);
        if ('refused' in opened) { setStartFailed(opened.refused); return; }
        setAsking(false);
        setSitesText('');
        setSession({ id: opened.sessionId, sites });
    };

    const finishLogin = async (keep: boolean) => {
        if (!session) return;
        setFinishing(true);
        try {
            const kept = await OSS_BROWSER_HOST.finishLogin!(session.id, keep);
            if (keep && !kept) setFailed('Nothing was kept: the site set no login in that browser. Log in, then keep it.');
        } catch {
            setFailed('The browser could not be closed. It closes by itself after 10 minutes.');
        } finally {
            setFinishing(false);
            setSession(null);
            load();
        }
    };

    const load = useCallback(async () => {
        try {
            const res = await fetch('/api/website-logins');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            setLogins((await res.json()).logins ?? []);
            setFailed(null);
        } catch (e) {
            setFailed(`The logins could not be read (${e instanceof Error ? e.message : String(e)}).`);
            setLogins(prev => prev ?? []);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const forget = async () => {
        if (!forgetting) return;
        setBusy(true);
        const res = await fetch(`/api/website-logins/${encodeURIComponent(forgetting.id)}`, { method: 'DELETE' })
            .catch(() => null);
        setBusy(false);
        if (res && (res.ok || res.status === 404)) {
            setLogins(prev => (prev ?? []).filter(l => l.id !== forgetting.id));
            setForgetting(null);
        } else {
            setFailed('That login could not be forgotten. Try again.');
            setForgetting(null);
        }
    };

    return (
        <div className="space-y-8">
            <Section
                variant="plain"
                collapsible={false}
                title="Saved logins"
                actions={<Button size="sm" onClick={() => { setAsking(true); setStartFailed(null); }}>Log in to a site</Button>}
            >
                <div className={`${FORM} space-y-4`}>
                    <p className="text-sm text-text-muted">
                        When you log in to a site in the browser inside Synapse, its session can be kept here, so a run
                        that needs the site starts logged in. Agents never see or type your password: you type it, and
                        what is kept is the site&apos;s session, encrypted, for 30 days at most or until the site ends
                        it. A run whose login has ended asks you to log in again.
                    </p>
                    {failed && <ErrorNote>{failed}</ErrorNote>}
                    {logins === null ? (
                        <p className="py-4 text-sm text-text-faint">Loading…</p>
                    ) : logins.length === 0 ? (
                        <EmptyState title="No saved logins.">Log in to a site to keep its login for runs.</EmptyState>
                    ) : (
                        <ul aria-label="Saved logins" className="divide-y divide-border rounded-md border border-border">
                            {logins.map(l => (
                                <li key={l.id} className="flex items-center gap-3 px-4 py-3 hover:bg-row-hover">
                                    <Globe className="size-4 shrink-0 text-text-faint" aria-hidden />
                                    <div className="min-w-0 flex-1">
                                        <div className="truncate text-sm font-medium text-text" translate="no">{l.sites.join(', ')}</div>
                                        <div className="mt-0.5 text-xs text-text-faint">
                                            Saved {new Date(l.created_at * 1000).toLocaleDateString()} · {endsIn(l.expires_at)}
                                        </div>
                                    </div>
                                    <Button variant="ghost" size="sm" className="shrink-0 text-text-muted hover:text-danger"
                                            onClick={() => setForgetting(l)} aria-label={`Forget the login for ${l.sites.join(', ')}`}>
                                        Forget
                                    </Button>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </Section>

            <Modal
                open={asking}
                onClose={() => setAsking(false)}
                title="Log in to a site"
                description="A browser opens here for you alone: no agent, nothing recorded. Log in as you normally do, then keep the login."
                footer={(
                    <>
                        <Button variant="secondary" onClick={() => setAsking(false)}>Cancel</Button>
                        <Button onClick={startLogin} loading={starting}>Open the browser</Button>
                    </>
                )}
            >
                <Field label="Site" hint="Its address, such as portal.saltmarsh.example. One per line for a login that spans sites." error={startFailed ?? undefined}>
                    <Textarea rows={2} value={sitesText} onChange={e => setSitesText(e.target.value)} spellCheck={false}
                              translate="no" placeholder="portal.saltmarsh.example" autoFocus />
                </Field>
            </Modal>

            {session && (
                <div role="dialog" aria-modal="true" aria-label="Log in to a site" className="fixed inset-0 z-50 bg-scrim/60 p-3 md:p-8">
                    <BrowserPane
                        host={OSS_BROWSER_HOST}
                        sessionId={session.id}
                        agentName="You"
                        actions={[]}
                        login={{ sites: session.sites, onFinish: finishLogin, busy: finishing }}
                        onClosed={() => setSession(null)}
                        className="h-full overflow-hidden rounded-xl border border-border shadow-elevated"
                    />
                </div>
            )}

            <ConfirmDialog
                open={forgetting !== null}
                onClose={() => setForgetting(null)}
                onConfirm={forget}
                busy={busy}
                title="Forget this login?"
                description={forgetting && (
                    <>Runs that use {forgetting.sites.join(', ')} will ask you to log in again. The site itself is not
                        told, so your session there may still be open until it ends.</>
                )}
                confirmLabel="Forget"
            />
        </div>
    );
};

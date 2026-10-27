"use client";
/*
 * Settings › Website logins (BRW2, D330, F1079): the sites a person logged in to in the browser inside Synapse, kept
 * so a run can start logged in. A login is the site's session, not the password (agents never see or type one), kept
 * encrypted for 30 days at most or until the site ends it (DESIGN §3.4). The page shows what each is for and when it
 * ends, and Forget; the session itself never reaches the page.
 */
import { useCallback, useEffect, useState } from 'react';
import { Globe } from 'lucide-react';
import { Button, ConfirmDialog, EmptyState, ErrorNote, Section } from '@/components/ui';
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
            <Section variant="plain" collapsible={false} title="Saved logins">
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
                        <EmptyState title="No saved logins.">Logins you keep from the browser appear here.</EmptyState>
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

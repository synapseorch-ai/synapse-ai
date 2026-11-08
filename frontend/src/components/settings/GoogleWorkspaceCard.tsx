'use client';
/*
 * Google Workspace, at the top of Apps (M2f, Naveen's call: as cloud's Apps
 * page has it). OSS connects Google in Settings → Integrations, with the
 * workspace's own OAuth client, so this card says how things stand and leads
 * there; it starts no sign-in of its own.
 */
import { useEffect, useState } from 'react';
import Link from 'next/link';

import { AppMark, buttonStyles } from '@/components/ui';

type GoogleState = { is_connected?: boolean; user_email?: string | null; has_credentials?: boolean };

export function GoogleWorkspaceCard() {
    const [state, setState] = useState<GoogleState | null>(null);

    useEffect(() => {
        let live = true;
        fetch('/api/config')
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (live) setState(d ?? {}); })
            .catch(() => { if (live) setState({}); });
        return () => { live = false; };
    }, []);

    const connected = !!state?.is_connected;
    return (
        <section
            aria-labelledby="google-card-title"
            data-google-card=""
            className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-3 rounded-xl border border-border bg-surface p-4 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:p-5"
        >
            <AppMark name="Google" imgSrc="/google-icon.svg" size="lg" />
            <div className="min-w-0">
                <h2 id="google-card-title" className="text-[15px] font-semibold tracking-[-0.01em] text-text">Google Workspace</h2>
                <p className="mt-0.5 text-sm text-text-muted">
                    {connected
                        ? <>Gmail, Calendar, Drive, Sheets and Docs are available to your agents{state?.user_email ? <>, as <span className="font-medium text-text">{state.user_email}</span></> : null}.</>
                        : 'Send Gmail, manage Calendar, and work in Drive, Sheets and Docs. Connect a Google account in Integrations.'}
                </p>
            </div>
            <div className="col-span-2 flex items-center gap-3 sm:col-span-1 sm:justify-end">
                {state === null ? null : connected ? (
                    <>
                        <span className="inline-flex items-center gap-2 text-sm font-medium text-success">
                            <span aria-hidden className="size-2 rounded-full bg-success" />
                            Connected
                        </span>
                        <Link href="/settings/workspace" className={buttonStyles({ variant: 'ghost', size: 'sm' })}>
                            Manage
                        </Link>
                    </>
                ) : (
                    <Link href="/settings/workspace" className={buttonStyles({ variant: 'secondary', size: 'sm' })}>
                        Open Integrations
                    </Link>
                )}
            </div>
        </section>
    );
}

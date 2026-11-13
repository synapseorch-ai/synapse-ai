'use client';

/*
 * The way to what Synapse AI just built (F1577; Naveen 11-13): a new workflow's page, where Run is, or Chat with the
 * agent it made. It sits under the turn that built it for as long as the conversation is there, live or read back.
 *
 * When the build ends while the person is still where they asked from, the host presses it for them (`go`), once. A
 * press is a press of this link, so they are taken there exactly as their own click would take them: through a page's
 * own question when it holds unsaved work (the workflow editor and the agents workbench ask before a link leaves
 * them). Someone who went elsewhere, or put the conversation away, is left where they are, and finds it here.
 *
 * Synced into cloud with the rest of this folder; each product gives the address (`href`).
 */
import { ArrowRight, MessageSquare, Workflow } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { cn, focusRing } from '@/lib/cn';
import type { BuiltPlace } from '@/lib/builderEvents';

/** What the card says it does: open the workflow, or talk to the agent. */
export function goToLabel(place: BuiltPlace): string {
    return place.kind === 'workflow' ? `Open ${place.name}` : `Chat with ${place.name}`;
}

export function GoToCard({ place, href, go = false, onGo, onGone, className }: {
    place: BuiltPlace;
    /** The host's address for it: a workflow's page, Chat with the agent selected. */
    href: string;
    /** Press it now: the build ended live while the person was still here. Acted on once, then `onGone`. */
    go?: boolean;
    /** What the host does as the person is taken there, by a press or by `go` (the panel steps aside for a chat). */
    onGo?: () => void;
    onGone?: () => void;
    className?: string;
}) {
    const link = React.useRef<HTMLAnchorElement>(null);
    const gone = React.useRef(onGone);
    React.useEffect(() => { gone.current = onGone; });
    React.useEffect(() => {
        if (!go) return;
        link.current?.click();
        gone.current?.();
    }, [go]);

    const workflow = place.kind === 'workflow';
    const Icon = workflow ? Workflow : MessageSquare;
    return (
        <Link
            ref={link}
            href={href}
            onClick={onGo}
            aria-label={goToLabel(place)}
            data-testid="built-go-to"
            className={cn(
                'group flex min-w-0 items-center gap-3 rounded-xl border border-border bg-surface px-3.5 py-3',
                'transition-colors hover:border-accent/60 hover:bg-surface-2',
                focusRing,
                className,
            )}
        >
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-accent-subtle text-accent-ink">
                <Icon className="size-4" aria-hidden />
            </span>
            <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-sm font-medium text-text">{place.name}</span>
                <span className="text-xs text-text-faint">
                    {workflow ? (place.made ? 'New workflow' : 'Workflow') : (place.made ? 'New agent' : 'Agent')}
                </span>
            </span>
            <span className="inline-flex shrink-0 items-center gap-1 text-[13px] font-medium text-accent-ink">
                {workflow ? 'Open' : 'Chat'}
                <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" aria-hidden />
            </span>
        </Link>
    );
}

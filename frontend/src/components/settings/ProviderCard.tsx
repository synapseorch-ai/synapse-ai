'use client';

import { ChevronDown } from 'lucide-react';
import { AppMark } from '@/components/ui';
import { cn } from '@/lib/cn';

/**
 * One provider on Settings → Models: its mark, name and state on a line that
 * opens it, and its fields below. A separate object, so it keeps its border
 * (UX1). The model providers and the rerankers are drawn by this one card, so
 * a key is entered the same way wherever it goes (Naveen, M2g — as cloud's
 * Rerankers already were).
 */
export function ProviderCard({ name, img, imgClassName, live, status, open, onToggle, children }: {
    name: string;
    /** A logo of ours; none is a theme-aware letter tile (`AppMark`). */
    img?: string;
    imgClassName?: string;
    /** Usable now: a key saved, a server running, models found. */
    live: boolean;
    /** "3 models", "No key configured", "Key saved". */
    status: string;
    open: boolean;
    onToggle: () => void;
    children: React.ReactNode;
}) {
    return (
        <div className="rounded-lg border border-border bg-surface" data-provider-card={name}>
            <button
                type="button"
                onClick={onToggle}
                aria-expanded={open}
                className={cn('flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-row-hover', open ? 'rounded-t-lg' : 'rounded-lg')}
            >
                <span className="flex min-w-0 items-center gap-3">
                    <AppMark name={name} imgSrc={img} imgClassName={imgClassName} size="sm"
                        className={live ? undefined : 'opacity-50 grayscale'} />
                    <span className="min-w-0">
                        <span className={`text-sm font-medium ${live ? 'text-text' : 'text-text-muted'}`}>{name}</span>{' '}
                        <span className="ml-2 inline-flex items-center gap-1.5 text-xs text-text-faint">
                            <span aria-hidden className={`size-1.5 rounded-full ${live ? 'bg-success' : 'bg-text-faint'}`} />
                            {status}
                        </span>
                    </span>
                </span>
                <ChevronDown className={cn('size-4 shrink-0 text-text-faint transition-transform', open && 'rotate-180')} aria-hidden />
            </button>
            {open && <div className="space-y-3 border-t border-border px-4 pb-4 pt-3">{children}</div>}
        </div>
    );
}

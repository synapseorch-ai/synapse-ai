'use client';

/*
 * The list beside a detail (M2h): the quiet row of a list and the head over the
 * open item, shared by every page that opens its first item rather than an
 * empty pane — Evals, Skills, the Vault's file, Channels and Widgets, in both
 * products. Nothing here reads an API.
 */
import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/**
 * A row of the list: the name, a quiet line under it, and one figure on the
 * right. `leading` is a small icon before the name; `actions` sit over the
 * figure on hover and focus (always shown on the open row and on a phone).
 */
export function ListRow({
    title, sub, meta, current, onClick, data, leading, actions, titleClassName,
}: {
    title: ReactNode;
    sub?: ReactNode;
    meta?: ReactNode;
    current: boolean;
    onClick: () => void;
    data?: Record<string, string>;
    leading?: ReactNode;
    actions?: ReactNode;
    titleClassName?: string;
}) {
    return (
        <li {...data} className="group relative m-0">
            <button
                type="button"
                onClick={onClick}
                aria-current={current ? 'true' : undefined}
                className={cn(
                    'grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 border-b border-border px-2.5 py-2.5 text-left transition-colors',
                    current ? 'bg-surface-2 shadow-[inset_2px_0_0_var(--color-text)]' : 'hover:bg-row-hover',
                )}
            >
                <span className={cn('truncate text-sm font-medium text-text', leading && 'flex min-w-0 items-center gap-2', titleClassName)}>
                    {leading}
                    {leading ? <span className="truncate">{title}</span> : title}
                </span>
                <span
                    className={cn(
                        'text-xs tabular-nums text-text-faint',
                        actions && 'max-md:invisible group-focus-within:invisible group-hover:invisible',
                        actions && current && 'invisible',
                    )}
                >
                    {meta}
                </span>
                {sub != null && sub !== '' && <span className="col-span-2 truncate text-xs text-text-faint">{sub}</span>}
            </button>
            {actions && (
                <div
                    className={cn(
                        'absolute right-1.5 top-1.5 z-10 flex items-center gap-0.5',
                        current ? 'flex' : 'md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100',
                    )}
                >
                    {actions}
                </div>
            )}
        </li>
    );
}

/** The head over the open item: its name, a quiet line, and its actions. */
export function DetailHead({
    title, sub, actions, leading,
}: {
    title: ReactNode;
    sub?: ReactNode;
    actions?: ReactNode;
    leading?: ReactNode;
}) {
    return (
        <div className="flex flex-wrap items-start justify-between gap-3">
            <div className={cn('min-w-0', leading && 'flex items-start gap-3')}>
                {leading}
                <div className="min-w-0">
                    <h2 className="m-0 truncate text-lg font-semibold tracking-[-0.01em] text-text">{title}</h2>
                    {sub && <p className="m-0 mt-0.5 text-[13px] text-text-muted">{sub}</p>}
                </div>
            </div>
            {actions && <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div>}
        </div>
    );
}

/**
 * The list beside the detail, on the wide frame: the list a fixed column, the
 * detail the rest; stacked under `lg`, the list first.
 */
export const LIST_DETAIL = 'grid items-start gap-8 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]';

/** On a phone a row's detail is below the list: bring it into view. */
export function showDetail(el: HTMLElement | null) {
    if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1023px)').matches)
        requestAnimationFrame(() => el?.scrollIntoView({ block: 'start', behavior: 'smooth' }));
}

'use client';
/**
 * Synapse AI's own chats, listed in its panel (TABS2, D327; F1062).
 *
 * The panel's history icon opens this in place of the conversation (Naveen, 2026-10-27): Synapse AI's chats and
 * nothing else, newest first, searched on the server. A pick carries that chat on in the panel; the back arrow
 * returns to the one it was showing. History on the chat page lists the same chats among the agents' (F1062).
 */
import { useEffect, useState } from 'react';
import { ArrowLeft, MessageSquarePlus } from 'lucide-react';

import { BadgeMark } from '@/components/tabs';
import { Mark } from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';
import { formatRelativeTime } from '@/lib/relativeTime';

/** One row of `GET /api/builder/conversations`. */
export interface AssistantChatRow {
    session_id: string;
    title: string;
    last_response: string | null;
    last_updated: string | null;
    turn_count: number;
    running: boolean;
    waiting: boolean;
}

export function AssistantChats({ currentId, onPick, onNew, onBack, listUrl = '/api/builder/conversations' }: {
    /** The chat the panel shows now, marked in the list. */
    currentId: string;
    onPick: (id: string) => void;
    onNew: () => void;
    onBack: () => void;
    /** Where the list is read: each product's own route. */
    listUrl?: string;
}) {
    const [query, setQuery] = useState('');
    const words = query.trim();
    // What was read, and for which words: the list shown is the one these words read, or nothing yet.
    const [read, setRead] = useState<{ words: string; rows: AssistantChatRow[]; failed: boolean } | null>(null);
    const loading = read?.words !== words;
    const rows = read?.rows ?? [];

    useEffect(() => {
        let live = true;
        const wait = setTimeout(() => {
            fetch(words ? `${listUrl}?q=${encodeURIComponent(words)}` : listUrl)
                .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
                .then((data) => {
                    if (live) setRead({ words, rows: Array.isArray(data?.conversations) ? data.conversations : [], failed: false });
                })
                .catch(() => { if (live) setRead({ words, rows: [], failed: true }); });
        }, words ? 250 : 0);
        return () => { live = false; clearTimeout(wait); };
    }, [words, listUrl]);

    return (
        <div className="flex min-h-0 flex-1 flex-col" data-testid="assistant-chats">
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
                <button
                    type="button"
                    onClick={onBack}
                    aria-label="Back to the chat"
                    className={cn('rounded-md p-1.5 text-text-faint transition-colors hover:bg-surface-2 hover:text-text', focusRing)}
                >
                    <ArrowLeft size={16} aria-hidden />
                </button>
                <h2 className="text-sm font-medium text-text">Your Synapse AI chats</h2>
            </div>
            <div className="shrink-0 border-b border-border px-3 py-2">
                <input
                    type="search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    aria-label="Search your Synapse AI chats"
                    placeholder="Search your Synapse AI chats"
                    autoFocus
                    className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text placeholder:text-text-faint"
                />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                {loading && rows.length === 0 && (
                    <p className="py-10 text-center text-xs text-text-faint">Loading…</p>
                )}
                {!loading && rows.length === 0 && (
                    <p className="py-10 text-center text-xs text-text-faint">
                        {read?.failed ? 'Your chats could not be read. Try again in a moment.'
                            : words ? 'Nothing matches' : 'No Synapse AI chats yet'}
                    </p>
                )}
                {rows.map((row) => {
                    const current = row.session_id === currentId;
                    return (
                        <button
                            key={row.session_id}
                            type="button"
                            onClick={() => onPick(row.session_id)}
                            aria-current={current ? 'true' : undefined}
                            className={cn(
                                'relative flex w-full items-start gap-3 border-b border-border px-4 py-3 text-left transition-colors hover:bg-row-hover',
                                current && 'bg-surface-2',
                                focusRing,
                            )}
                        >
                            {current && <span className="absolute bottom-0 left-0 top-0 w-0.5 bg-accent" aria-hidden />}
                            <Mark size={16} compact={false} className="mt-0.5 shrink-0 text-accent" aria-hidden />
                            <span className="min-w-0 flex-1">
                                <span className="flex items-start justify-between gap-2">
                                    <span className="line-clamp-2 text-[13px] leading-5 text-text">{row.title || '(no messages)'}</span>
                                    <span className="shrink-0 text-xs text-text-faint">{formatRelativeTime(row.last_updated)}</span>
                                </span>
                                {(row.running || row.waiting) ? (
                                    <span className="mt-1 flex items-center gap-1.5 text-xs text-text-muted">
                                        <BadgeMark badge={row.running ? 'working' : 'needs-you'} />
                                        {row.running ? 'Working' : 'Waiting for you'}
                                    </span>
                                ) : row.last_response ? (
                                    <span className="mt-1 block truncate text-xs text-text-faint">↩ {row.last_response}</span>
                                ) : null}
                            </span>
                        </button>
                    );
                })}
            </div>
            <div className="shrink-0 border-t border-border px-3 py-3">
                <button
                    type="button"
                    onClick={onNew}
                    className={cn(
                        'flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-surface px-3 py-2 text-sm text-text transition-colors hover:bg-surface-2',
                        focusRing,
                    )}
                >
                    <MessageSquarePlus size={14} aria-hidden />
                    New chat
                </button>
            </div>
        </div>
    );
}

'use client';

/**
 * An answer's Sources (Knowledge, B1b): each `[K#]` label the answer used,
 * resolved by the server to the document, page and heading it came from, with
 * how fresh it is and whether the words the answer quoted are really there.
 *
 * Every row is the label's anchor: the labels in the answer link here (see
 * `linkLabels`). The snippet is document text, shown as text; nothing in it is
 * ever rendered as a link or an image.
 *
 * Drawn as chips (M2c, Naveen's call): the label and the document, which opens
 * it; the page, heading, snippet, path and date open from the chip's chevron, or
 * by following the label from the answer. What is wrong is never folded away: a
 * quote not in its passage and a label no search gave stay in view.
 */
import { AlertTriangle, ChevronDown } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';

import { cn } from '@/lib/cn';
import type { KnowledgeCitation } from '@/lib/knowledge';

/** `[K3]` in an answer → a link to row 3 of its Sources, for the labels that resolved. */
export function linkLabels(text: string, citations: KnowledgeCitation[] | undefined, anchor: string): string {
    if (!citations?.length || !text) return text;
    const known = new Set(citations.map(c => c.label));
    // Outside fenced code only: a label quoted in a code block stays as written.
    return text.split(/(```[\s\S]*?```)/).map((part, i) => (i % 2 === 1 ? part : part.replace(
        /\[K(\d{1,3})\](?!\()/g,
        (label, n: string) => (known.has(label) ? `[\\[K${n}\\]](#${anchor}-k${n})` : label),
    ))).join('');
}

function pages(c: KnowledgeCitation): string {
    if (!c.page_start) return '';
    return !c.page_end || c.page_end === c.page_start ? `p. ${c.page_start}` : `pp. ${c.page_start}–${c.page_end}`;
}

function indexed(at: string | null | undefined): string {
    if (!at) return '';
    const day = new Date(at);
    return Number.isNaN(day.getTime()) ? '' : day.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function KnowledgeSources({ citations = [], unresolved = [], anchor }: {
    citations?: KnowledgeCitation[];
    unresolved?: string[];
    anchor: string;
}) {
    if (!citations.length && !unresolved.length) return null;
    const failed = citations.filter(c => c.quote_check && !c.quote_check.matched);
    return (
        <section aria-label="Sources" className="flex flex-col gap-1.5 text-xs">
            <h3 className="text-xs font-medium text-text-faint">Sources</h3>
            {citations.length > 0 && (
                <ol className="flex flex-wrap gap-1.5">
                    {citations.map(c => <SourceChip key={c.label} citation={c} anchor={anchor} />)}
                </ol>
            )}
            {failed.map(c => (
                <p key={c.label} className="flex gap-1.5 text-warning">
                    <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                    <span>The words quoted for {c.label} are not in this passage: “{c.quote_check!.quote}”</span>
                </p>
            ))}
            {unresolved.length > 0 && (
                <p className="flex gap-1.5 text-warning">
                    <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                    <span>Not from a search in this answer: <span className="font-code" translate="no">{unresolved.join(' ')}</span></span>
                </p>
            )}
        </section>
    );
}

function SourceChip({ citation: c, anchor }: { citation: KnowledgeCitation; anchor: string }) {
    const [open, setOpen] = useState(false);
    const name = c.title || c.path.split('/').pop() || c.path;
    const where = [pages(c), (c.heading_path || []).join(' › ')].filter(Boolean).join(' · ');
    const day = indexed(c.indexed_at);
    const failed = c.quote_check && !c.quote_check.matched;
    const id = `${anchor}-k${c.label.slice(2, -1)}`;
    const linkClass = 'min-w-0 truncate font-medium text-accent-ink hover:underline underline-offset-2';
    return (
        <li
            id={id}
            tabIndex={-1}
            // Following the label from the answer lands here: open what it points at.
            onFocus={(e) => { if (e.target === e.currentTarget) setOpen(true); }}
            className={cn(
                'flex min-w-0 max-w-full flex-col rounded-lg border bg-surface scroll-mt-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]',
                failed ? 'border-border-strong' : 'border-border',
                open && 'basis-full',
            )}
        >
            <div className="flex min-w-0 items-center gap-1.5 py-1 pl-2.5 pr-1">
                {failed && <AlertTriangle className="size-3.5 shrink-0 text-warning" role="img" aria-label="Quote not found" />}
                <span className="font-code text-text-faint shrink-0" translate="no">{c.label}</span>
                {c.source_url ? (
                    <a href={c.source_url} target="_blank" rel="noopener noreferrer" className={linkClass} title={name}>{name}</a>
                ) : (
                    <Link href={`/vault?path=${encodeURIComponent(c.path)}`} className={linkClass} title={name}>{name}</Link>
                )}
                <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={`${id}-detail`}
                    aria-label={`Details for ${c.label}`}
                    onClick={() => setOpen(v => !v)}
                    className="ml-0.5 flex size-6 shrink-0 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
                >
                    <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} aria-hidden />
                </button>
            </div>
            {open && (
                <div id={`${id}-detail`} className="min-w-0 space-y-0.5 border-t border-border px-2.5 py-2">
                    {where && <p className="text-text-muted">{where}</p>}
                    {c.snippet && <p className="text-text-muted line-clamp-3">{c.snippet}</p>}
                    <p className="text-text-faint break-all">
                        <span translate="no">{c.path}</span>{day && ` · indexed ${day}`}
                    </p>
                </div>
            )}
        </li>
    );
}

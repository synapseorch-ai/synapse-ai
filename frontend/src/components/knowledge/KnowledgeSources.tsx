'use client';

/**
 * An answer's Sources (Knowledge, B1b): each `[K#]` label the answer used,
 * resolved by the server to the document, page and heading it came from, with
 * how fresh it is and whether the words the answer quoted are really there.
 *
 * Every row is the label's anchor: the labels in the answer link here (see
 * `linkLabels`). The snippet is document text, shown as text; nothing in it is
 * ever rendered as a link or an image.
 */
import Link from 'next/link';

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
    return (
        <section aria-label="Sources" className="space-y-1.5 text-xs">
            <h3 className="text-[10px] font-mono uppercase tracking-widest text-text-faint">Sources</h3>
            {citations.length > 0 && (
                <ol className="space-y-2">
                    {citations.map(c => {
                        const name = c.title || c.path.split('/').pop() || c.path;
                        const where = [pages(c), (c.heading_path || []).join(' › ')].filter(Boolean).join(' · ');
                        const day = indexed(c.indexed_at);
                        const failed = c.quote_check && !c.quote_check.matched;
                        const linkClass = 'font-medium text-accent underline underline-offset-2 hover:text-accent-hover break-all';
                        return (
                            <li
                                key={c.label}
                                id={`${anchor}-k${c.label.slice(2, -1)}`}
                                tabIndex={-1}
                                className="flex gap-2 rounded-sm scroll-mt-4 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                            >
                                <span className="font-code text-text-faint shrink-0" translate="no">{c.label}</span>
                                <div className="min-w-0 space-y-0.5">
                                    <div className="flex flex-wrap gap-x-1.5">
                                        {c.source_url ? (
                                            <a href={c.source_url} target="_blank" rel="noopener noreferrer" className={linkClass}>{name}</a>
                                        ) : (
                                            <Link href={`/vault?path=${encodeURIComponent(c.path)}`} className={linkClass}>{name}</Link>
                                        )}
                                        {where && <span className="text-text-muted">{where}</span>}
                                    </div>
                                    {c.snippet && <p className="text-text-faint line-clamp-2">{c.snippet}</p>}
                                    <p className="text-text-faint break-all">
                                        <span translate="no">{c.path}</span>{day && ` · indexed ${day}`}
                                    </p>
                                    {failed && (
                                        <p className="text-amber-500">
                                            The words quoted for {c.label} are not in this passage: “{c.quote_check!.quote}”
                                        </p>
                                    )}
                                </div>
                            </li>
                        );
                    })}
                </ol>
            )}
            {unresolved.length > 0 && (
                <p className="text-amber-500">
                    Not from a search in this answer: <span className="font-code" translate="no">{unresolved.join(' ')}</span>
                </p>
            )}
        </section>
    );
}

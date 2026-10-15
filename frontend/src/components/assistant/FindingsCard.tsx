'use client';

/*
 * The AI builder's findings card (BWA1, F577).
 *
 * When someone asks for a goal the workspace has no tool for, the Researcher
 * looks in the Apps catalog, the official MCP Registry and the web, and ends
 * with this card: one row per provider, how it would connect here, what the
 * person would need, and the pages it rests on. Nothing is connected, booked
 * or bought; the person says which, and gets a plan to approve.
 */
import { ExternalLink } from 'lucide-react';

import type { ResearchFindings } from '@/lib/builderEvents';

/** How each option would connect, in words. Only "no public way in" is a warning. */
const HOW: Record<ResearchFindings['options'][number]['how'], { label: string; tone: string }> = {
    mcp: { label: 'Its own MCP server', tone: 'text-success' },
    api: { label: 'Public API', tone: 'text-accent-ink' },
    none: { label: 'No public way in', tone: 'text-warning' },
    unclear: { label: 'Could not tell', tone: 'text-text-faint' },
};

function host(url: string): string {
    try {
        return new URL(url).hostname.replace(/^www\./, '');
    } catch {
        return url;
    }
}

/** One link per site: the card names a source by its host, so two pages of one site read as a repeat. */
function bySite(urls: string[] = []): string[] {
    const seen = new Set<string>();
    return urls.filter((url) => {
        const site = host(url);
        if (seen.has(site)) return false;
        seen.add(site);
        return true;
    });
}

export function FindingsCard({ findings }: { findings: ResearchFindings }) {
    return (
        <section
            aria-label="Who can do it"
            className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface px-4 py-3.5"
        >
            <div className="flex flex-col gap-0.5">
                <span className="text-xs text-text-faint">Who can do it</span>
                {findings.goal && <span className="text-sm font-medium text-text">{findings.goal}</span>}
            </div>
            <ul className="flex flex-col divide-y divide-border">
                {findings.options.map((option, i) => {
                    const how = HOW[option.how] ?? HOW.unclear;
                    return (
                        <li key={`${option.provider}-${i}`} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0">
                            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
                                <span className="text-sm font-medium text-text">{option.provider}</span>
                                <span className={`text-xs font-medium ${how.tone}`}>{how.label}</span>
                            </div>
                            {option.detail && (
                                <span className="text-sm leading-relaxed text-text-muted">{option.detail}</span>
                            )}
                            {option.needs && (
                                <span className="text-xs text-text-muted">
                                    <span className="text-text-faint">Needs </span>
                                    {option.needs}
                                </span>
                            )}
                            {option.sources && option.sources.length > 0 && (
                                <span className="flex flex-wrap gap-x-3 gap-y-0.5">
                                    {bySite(option.sources).map((url) => (
                                        <a
                                            key={url}
                                            href={url}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline"
                                        >
                                            {host(url)}
                                            <ExternalLink className="size-3" aria-hidden />
                                        </a>
                                    ))}
                                </span>
                            )}
                        </li>
                    );
                })}
            </ul>
            <p className="border-t border-border pt-2.5 text-xs text-text-faint">
                Nothing is connected or booked yet. Say which one, and you get a plan to approve first.
            </p>
        </section>
    );
}

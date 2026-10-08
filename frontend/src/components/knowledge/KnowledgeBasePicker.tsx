'use client';

/*
 * The knowledge bases an agent answers from, with sources (B1b): one chip per
 * base, a "Removed base" chip for an id that no longer exists, at most
 * `MAX_KNOWLEDGE_BASES`. Both products' agent editors draw it (D175).
 *
 * `variant="switches"` draws one row per base with a switch (the agents
 * workbench, M2d); the chips stay the default. Both keep the same rules: the
 * cap, the "(off)" note, and the "Removed base ✕" chip for an id that is gone.
 */
import Link from 'next/link';

import { Label, Switch } from '@/components/ui';
import type { KnowledgeBase } from '@/lib/knowledge';

/** One search covers an agent's bases, so it names at most this many (`core.models.AGENT_KNOWLEDGE_MAX`). */
export const MAX_KNOWLEDGE_BASES = 10;

export function KnowledgeBasePicker({ value, onChange, bases, variant = 'chips', hideLabel = false }: {
    /** The agent's `knowledge`: base ids, in the order chosen. */
    value: string[];
    onChange: (ids: string[]) => void;
    /** Every base in the workspace. */
    bases: KnowledgeBase[];
    variant?: 'chips' | 'switches';
    /** The section around it already names it ("Knowledge bases" as its heading). */
    hideLabel?: boolean;
}) {
    const known = new Set(bases.map(kb => kb.id));
    const gone = value.filter(id => !known.has(id));
    const full = value.length >= MAX_KNOWLEDGE_BASES;
    const toggle = (id: string) => onChange(value.includes(id) ? value.filter(k => k !== id) : [...value, id]);
    const chip = (selected: boolean) => `px-3 py-1.5 text-xs font-bold border transition-colors rounded-md disabled:opacity-50 disabled:cursor-not-allowed ${selected
        ? 'bg-accent text-accent-fg border-border-strong'
        : 'bg-surface border-border text-text-muted hover:border-text-faint'}`;
    const empty = bases.length === 0 && gone.length === 0 && (
        <span className="text-xs text-text-faint">
            No knowledge bases yet — create one on the{' '}
            <Link href="/knowledge" className="underline">Knowledge</Link> page.
        </span>
    );
    const removed = gone.map(id => (
        <button key={id} type="button" aria-pressed onClick={() => toggle(id)}
            title="This knowledge base no longer exists. Click to remove it."
            className={chip(true)}>
            Removed base ✕
        </button>
    ));
    const help = (
        <p className="text-2xs text-text-faint mt-1">
            The agent searches these with <span className="font-mono">search_knowledge</span> and cites the
            passages it uses. Up to {MAX_KNOWLEDGE_BASES}. A widget&apos;s visitors reach only bases open to
            visitors, and only when the widget allows the tool.
        </p>
    );
    if (variant === 'switches') {
        return (
            <div className="space-y-2">
                {!hideLabel && <Label className="block">Knowledge bases</Label>}
                {bases.length === 0 && gone.length === 0 && (
                    <p className="text-sm text-text-muted">
                        No knowledge bases yet — create one on the{' '}
                        <Link href="/knowledge" className="underline">Knowledge</Link> page.
                    </p>
                )}
                {bases.length > 0 && (
                    <ul className="divide-y divide-border">
                        {bases.map(kb => {
                            const selected = value.includes(kb.id);
                            const notes = [kb.description, kb.status !== 'active' ? 'Switched off: not searched' : '',
                                kb.visitor_access ? 'Open to visitors' : ''].filter(Boolean).join(' · ');
                            return (
                                <li key={kb.id} className="flex items-center gap-3 py-2.5">
                                    <span className="min-w-0 flex-1">
                                        <span className="block truncate text-sm text-text">{kb.name}{kb.status !== 'active' && ' (off)'}</span>
                                        {notes && <span className="block truncate text-xs text-text-faint" title={notes}>{notes}</span>}
                                    </span>
                                    <Switch
                                        checked={selected}
                                        onChange={() => toggle(kb.id)}
                                        label={kb.name}
                                        disabled={!selected && full}
                                    />
                                </li>
                            );
                        })}
                    </ul>
                )}
                {removed.length > 0 && <div className="flex flex-wrap gap-2 pt-1">{removed}</div>}
                <p className="text-2xs text-text-faint">
                    The agent searches these with <span className="font-mono">search_knowledge</span> and cites the
                    passages it uses. Up to {MAX_KNOWLEDGE_BASES}. A widget&apos;s visitors reach only bases open to
                    visitors, and only when the widget allows the tool.
                </p>
            </div>
        );
    }
    return (
        <div className="space-y-1">
            {!hideLabel && <Label className="block">Knowledge bases</Label>}
            <div className="bg-bg border border-border p-3 flex flex-wrap gap-2 min-h-[50px] rounded-md">
                {empty}
                {bases.map(kb => {
                    const selected = value.includes(kb.id);
                    const notes = [kb.description, kb.status !== 'active' ? 'Switched off: not searched' : '',
                        kb.visitor_access ? 'Open to visitors' : ''].filter(Boolean).join(' · ');
                    return (
                        <button
                            key={kb.id}
                            type="button"
                            aria-pressed={selected}
                            title={notes || undefined}
                            disabled={!selected && full}
                            onClick={() => toggle(kb.id)}
                            className={chip(selected)}
                        >
                            {kb.name}{kb.status !== 'active' && ' (off)'} {selected && '✓'}
                        </button>
                    );
                })}
                {removed}
            </div>
            {help}
        </div>
    );
}

"use client";

/*
 * What a stranger may make an agent use, grouped the way the agents workbench
 * groups an agent's tools (M2h-2, Naveen: "group the tools like how we do it in
 * agents"): one card per source, a box that ticks the whole group, an expander
 * for its tools, in a box that scrolls so the section stays short (M2d).
 *
 * The visitor-tool list (`/api/widgets/visitor-tools`) says each tool's name and
 * category; the groups come from the agent picker's own catalogue
 * (`useCapabilities`, `/api/tools/available`). A tool the catalogue does not
 * know goes under "Other". Every tool keeps its own checkbox, id and name
 * ("Visitors can use x") and its category badge, as before.
 */
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Badge, Checkbox, SearchInput, type BadgeTone } from '@/components/ui';
import { cn } from '@/lib/cn';
import { matchesQuery } from '@/lib/search';
import { humanizeToolName } from '@/lib/toolNames';

import { useCapabilities } from './hooks/useCapabilities';

export interface VisitorToolChoice { name: string; category: string }

/** Said plainly, because the person ticking is deciding what a stranger may make
 *  their agent do. A read-only tool is a warning too: it reads *their* files. */
export const TOOL_CATEGORY: Record<string, { label: string; tone: BadgeTone }> = {
    destructive: { label: 'Changes data', tone: 'danger' },
    open_world: { label: 'Reaches outside', tone: 'warning' },
    read_only: { label: 'Reads your data', tone: 'warning' },
    undeclared: { label: 'Does not say what it does', tone: 'warning' },
};

type Group = { id: string; label: string; description?: string; tools: (VisitorToolChoice & { description: string })[] };

export function VisitorToolGroups({
    tools, ticked, onChange, idPrefix, subject,
}: {
    tools: VisitorToolChoice[];
    ticked: string[];
    onChange: (next: string[]) => void;
    /** `widget-visitor-tool` / `channel-sender-tool`: each checkbox's id is `${idPrefix}-${name}`. */
    idPrefix: string;
    /** "Visitors" / "Senders": each checkbox is named "{subject} can use {tool}". */
    subject: string;
}) {
    const { capabilities, loading } = useCapabilities();
    const [open, setOpen] = useState<Set<string>>(new Set());
    const [query, setQuery] = useState('');

    const groups = useMemo<Group[]>(() => {
        const home = new Map<string, { cap: (typeof capabilities)[number]; description: string }>();
        for (const cap of capabilities)
            for (const t of cap.toolDetails) if (!home.has(t.name)) home.set(t.name, { cap, description: t.description });
        const out = new Map<string, Group>();
        for (const tool of tools) {
            const at = home.get(tool.name);
            const id = at?.cap.id ?? '__other__';
            if (!out.has(id)) out.set(id, { id, label: at?.cap.label ?? 'Other', description: at?.cap.description, tools: [] });
            out.get(id)!.tools.push({ ...tool, description: at?.description ?? '' });
        }
        return [...out.values()].sort((a, b) => (a.id === '__other__' ? 1 : b.id === '__other__' ? -1 : a.label.localeCompare(b.label)));
    }, [capabilities, tools]);

    const q = query.trim();
    const shown = groups
        .map(g => ({
            g,
            hit: !q || matchesQuery(q, g.label, g.description),
            matched: g.tools.filter(t => !q || matchesQuery(q, t.name, humanizeToolName(t.name), t.description)),
        }))
        .filter(x => x.hit || x.matched.length > 0);

    const set = (names: string[], on: boolean) => {
        const rest = ticked.filter(n => !names.includes(n));
        onChange(on ? [...rest, ...names] : rest);
    };
    const toggleOpen = (id: string) => setOpen(prev => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
    });

    const toolRow = (tool: Group['tools'][number]) => {
        const id = `${idPrefix}-${tool.name}`;
        const category = TOOL_CATEGORY[tool.category] ?? TOOL_CATEGORY.undeclared;
        return (
            <li key={tool.name} className="flex items-start gap-2.5 py-2">
                <Checkbox
                    id={id}
                    label={`${subject} can use ${tool.name}`}
                    checked={ticked.includes(tool.name)}
                    onChange={on => set([tool.name], on)}
                    className="mt-0.5"
                />
                <label htmlFor={id} className="block min-w-0 flex-1 cursor-pointer">
                    <span className="block text-xs text-text">
                        {humanizeToolName(tool.name)}
                        <span className="ml-1.5 font-code text-2xs text-text-faint" translate="no">{tool.name}</span>
                    </span>
                    {tool.description && <span className="mt-0.5 line-clamp-2 text-xs leading-tight text-text-faint">{tool.description}</span>}
                </label>
                <Badge tone={category.tone}>{category.label}</Badge>
            </li>
        );
    };

    // Until the catalogue is in, every tool would land under "Other" for a moment.
    if (loading) return <p className="m-0 text-xs text-text-faint">Loading the agent&apos;s tools…</p>;

    return (
        <div className="grid gap-2">
            {groups.length > 8 && (
                <SearchInput value={query} onChange={setQuery} placeholder="Search tools…" className="max-w-sm" />
            )}
            {q && shown.length === 0 && <p className="m-0 text-sm text-text-faint">Nothing matches that.</p>}
            {/* A fixed height that scrolls, so the section stays short (as the agents workbench, M2d). */}
            <div className="max-h-[440px] overflow-y-auto rounded-lg border border-border bg-surface-2 p-2 modern-scrollbar [:root[data-theme=dark]_&]:bg-bg">
                <div className="grid items-start gap-2">
                    {shown.map(({ g, hit, matched }) => {
                        const list = q && !hit ? matched : g.tools;
                        const names = g.tools.map(t => t.name);
                        const on = names.filter(n => ticked.includes(n)).length;
                        const isOpen = open.has(g.id) || (!!q && !hit);
                        // Tick a group only from its box; one tool is that tool's own row.
                        if (g.tools.length === 1) {
                            return (
                                <div key={g.id} className={cn('rounded-lg border bg-surface px-3', on ? 'border-border-strong' : 'border-border')}>
                                    <ul className="m-0 list-none p-0">{toolRow(g.tools[0])}</ul>
                                </div>
                            );
                        }
                        return (
                            <div key={g.id} className={cn('rounded-lg border bg-surface', on ? 'border-border-strong' : 'border-border')}>
                                <div className="flex items-center gap-2.5 px-3 py-2" title={g.description || undefined}>
                                    <Checkbox
                                        checked={on === names.length ? true : on > 0 ? 'indeterminate' : false}
                                        onChange={() => set(names, on !== names.length)}
                                        label={`${subject} can use every ${g.label} tool`}
                                    />
                                    <button
                                        type="button"
                                        onClick={() => toggleOpen(g.id)}
                                        aria-expanded={isOpen}
                                        aria-label={`${g.label} tools, ${on} of ${names.length} on`}
                                        className="flex min-w-0 flex-1 items-center gap-2 rounded-sm text-left"
                                    >
                                        <span className="flex-1 truncate text-sm font-medium text-text">{g.label}</span>
                                        <span className="text-xs tabular-nums text-text-faint">{on}/{names.length}</span>
                                        {isOpen
                                            ? <ChevronDown className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                                            : <ChevronRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
                                    </button>
                                </div>
                                {isOpen && (
                                    <ul className="m-0 list-none divide-y divide-border border-t border-border px-3 py-0">
                                        {list.map(toolRow)}
                                    </ul>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}

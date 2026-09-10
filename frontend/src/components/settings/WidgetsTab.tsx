"use client";
import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { AlertCircle, Globe, Plus, RefreshCw, Trash2 } from 'lucide-react';

import { Button, Input, Label, Modal } from '@/components/ui';

interface WidgetRecord {
    id: string;
    name: string;
    agent_id: string;
    public_key: string;
    allowed_origins: string[];
    enabled: boolean;
    greeting: string;
    accent: string;
    title: string;
    daily_message_cap: number;
}

interface AgentOption { id: string; name: string }

const BLANK = {
    name: '',
    agent_id: '',
    allowed_origins: '',
    greeting: 'Hi! How can I help?',
    accent: '#4f46e5',
    title: '',
    daily_message_cap: 500,
};

const snippet = (base: string, key: string) =>
    `<script src="${base}/embed/v1.js" data-key="${key}" async></script>`;

/**
 * Widgets: an agent answering on a site that is not this one.
 *
 * The screen is mostly one field. `allowed_origins` is the only thing standing
 * between a key printed in public HTML and somebody else's model bill, so it is
 * the field the layout leads with and the one the empty state explains — a
 * widget with no origins listed is embeddable nowhere, deliberately, and someone
 * who does not know that will read it as broken.
 */
export const WidgetsTab = () => {
    const [widgets, setWidgets] = useState<WidgetRecord[]>([]);
    const [agents, setAgents] = useState<AgentOption[]>([]);
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState({ ...BLANK });
    const [error, setError] = useState('');
    const [copied, setCopied] = useState('');
    const [reloads, setReloads] = useState(0);

    // The install's own address, for the snippet. Read through the external-store
    // hook rather than an effect: it is a value that belongs to the browser, and
    // the server render has to agree with the first client one — `''` on both
    // sides, then the real origin.
    const base = useSyncExternalStore(
        () => () => {},
        () => window.location.origin,
        () => '',
    );

    const load = useCallback(() => setReloads(n => n + 1), []);

    useEffect(() => {
        let cancelled = false;
        void (async () => {
            const [w, a] = await Promise.all([
                fetch('/api/widgets').then(r => (r.ok ? r.json() : [])),
                fetch('/api/agents').then(r => (r.ok ? r.json() : [])),
            ]);
            if (cancelled) return;
            setWidgets(Array.isArray(w) ? w : []);
            setAgents(
                Array.isArray(a) ? a.map((x: AgentOption) => ({ id: x.id, name: x.name })) : [],
            );
        })();
        return () => { cancelled = true; };
    }, [reloads]);

    const save = async () => {
        setError('');
        const response = await fetch('/api/widgets', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ...form,
                allowed_origins: form.allowed_origins
                    .split(/[\s,]+/).map(s => s.trim()).filter(Boolean),
            }),
        });
        if (!response.ok) {
            // The 409 here is the tool-policy refusal, and its sentence names the
            // agent's behaviour and what to do about it. Showing the server's own
            // words beats paraphrasing them into something vaguer.
            setError((await response.json().catch(() => ({}))).detail ?? 'Could not save.');
            return;
        }
        setCreating(false);
        setForm({ ...BLANK });
        load();
    };

    const toggle = async (widget: WidgetRecord) => {
        await fetch(`/api/widgets/${widget.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...widget, enabled: !widget.enabled }),
        });
        load();
    };

    const rotate = async (widget: WidgetRecord) => {
        if (!confirm('Issue a new key? Every page using the current snippet stops working until it is updated.')) return;
        await fetch(`/api/widgets/${widget.id}/rotate-key`, { method: 'POST' });
        load();
    };

    const remove = async (widget: WidgetRecord) => {
        if (!confirm(`Delete "${widget.name || widget.id}"?`)) return;
        await fetch(`/api/widgets/${widget.id}`, { method: 'DELETE' });
        load();
    };

    const copy = async (widget: WidgetRecord) => {
        await navigator.clipboard.writeText(snippet(base, widget.public_key));
        setCopied(widget.id);
        setTimeout(() => setCopied(''), 2000);
    };

    return (
        <div className="space-y-4">
            <div className="flex items-start justify-between gap-4">
                <p className="text-xs text-text-faint max-w-2xl">
                    Drop an agent into your own site with one tag. The key in the snippet is public
                    by design — what decides who may use it is the list of sites you allow below.
                </p>
                <Button onClick={() => setCreating(true)} className="shrink-0">
                    <Plus className="w-3.5 h-3.5" /> New widget
                </Button>
            </div>

            {widgets.length === 0 && (
                <p className="text-xs text-text-faint border border-border rounded-md p-4">
                    No widgets yet.
                </p>
            )}

            {widgets.map(widget => (
                <div key={widget.id} className="border border-border rounded-md p-4 space-y-3">
                    <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                            <p className="text-sm font-bold text-text truncate">
                                {widget.name || widget.id}
                            </p>
                            <p className="text-2xs text-text-faint truncate">
                                {agents.find(a => a.id === widget.agent_id)?.name ?? widget.agent_id}
                            </p>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            <Button variant="secondary" onClick={() => toggle(widget)}>
                                {widget.enabled ? 'Disable' : 'Enable'}
                            </Button>
                            <Button variant="secondary" onClick={() => rotate(widget)} title="Issue a new key">
                                <RefreshCw className="w-3.5 h-3.5" />
                            </Button>
                            <Button variant="secondary" onClick={() => remove(widget)} title="Delete">
                                <Trash2 className="w-3.5 h-3.5" />
                            </Button>
                        </div>
                    </div>

                    <div className="relative group">
                        <pre className="bg-bg border border-border p-3 text-xs text-text-muted overflow-x-auto font-code rounded-md">
                            {snippet(base, widget.public_key)}
                        </pre>
                        <button
                            onClick={() => copy(widget)}
                            className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity px-2 py-1 bg-surface hover:bg-surface-2 text-text-muted text-2xs font-bold border border-border-strong rounded-md"
                        >
                            {copied === widget.id ? '✓ Copied' : 'Copy'}
                        </button>
                    </div>

                    <div className="flex items-start gap-2 text-xs">
                        <Globe className="w-3.5 h-3.5 text-text-faint shrink-0 mt-0.5" />
                        {widget.allowed_origins.length > 0 ? (
                            <span className="text-text-muted font-code">
                                {widget.allowed_origins.join('  ·  ')}
                            </span>
                        ) : (
                            <span className="text-warning">
                                No sites allowed, so this widget will not load anywhere. Add the site
                                you want to embed it on.
                            </span>
                        )}
                    </div>
                </div>
            ))}

            <Modal open={creating} onClose={() => setCreating(false)} title="New widget">
                <div className="space-y-3">
                    {error && (
                        <p className="flex items-start gap-2 text-xs text-danger border border-danger/40 rounded-md p-3">
                            <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </p>
                    )}
                    <div>
                        <Label>Name</Label>
                        <Input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                            placeholder="Support chat" />
                    </div>
                    <div>
                        <Label>Agent</Label>
                        <select
                            value={form.agent_id}
                            onChange={e => setForm({ ...form, agent_id: e.target.value })}
                            className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text"
                        >
                            <option value="">Choose an agent…</option>
                            {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
                        </select>
                    </div>
                    <div>
                        <Label>Allowed sites</Label>
                        <Input
                            value={form.allowed_origins}
                            onChange={e => setForm({ ...form, allowed_origins: e.target.value })}
                            placeholder="https://acme.com  *.acme.com"
                        />
                        <p className="text-2xs text-text-faint mt-1">
                            One per line or space-separated. <code className="font-code">*.acme.com</code> covers
                            subdomains but not <code className="font-code">acme.com</code> itself — list both if you
                            need both. Leave this empty and the widget loads nowhere.
                        </p>
                    </div>
                    <div>
                        <Label>Greeting</Label>
                        <Input value={form.greeting}
                            onChange={e => setForm({ ...form, greeting: e.target.value })} />
                    </div>
                    <div>
                        <Label>Messages per day</Label>
                        <Input
                            type="number"
                            value={String(form.daily_message_cap)}
                            onChange={e => setForm({ ...form, daily_message_cap: Number(e.target.value) })}
                        />
                        <p className="text-2xs text-text-faint mt-1">
                            The ceiling on what this widget can spend in a day. 0 means no cap.
                        </p>
                    </div>
                    <p className="text-2xs text-text-faint border-t border-border pt-3">
                        A widget has no file uploads and never asks a visitor to approve a tool call —
                        they are not a member of this workspace and cannot answer that. An agent whose
                        tool policy would stop and ask cannot be published here.
                    </p>
                    <div className="flex justify-end gap-2">
                        <Button variant="secondary" onClick={() => setCreating(false)}>Cancel</Button>
                        <Button onClick={save} disabled={!form.agent_id}>Create</Button>
                    </div>
                </div>
            </Modal>
        </div>
    );
};

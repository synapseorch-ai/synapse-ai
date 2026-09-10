"use client";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { AlertCircle, Globe, Plus, RefreshCw, Trash2, Upload, X } from 'lucide-react';

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
    launcher_label: string;
    daily_message_cap: number;
    font: string;
    theme: string;
    header_style: string;
    launcher: string;
    logo: string;
    position: string;
}

interface AgentOption { id: string; name: string }

const BLANK = {
    name: '',
    agent_id: '',
    allowed_origins: '',
    greeting: 'Hi! How can I help?',
    title: '',
    daily_message_cap: 500,
};

/** Kept in step with `core/chat_widgets/branding.py` — the API validates against
 *  the same names, so a font added in one place and not the other is refused
 *  loudly rather than silently rendering as the default. */
const PRESET_ACCENTS = [
    '#4f46e5', '#2563eb', '#0891b2', '#059669', '#ca8a04',
    '#dc2626', '#db2777', '#7c3aed', '#475569', '#18181b',
];
const FONTS = [
    { id: 'system', label: 'System', hint: "The visitor's own device font" },
    { id: 'sans', label: 'Sans', hint: 'Helvetica, Arial' },
    { id: 'serif', label: 'Serif', hint: 'Georgia, Times' },
    { id: 'rounded', label: 'Rounded', hint: 'Avenir, Nunito' },
    { id: 'mono', label: 'Mono', hint: 'Menlo, Consolas' },
];
const THEMES = [
    { id: 'auto', label: 'Auto', hint: "Follows the visitor's device" },
    { id: 'light', label: 'Light', hint: '' },
    { id: 'dark', label: 'Dark', hint: '' },
];

const snippet = (base: string, key: string) =>
    `<script src="${base}/embed/v1.js" data-key="${key}" async></script>`;

/**
 * Widgets: an agent answering on a site that is not this one.
 *
 * Two things carry this screen. `allowed_origins` is the only thing standing
 * between a key printed in public HTML and somebody else's model bill, so it is
 * the field the layout leads with and the one the empty state explains — a
 * widget with no origins listed is embeddable nowhere, deliberately, and someone
 * who does not know that will read it as broken.
 *
 * The other is the preview, which renders the **real** widget in an iframe
 * rather than a mock. A mock is quicker to build and starts drifting the first
 * time the real one changes; a preview that lies is worse than none, because it
 * is believed. Draft changes reach it by `postMessage`, so colour, logo, font
 * and text update as they are typed without a save.
 */
export const WidgetsTab = () => {
    const [widgets, setWidgets] = useState<WidgetRecord[]>([]);
    const [agents, setAgents] = useState<AgentOption[]>([]);
    const [selectedId, setSelectedId] = useState<string>('');
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState({ ...BLANK });
    const [error, setError] = useState('');
    const [copied, setCopied] = useState('');
    const [reloads, setReloads] = useState(0);
    const frame = useRef<HTMLIFrameElement>(null);

    // This install's own address, for the snippet. Read through the
    // external-store hook rather than an effect: it belongs to the browser, and
    // the server render has to agree with the first client one.
    const base = useSyncExternalStore(
        () => () => {},
        () => window.location.origin,
        () => '',
    );

    const load = useCallback(() => setReloads((n) => n + 1), []);

    useEffect(() => {
        let cancelled = false;
        void (async () => {
            const [w, a] = await Promise.all([
                fetch('/api/widgets').then((r) => (r.ok ? r.json() : [])),
                fetch('/api/agents').then((r) => (r.ok ? r.json() : [])),
            ]);
            if (cancelled) return;
            setWidgets(Array.isArray(w) ? w : []);
            setAgents(Array.isArray(a) ? a.map((x: AgentOption) => ({ id: x.id, name: x.name })) : []);
        })();
        return () => { cancelled = true; };
    }, [reloads]);

    const selected = useMemo(
        () => widgets.find((w) => w.id === selectedId) ?? null,
        [widgets, selectedId],
    );

    /**
     * Push the draft into the preview frame.
     *
     * `on_accent` is computed here as well as on the server so an unsaved colour
     * still gets readable text on it — the same relative-luminance rule as
     * `branding.readable_on`, because a preview that shows white-on-yellow for a
     * colour the server would render black-on-yellow is exactly the drift the
     * live preview exists to avoid.
     */
    const pushTheme = useCallback((widget: WidgetRecord | null) => {
        if (!widget || !frame.current?.contentWindow) return;
        frame.current.contentWindow.postMessage(
            {
                type: 'synapse:preview-theme',
                theme: {
                    accent: widget.accent,
                    on_accent: readableOn(widget.accent),
                    font: widget.font,
                    theme: widget.theme,
                    header_style: widget.header_style,
                    logo: widget.logo,
                    title: widget.title,
                    greeting: widget.greeting,
                },
            },
            window.location.origin,
        );
    }, []);

    useEffect(() => { pushTheme(selected); }, [selected, pushTheme]);

    // The frame says when it is ready; it may mount after the first push.
    useEffect(() => {
        const onMessage = (event: MessageEvent) => {
            if (event.origin !== window.location.origin) return;
            if ((event.data as { type?: string })?.type === 'synapse:preview-ready') {
                pushTheme(selected);
            }
        };
        window.addEventListener('message', onMessage);
        return () => window.removeEventListener('message', onMessage);
    }, [selected, pushTheme]);

    /** Edit the selected widget locally; the preview follows immediately. */
    const draft = (patch: Partial<WidgetRecord>) =>
        setWidgets((all) => all.map((w) => (w.id === selectedId ? { ...w, ...patch } : w)));

    /** `patch` rather than reading `selected` again: a toggle that saved from
     *  the closure would send the value it had before the toggle. */
    const save = async (patch: Partial<WidgetRecord> = {}) => {
        if (!selected) return;
        setError('');
        const response = await fetch(`/api/widgets/${selected.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...selected, ...patch }),
        });
        if (!response.ok) {
            setError((await response.json().catch(() => ({}))).detail ?? 'Could not save.');
            return;
        }
        load();
    };

    const uploadLogo = async (file: File) => {
        if (!selected) return;
        setError('');
        const body = new FormData();
        body.append('file', file);
        const response = await fetch(`/api/widgets/${selected.id}/logo`, { method: 'POST', body });
        if (!response.ok) {
            setError((await response.json().catch(() => ({}))).detail ?? 'Could not upload that.');
            return;
        }
        draft({ logo: (await response.json()).logo });
    };

    const removeLogo = async () => {
        if (!selected) return;
        await fetch(`/api/widgets/${selected.id}/logo`, { method: 'DELETE' });
        draft({ logo: '' });
    };

    const create = async () => {
        setError('');
        const response = await fetch('/api/widgets', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ...form,
                allowed_origins: form.allowed_origins.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean),
            }),
        });
        if (!response.ok) {
            // The 409 here is the tool-policy refusal, and its sentence names the
            // agent's behaviour and what to do about it. The server's own words
            // beat a paraphrase.
            setError((await response.json().catch(() => ({}))).detail ?? 'Could not save.');
            return;
        }
        setCreating(false);
        setForm({ ...BLANK });
        setSelectedId((await response.json()).id);
        load();
    };

    const act = async (path: string, method: string, confirmText?: string) => {
        if (confirmText && !confirm(confirmText)) return;
        await fetch(path, { method });
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
                    by design — what decides who may use it is the list of sites you allow.
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

            {widgets.length > 0 && (
                <div className="flex gap-2 flex-wrap">
                    {widgets.map((w) => (
                        <button
                            key={w.id}
                            onClick={() => setSelectedId(w.id)}
                            className={`px-3 py-1.5 text-xs border rounded-md ${
                                w.id === selectedId
                                    ? 'border-accent text-accent bg-accent-subtle'
                                    : 'border-border text-text-muted hover:bg-surface-2'
                            }`}
                        >
                            {w.name || 'Untitled'}
                            {w.allowed_origins.length === 0 && (
                                <span className="ml-2 text-warning">no sites</span>
                            )}
                        </button>
                    ))}
                </div>
            )}

            {selected && (
                <div className="grid grid-cols-1 lg:grid-cols-[1fr_400px] gap-5 items-start">
                    <div className="space-y-4">
                        {error && (
                            <p className="flex items-start gap-2 text-xs text-danger border border-danger/40 rounded-md p-3">
                                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                                <span>{error}</span>
                            </p>
                        )}

                        <div className="relative group">
                            <pre className="bg-bg border border-border p-3 text-xs text-text-muted overflow-x-auto font-code rounded-md">
                                {snippet(base, selected.public_key)}
                            </pre>
                            <button
                                onClick={() => copy(selected)}
                                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity px-2 py-1 bg-surface hover:bg-surface-2 text-text-muted text-2xs font-bold border border-border-strong rounded-md"
                            >
                                {copied === selected.id ? '✓ Copied' : 'Copy'}
                            </button>
                        </div>

                        <div>
                            <Label>Allowed sites</Label>
                            <Input
                                value={selected.allowed_origins.join('  ')}
                                onChange={(e) =>
                                    draft({
                                        allowed_origins: e.target.value
                                            .split(/[\s,]+/)
                                            .map((s) => s.trim())
                                            .filter(Boolean),
                                    })
                                }
                                placeholder="https://acme.com  *.acme.com"
                            />
                            {selected.allowed_origins.length === 0 ? (
                                <p className="flex items-start gap-2 text-2xs text-warning mt-1">
                                    <Globe className="w-3 h-3 shrink-0 mt-0.5" />
                                    <span>
                                        No sites allowed, so this widget will not load anywhere.
                                    </span>
                                </p>
                            ) : (
                                <p className="text-2xs text-text-faint mt-1">
                                    <code className="font-code">*.acme.com</code> covers subdomains but
                                    not <code className="font-code">acme.com</code> itself.
                                </p>
                            )}
                        </div>

                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <Label>Header title</Label>
                                <Input value={selected.title}
                                    onChange={(e) => draft({ title: e.target.value })} />
                            </div>
                            <div>
                                <Label>Greeting</Label>
                                <Input value={selected.greeting}
                                    onChange={(e) => draft({ greeting: e.target.value })} />
                            </div>
                        </div>

                        <div>
                            <Label>Accent colour</Label>
                            <div className="flex items-center gap-2 flex-wrap">
                                {PRESET_ACCENTS.map((hex) => (
                                    <button
                                        key={hex}
                                        aria-label={hex}
                                        onClick={() => draft({ accent: hex })}
                                        style={{ background: hex }}
                                        className={`w-6 h-6 rounded-md border-2 ${
                                            selected.accent === hex ? 'border-text' : 'border-transparent'
                                        }`}
                                    />
                                ))}
                                <Input
                                    value={selected.accent}
                                    onChange={(e) => draft({ accent: e.target.value })}
                                    className="w-28 font-code"
                                />
                            </div>
                        </div>

                        <div>
                            <Label>Logo</Label>
                            <div className="flex items-center gap-3">
                                {selected.logo && (
                                    // eslint-disable-next-line @next/next/no-img-element
                                    <img src={selected.logo} alt="" className="w-8 h-8 rounded-md object-contain border border-border" />
                                )}
                                <label className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs border border-border rounded-md cursor-pointer hover:bg-surface-2">
                                    <Upload className="w-3.5 h-3.5" />
                                    {selected.logo ? 'Replace' : 'Upload'}
                                    <input
                                        type="file"
                                        accept="image/png,image/jpeg,image/gif,image/webp"
                                        className="hidden"
                                        onChange={(e) => {
                                            const file = e.target.files?.[0];
                                            if (file) void uploadLogo(file);
                                            e.target.value = '';
                                        }}
                                    />
                                </label>
                                {selected.logo && (
                                    <button onClick={removeLogo}
                                        className="inline-flex items-center gap-1 text-xs text-text-faint hover:text-danger">
                                        <X className="w-3.5 h-3.5" /> Remove
                                    </button>
                                )}
                            </div>
                            <p className="text-2xs text-text-faint mt-1">
                                PNG, JPEG, GIF or WebP, under 128 KB. Drawn at 22 pixels, so small is fine.
                            </p>
                        </div>

                        <div className="grid grid-cols-2 gap-3">
                            <Choice label="Font" value={selected.font} options={FONTS}
                                onChange={(font) => draft({ font })} />
                            <Choice label="Theme" value={selected.theme} options={THEMES}
                                onChange={(theme) => draft({ theme })} />
                            <Choice label="Header" value={selected.header_style}
                                options={[
                                    { id: 'accent', label: 'Accent', hint: 'Solid colour' },
                                    { id: 'neutral', label: 'Neutral', hint: 'Quiet, accent on buttons' },
                                ]}
                                onChange={(header_style) => draft({ header_style })} />
                            <Choice label="Launcher" value={selected.launcher}
                                options={[
                                    { id: 'bubble', label: 'Chat bubble', hint: '' },
                                    { id: 'logo', label: 'Your logo', hint: 'Needs a logo above' },
                                ]}
                                onChange={(launcher) => draft({ launcher })} />
                            <Choice label="Position" value={selected.position}
                                options={[
                                    { id: 'right', label: 'Right', hint: '' },
                                    { id: 'left', label: 'Left', hint: '' },
                                ]}
                                onChange={(position) => draft({ position })} />
                            <div>
                                <Label>Messages per day</Label>
                                <Input type="number" value={String(selected.daily_message_cap)}
                                    onChange={(e) => draft({ daily_message_cap: Number(e.target.value) })} />
                            </div>
                        </div>

                        <div className="flex items-center gap-2 pt-1">
                            <Button onClick={() => save()}>Save changes</Button>
                            <Button variant="secondary"
                                onClick={() => save({ enabled: !selected.enabled })}>
                                {selected.enabled ? 'Disable' : 'Enable'}
                            </Button>
                            <Button variant="secondary" title="Issue a new key"
                                onClick={() => act(
                                    `/api/widgets/${selected.id}/rotate-key`, 'POST',
                                    'Issue a new key? Every page using the current snippet stops working until it is updated.',
                                )}>
                                <RefreshCw className="w-3.5 h-3.5" />
                            </Button>
                            <Button variant="secondary" title="Delete"
                                onClick={() => act(
                                    `/api/widgets/${selected.id}`, 'DELETE',
                                    `Delete "${selected.name || selected.id}"? The snippet stops working everywhere it is installed.`,
                                ).then(() => setSelectedId(''))}>
                                <Trash2 className="w-3.5 h-3.5" />
                            </Button>
                        </div>
                    </div>

                    {/*
                      * The real widget, not a mock. Sandboxed the same way a
                      * customer's page sandboxes it, so what is on screen here is
                      * what a visitor gets — including anything we got wrong.
                      */}
                    <div className="border border-border rounded-md overflow-hidden bg-surface-2">
                        <p className="px-3 py-2 text-2xs uppercase font-bold tracking-wider text-text-faint border-b border-border">
                            Preview
                        </p>
                        <iframe
                            ref={frame}
                            key={selected.id}
                            src={`/embed/preview/${selected.id}`}
                            title="Widget preview"
                            sandbox="allow-same-origin allow-scripts allow-forms"
                            className="w-full h-[520px] border-0 bg-transparent"
                        />
                    </div>
                </div>
            )}

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
                        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                            placeholder="Support chat" />
                    </div>
                    <div>
                        <Label>Agent</Label>
                        <select
                            value={form.agent_id}
                            onChange={(e) => setForm({ ...form, agent_id: e.target.value })}
                            className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text"
                        >
                            <option value="">Choose an agent…</option>
                            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                        </select>
                    </div>
                    <div>
                        <Label>Allowed sites</Label>
                        <Input
                            value={form.allowed_origins}
                            onChange={(e) => setForm({ ...form, allowed_origins: e.target.value })}
                            placeholder="https://acme.com  *.acme.com"
                        />
                        <p className="text-2xs text-text-faint mt-1">
                            Space-separated. Leave this empty and the widget loads nowhere.
                        </p>
                    </div>
                    <p className="text-2xs text-text-faint border-t border-border pt-3">
                        A widget has no file uploads and never asks a visitor to approve a tool call —
                        they are not a member of this workspace and cannot answer that. An agent whose
                        tool policy would stop and ask cannot be published here.
                    </p>
                    <div className="flex justify-end gap-2">
                        <Button variant="secondary" onClick={() => setCreating(false)}>Cancel</Button>
                        <Button onClick={create} disabled={!form.agent_id}>Create</Button>
                    </div>
                </div>
            </Modal>
        </div>
    );
};

const Choice = ({ label, value, options, onChange }: {
    label: string;
    value: string;
    options: { id: string; label: string; hint: string }[];
    onChange: (id: string) => void;
}) => (
    <div>
        <Label>{label}</Label>
        <select
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm text-text"
        >
            {options.map((o) => (
                <option key={o.id} value={o.id}>
                    {o.label}{o.hint ? ` — ${o.hint}` : ''}
                </option>
            ))}
        </select>
    </div>
);

/**
 * The same rule as `branding.readable_on`, so an unsaved colour previews with the
 * text colour the server would have chosen for it. Relative luminance per WCAG,
 * including the sRGB gamma step — the cheap `(r+g+b)/3` version calls mid-greens
 * dark and picks the wrong one on exactly the colours people choose.
 */
function readableOn(accent: string): string {
    const hex = /^#[0-9a-f]{6}$/i.test(accent) ? accent : '#4f46e5';
    const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const linear = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    const luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    return luminance > 0.45 ? '#18181b' : '#ffffff';
}

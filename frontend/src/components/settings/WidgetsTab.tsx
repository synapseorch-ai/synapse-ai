"use client";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Globe, Plus, RefreshCw, Trash2, Upload, X } from 'lucide-react';

import { AppScreen } from '@/components/app/AppScreen';
import { VisitorToolGroups, type VisitorToolChoice } from '@/components/settings/VisitorToolGroups';
import { DetailHead, ListRow, showDetail } from '@/components/run';
import {
    Button, ConfirmDialog, EmptyState, ErrorNote, Field, IconButton, Input, Modal, Select,
    Toast, useToast,
} from '@/components/ui';
import { widgetSnippet } from '@/lib/widgetSnippet';

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
    /** The tools a visitor's turn may use — none unless ticked (F280). Absent
     *  on a widget saved before the list existed, which means none. */
    visitor_tools?: string[];
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
    const [loaded, setLoaded] = useState(false);
    const [confirming, setConfirming] = useState<'rotate' | 'delete' | null>(null);
    const frame = useRef<HTMLIFrameElement>(null);
    const detailRef = useRef<HTMLElement>(null);
    const { toast, show, dismiss } = useToast();

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
            const rows: WidgetRecord[] = Array.isArray(w) ? w : [];
            setWidgets(rows);
            setLoaded(true);
            setAgents(Array.isArray(a) ? a.map((x: AgentOption) => ({ id: x.id, name: x.name })) : []);
            // Land on something. A screen that lists one widget and shows an
            // empty right-hand side reads as broken — and the preview is the
            // point of the screen, so it should be on when you arrive.
            setSelectedId((current) =>
                rows.some((r) => r.id === current) ? current : (rows[0]?.id ?? ''),
            );
        })();
        return () => { cancelled = true; };
    }, [reloads]);

    const selected = useMemo(
        () => widgets.find((w) => w.id === selectedId) ?? null,
        [widgets, selectedId],
    );

    // What the selected widget's agent could let a visitor use. Keyed by the
    // agent, so switching widgets never shows the previous agent's list.
    const [choices, setChoices] = useState<{ agentId: string; tools: VisitorToolChoice[] } | null>(null);
    const selectedAgent = selected?.agent_id ?? '';
    useEffect(() => {
        if (!selectedAgent) return;
        let cancelled = false;
        void (async () => {
            const response = await fetch(`/api/widgets/visitor-tools?agent_id=${encodeURIComponent(selectedAgent)}`);
            const body = response.ok ? await response.json() : { tools: [] };
            if (!cancelled) setChoices({ agentId: selectedAgent, tools: body.tools ?? [] });
        })();
        return () => { cancelled = true; };
    }, [selectedAgent]);
    const toolChoices = choices?.agentId === selectedAgent ? choices.tools : null;

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
        if (!('enabled' in patch)) show('Saved.');
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
            // A 409 here names what is wrong with the choice (the AI builder's
            // own agents cannot be published). The server's own words beat a
            // paraphrase.
            setError((await response.json().catch(() => ({}))).detail ?? 'Could not save.');
            return;
        }
        setCreating(false);
        setForm({ ...BLANK });
        setSelectedId((await response.json()).id);
        load();
    };

    const act = async (path: string, method: string) => {
        await fetch(path, { method });
        load();
    };

    const copy = async (widget: WidgetRecord) => {
        await navigator.clipboard.writeText(widgetSnippet(base, widget.public_key));
        setCopied(widget.id);
        setTimeout(() => setCopied(''), 2000);
    };

    const agentName = (id: string) => agents.find((a) => a.id === id)?.name ?? id;

    return (
        <AppScreen
            id="widgets"
            width="wide"
            actions={<Button onClick={() => setCreating(true)}><Plus className="size-4" aria-hidden /> New widget</Button>}
        >
            {loaded && widgets.length === 0 && (
                <EmptyState
                    title="No widgets yet"
                    action={<Button onClick={() => setCreating(true)}><Plus className="size-4" aria-hidden /> New widget</Button>}
                >
                    Drop an agent into your own site with one tag. The key in the snippet is public
                    by design — what decides who may use it is the list of sites you allow.
                </EmptyState>
            )}

            {widgets.length > 0 && (
                <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
                    <nav aria-label="Widgets">
                        <ul className="m-0 list-none border-t border-border p-0">
                            {widgets.map((w) => (
                                <ListRow
                                    key={w.id}
                                    data={{ 'data-widget-row': w.id }}
                                    title={w.name || 'Untitled'}
                                    sub={agentName(w.agent_id)}
                                    meta={w.allowed_origins.length === 0 ? 'no sites' : !w.enabled ? 'Off' : undefined}
                                    current={w.id === selectedId}
                                    onClick={() => { setSelectedId(w.id); showDetail(detailRef.current); }}
                                />
                            ))}
                        </ul>
                    </nav>

                    <section ref={detailRef} aria-label="Widget" className="@container min-w-0 scroll-mt-4">
                        {selected && (
                            <div className="grid gap-6">
                                <DetailHead
                                    title={selected.name || 'Untitled'}
                                    sub={<>{agentName(selected.agent_id)}{!selected.enabled && ' · Off'}</>}
                                    actions={
                                        <>
                                            <Button variant="secondary" onClick={() => save({ enabled: !selected.enabled })}>
                                                {selected.enabled ? 'Disable' : 'Enable'}
                                            </Button>
                                            <IconButton icon={RefreshCw} label="Issue a new key" onClick={() => setConfirming('rotate')} />
                                            <IconButton icon={Trash2} tone="danger" label="Delete" onClick={() => setConfirming('delete')} />
                                        </>
                                    }
                                />
                                <ErrorNote>{error}</ErrorNote>

                                <div className="grid items-start gap-6 @[52rem]:grid-cols-[minmax(0,1fr)_360px]">
                                    {/*
                                      * `minmax(0,1fr)` above and `min-w-0` here, both needed. A
                                      * grid track defaults to `min-width: auto`, so the snippet
                                      * `<pre>` — one long unbreakable line — widens this column
                                      * past the viewport and pushes the preview off the right
                                      * edge, `overflow-x-auto` on the `<pre>` notwithstanding.
                                      */}
                                    <div className="grid min-w-0 gap-5">
                                        <div className="grid gap-1.5">
                                            <div className="flex items-center justify-between gap-2">
                                                <span className="block text-sm font-medium text-text">The tag</span>
                                                <Button size="sm" variant="secondary" onClick={() => copy(selected)}>
                                                    {copied === selected.id ? 'Copied' : 'Copy snippet'}
                                                </Button>
                                            </div>
                                            <pre className="m-0 overflow-x-auto rounded-md border border-border bg-surface-2 p-3 font-code text-xs text-text-muted">
                                                {widgetSnippet(base, selected.public_key)}
                                            </pre>
                                            <p className="m-0 text-xs text-text-faint">
                                                The key in the snippet is public by design — what decides who may use it is
                                                the list of sites you allow.
                                            </p>
                                        </div>

                                        <div className="grid gap-1.5">
                                            <label htmlFor="widget-allowed-sites" className="block text-sm font-medium text-text">Allowed sites</label>
                                            <Input
                                                id="widget-allowed-sites"
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
                                                <p className="m-0 flex items-start gap-2 text-xs font-medium text-text">
                                                    <Globe className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                                    <span>No sites allowed, so this widget will not load anywhere.</span>
                                                </p>
                                            ) : (
                                                <p className="m-0 text-xs text-text-faint">
                                                    <code className="font-code">*.acme.com</code> covers subdomains but
                                                    not <code className="font-code">acme.com</code> itself.
                                                </p>
                                            )}
                                        </div>

                                        <div className="grid gap-1.5">
                                            <span className="block text-sm font-medium text-text">What visitors can use</span>
                                            <p className="m-0 text-xs text-text-faint">
                                                Anyone on the sites above can make the agent use a tool you tick here —
                                                a read-only one reads your files and data for them. Nothing is ticked
                                                until you tick it.
                                            </p>
                                            {toolChoices === null ? (
                                                <p className="m-0 text-xs text-text-faint">Loading the agent&apos;s tools…</p>
                                            ) : toolChoices.length === 0 ? (
                                                <p className="m-0 text-xs text-text-faint">This agent has no tools to offer visitors.</p>
                                            ) : (
                                                <VisitorToolGroups
                                                    tools={toolChoices}
                                                    ticked={selected.visitor_tools ?? []}
                                                    onChange={(visitor_tools) => draft({ visitor_tools })}
                                                    idPrefix="widget-visitor-tool"
                                                    subject="Visitors"
                                                />
                                            )}
                                        </div>

                                        <div className="grid grid-cols-2 gap-3">
                                            <div className="grid gap-1.5">
                                                <label htmlFor="widget-title" className="block text-sm font-medium text-text">Header title</label>
                                                <Input id="widget-title" value={selected.title}
                                                    onChange={(e) => draft({ title: e.target.value })} />
                                            </div>
                                            <div className="grid gap-1.5">
                                                <label htmlFor="widget-greeting" className="block text-sm font-medium text-text">Greeting</label>
                                                <Input id="widget-greeting" value={selected.greeting}
                                                    onChange={(e) => draft({ greeting: e.target.value })} />
                                            </div>
                                        </div>

                                        <div className="grid gap-1.5">
                                            <label htmlFor="widget-accent" className="block text-sm font-medium text-text">Accent colour</label>
                                            <div className="flex flex-wrap items-center gap-2">
                                                {PRESET_ACCENTS.map((hex) => (
                                                    <button
                                                        key={hex}
                                                        type="button"
                                                        aria-label={hex}
                                                        aria-pressed={selected.accent === hex}
                                                        onClick={() => draft({ accent: hex })}
                                                        style={{ background: hex }}
                                                        className={`size-6 rounded-md border-2 ${
                                                            selected.accent === hex ? 'border-text' : 'border-transparent'
                                                        }`}
                                                    />
                                                ))}
                                                <Input
                                                    id="widget-accent"
                                                    value={selected.accent}
                                                    onChange={(e) => draft({ accent: e.target.value })}
                                                    className="w-28 font-code"
                                                />
                                            </div>
                                        </div>

                                        <div className="grid gap-1.5">
                                            <span className="block text-sm font-medium text-text">Logo</span>
                                            <div className="flex items-center gap-3">
                                                {selected.logo && (
                                                    // eslint-disable-next-line @next/next/no-img-element
                                                    <img src={selected.logo} alt="" className="size-8 rounded-md border border-border object-contain" />
                                                )}
                                                <label className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 text-[13px] font-medium text-text hover:bg-surface-2 focus-within:outline focus-within:outline-2 focus-within:outline-accent">
                                                    <Upload className="size-3.5" aria-hidden />
                                                    {selected.logo ? 'Replace' : 'Upload'}
                                                    <input
                                                        type="file"
                                                        accept="image/png,image/jpeg,image/gif,image/webp"
                                                        className="sr-only"
                                                        onChange={(e) => {
                                                            const file = e.target.files?.[0];
                                                            if (file) void uploadLogo(file);
                                                            e.target.value = '';
                                                        }}
                                                    />
                                                </label>
                                                {selected.logo && (
                                                    <Button size="sm" variant="ghost" onClick={removeLogo}>
                                                        <X className="size-3.5" aria-hidden /> Remove
                                                    </Button>
                                                )}
                                            </div>
                                            <p className="m-0 text-xs text-text-faint">
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
                                            <div className="grid content-start gap-1.5">
                                                <label htmlFor="widget-daily-cap" className="block text-sm font-medium text-text">Messages per day</label>
                                                <Input id="widget-daily-cap" type="number" value={String(selected.daily_message_cap)}
                                                    onChange={(e) => draft({ daily_message_cap: Number(e.target.value) })} />
                                            </div>
                                        </div>

                                        <div>
                                            <Button onClick={() => save()}>Save changes</Button>
                                        </div>
                                    </div>

                                    {/*
                                      * The real widget, not a mock. Sandboxed the same way a
                                      * customer's page sandboxes it, so what is on screen here is
                                      * what a visitor gets — including anything we got wrong.
                                      */}
                                    <div className="overflow-hidden rounded-lg border border-border bg-surface-2 @[52rem]:sticky @[52rem]:top-4">
                                        <p className="m-0 border-b border-border px-3 py-2 text-xs font-medium text-text-muted">
                                            Preview
                                        </p>
                                        <iframe
                                            ref={frame}
                                            key={selected.id}
                                            src={`/embed/preview/${selected.id}`}
                                            title="Widget preview"
                                            sandbox="allow-same-origin allow-scripts allow-forms"
                                            className="h-[520px] w-full border-0 bg-transparent"
                                        />
                                    </div>
                                </div>
                            </div>
                        )}
                    </section>
                </div>
            )}

            <Modal
                open={creating}
                onClose={() => setCreating(false)}
                title="New widget"
                footer={
                    <>
                        <Button variant="secondary" onClick={() => setCreating(false)}>Cancel</Button>
                        <Button onClick={create} disabled={!form.agent_id}>Create</Button>
                    </>
                }
            >
                <div className="grid gap-4">
                    <ErrorNote>{creating ? error : ''}</ErrorNote>
                    <div className="grid gap-1.5">
                        <label htmlFor="widget-new-name" className="block text-sm font-medium text-text">Name</label>
                        <Input id="widget-new-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                            placeholder="Support chat" />
                    </div>
                    <Field label="Agent">
                        <Select
                            value={form.agent_id || undefined}
                            onChange={(agent_id) => setForm({ ...form, agent_id })}
                            placeholder="Choose an agent…"
                            options={agents.map((a) => ({ value: a.id, label: a.name }))}
                        />
                    </Field>
                    <div className="grid gap-1.5">
                        <label htmlFor="widget-new-allowed-sites" className="block text-sm font-medium text-text">Allowed sites</label>
                        <Input
                            id="widget-new-allowed-sites"
                            value={form.allowed_origins}
                            onChange={(e) => setForm({ ...form, allowed_origins: e.target.value })}
                            placeholder="https://acme.com  *.acme.com"
                        />
                        <p className="m-0 text-xs text-text-faint">
                            Space-separated. Leave this empty and the widget loads nowhere.
                        </p>
                    </div>
                    <p className="m-0 border-t border-border pt-3 text-xs text-text-faint">
                        A widget has no file uploads and never asks a visitor to approve anything —
                        they are not a member of this workspace. Visitors can use none of the
                        agent&apos;s tools until you tick them after creating the widget.
                    </p>
                </div>
            </Modal>

            <ConfirmDialog
                open={confirming === 'rotate' && !!selected}
                onClose={() => setConfirming(null)}
                onConfirm={async () => {
                    if (selected) await act(`/api/widgets/${selected.id}/rotate-key`, 'POST');
                    setConfirming(null);
                }}
                title="Issue a new key?"
                description="Every page using the current snippet stops working until it is updated."
                confirmLabel="Issue a new key"
            />
            <ConfirmDialog
                open={confirming === 'delete' && !!selected}
                onClose={() => setConfirming(null)}
                onConfirm={async () => {
                    if (selected) {
                        await act(`/api/widgets/${selected.id}`, 'DELETE');
                        setSelectedId('');
                    }
                    setConfirming(null);
                }}
                title={`Delete "${selected?.name || selected?.id}"?`}
                description="The snippet stops working everywhere it is installed."
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </AppScreen>
    );
};

const Choice = ({ label, value, options, onChange }: {
    label: string;
    value: string;
    options: { id: string; label: string; hint: string }[];
    onChange: (id: string) => void;
}) => (
    <Field label={label}>
        <Select
            aria-label={label}
            value={value}
            onChange={onChange}
            options={options.map((o) => ({ value: o.id, label: o.label, hint: o.hint || undefined }))}
        />
    </Field>
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

/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { Wrench, Plus, Trash, X, ExternalLink, AlertTriangle, CheckCircle2, RefreshCw, Container, Import } from 'lucide-react';
import { PythonToolEditor, type PythonDraftTool } from './PythonToolEditor';
import { OpenApiImport } from './OpenApiImport';
import { Badge, Button, buttonStyles, CodeEditor, ConfirmDialog, Label, Select, TemplateInput, TemplateScope, schemaArgs } from '@/components/ui';
import { AppScreen } from '@/components/app/AppScreen';
import { annotationsFor, effectOf, effectOfMethod, TOOL_EFFECTS, type ToolEffect } from '@/lib/toolHints';

interface CustomToolsTabProps {
    customTools: any[];
    draftTool: any;
    setDraftTool: (v: any) => void;
    toolBuilderMode: 'config' | 'python';
    setToolBuilderMode: (v: 'config' | 'python') => void;
    headerRows: { id: string; key: string; value: string }[];
    setHeaderRows: (v: { id: string; key: string; value: string }[]) => void;
    n8nWorkflows: any[];
    n8nWorkflowsLoading: boolean;
    n8nWorkflowId: string | null;
    setN8nWorkflowId: (v: string | null) => void;
    getN8nBaseUrl: () => string;
    onSaveTool: () => void;
    onDeleteTool: (name: string) => void;
    /** Called with tools imported from an OpenAPI spec (already saved to backend) */
    onImported: (tools: any[]) => void;
    /** True when n8n URL + API key are configured */
    n8nIntegrated: boolean;
}

export const CustomToolsTab = ({
    customTools, draftTool, setDraftTool,
    toolBuilderMode, setToolBuilderMode,
    headerRows, setHeaderRows,
    n8nWorkflows, n8nWorkflowsLoading,
    n8nWorkflowId, setN8nWorkflowId,
    getN8nBaseUrl, onSaveTool, onDeleteTool, onImported,
    n8nIntegrated,
}: CustomToolsTabProps) => {
    // OpenAPI import panel toggle
    const [showImport, setShowImport] = useState(false);
    // The argument names `{{` offers in the URL, headers and body — from the schema
    // as typed, and none while it does not parse.
    const schemaArgNames = useMemo(() => {
        try {
            return schemaArgs(JSON.parse(draftTool?.inputSchemaStr || '{}'));
        } catch {
            return [];
        }
    }, [draftTool?.inputSchemaStr]);
    // Docker status state
    type DockerStatus = { installed: boolean; running: boolean; image_exists: boolean } | null;
    const [dockerStatus, setDockerStatus] = useState<DockerStatus>(null);
    // Tool groups: a tool carries its group's id; the agent picker shows one
    // row per group and an agent may take the whole group (`group:<id>`).
    const [groups, setGroups] = useState<{ id: string; name: string; description?: string }[]>([]);
    const [newGroupName, setNewGroupName] = useState('');
    const reloadGroups = () => fetch('/api/tools/groups').then(r => r.json()).then(g => setGroups(Array.isArray(g) ? g : [])).catch(() => undefined);
    useEffect(() => { reloadGroups(); }, []);
    const createGroup = async () => {
        const name = newGroupName.trim();
        if (!name) return;
        const res = await fetch('/api/tools/groups', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
        if (res.ok) {
            const g = await res.json();
            setNewGroupName('');
            await reloadGroups();
            if (draftTool) setDraftTool({ ...draftTool, group: g.id });
        }
    };
    const [dockerChecking, setDockerChecking] = useState(false);
    const [dockerBuilding, setDockerBuilding] = useState(false);
    const [dockerBuildError, setDockerBuildError] = useState<string | null>(null);

    const checkDockerStatus = useCallback(async () => {
        setDockerChecking(true);
        try {
            const res = await fetch('/api/tools/docker/status');
            if (res.ok) setDockerStatus(await res.json());
        } finally {
            setDockerChecking(false);
        }
    }, []);

    const buildSandboxImage = async () => {
        setDockerBuilding(true);
        setDockerBuildError(null);
        try {
            const res = await fetch('/api/tools/docker/build', { method: 'POST' });
            if (res.ok) {
                await checkDockerStatus();
            } else {
                const err = await res.json();
                setDockerBuildError(err.detail || 'Build failed');
            }
        } catch (e: any) {
            setDockerBuildError(String(e));
        } finally {
            setDockerBuilding(false);
        }
    };

    useEffect(() => { checkDockerStatus(); }, [checkDockerStatus]);

    // ── A tool's kind, as a quiet chip ─────────────────────────────────────
    const getToolBadge = (t: any) => {
        if (t.tool_type === 'python') return <Badge size="sm" tone="machine">Python</Badge>;
        if (t.workflowId || t.url?.includes('webhook')) return <Badge size="sm">n8n</Badge>;
        return <Badge size="sm">HTTP · {(t.method || 'POST').toUpperCase()}</Badge>;
    };

    // ── Which tool is open (M2f: the list and the editor side by side) ─────
    const draftFromTool = (t: any) => (t.tool_type === 'python'
        ? { draft: { ...t, schemaParams: t.schemaParams || [] }, rows: null, mode: 'python' as const }
        : {
            draft: {
                ...t,
                inputSchemaStr: JSON.stringify(t.inputSchema || {}, null, 2),
                outputSchemaStr: t.outputSchema ? JSON.stringify(t.outputSchema, null, 2) : '',
            },
            rows: (() => {
                const r = Object.entries(t.headers || {}).map(([k, v], i) => ({ id: `h-${i}`, key: k, value: v as string }));
                return r.length ? r : [{ id: 'h1', key: '', value: '' }];
            })(),
            mode: 'config' as const,
        });
    const snapshot = (d: any, rows: unknown) => JSON.stringify([d, rows]);
    const baseline = useRef<string | null>(null);
    // The name last opened, so a save (which closes the draft) opens it again.
    const openName = useRef<string | null>(null);
    const userClosed = useRef(false);
    const [pending, setPending] = useState<null | (() => void)>(null);

    const openTool = (t: any) => {
        const { draft, rows, mode } = draftFromTool(t);
        setDraftTool(draft);
        if (rows) setHeaderRows(rows);
        setToolBuilderMode(mode);
        setShowImport(false);
        openName.current = t.name;
        userClosed.current = false;
        baseline.current = snapshot(draft, mode === 'python' ? null : rows);
    };
    const startNew = () => {
        const initialInput = { type: 'object', properties: { input: { type: 'string' } } };
        const draft = {
            name: '', generalName: '', description: '', url: '', method: 'POST',
            inputSchema: initialInput, inputSchemaStr: JSON.stringify(initialInput, null, 2),
            outputSchemaStr: '', tool_type: 'http',
        };
        const rows = [{ id: 'h1', key: '', value: '' }];
        setDraftTool(draft);
        setHeaderRows(rows);
        setToolBuilderMode('config');
        setShowImport(false);
        openName.current = null;
        userClosed.current = false;
        baseline.current = snapshot(draft, rows);
    };
    const closeEditor = () => {
        userClosed.current = true;
        openName.current = null;
        setDraftTool(null);
        setToolBuilderMode('config');
    };
    // Python tools keep no header rows, so they are left out of the comparison.
    const dirty = !!draftTool && baseline.current !== null
        && snapshot(draftTool, toolBuilderMode === 'python' ? null : headerRows) !== baseline.current;
    // Ask before an unsaved edit is dropped, as the agents workbench does.
    const guard = (next: () => void) => { if (dirty) setPending(() => next); else next(); };

    // A deleted tool that was open closes, and the list's first opens instead.
    useEffect(() => {
        if (!draftTool || !openName.current) return;
        if (customTools.some((t: any) => t.name === openName.current)) return;
        if (draftTool.name !== openName.current) return; // renamed in the editor: still a draft of it
        openName.current = null;
        setDraftTool(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [customTools]);

    // Open the first tool when the page loads, and the saved one after a save.
    useEffect(() => {
        if (draftTool || showImport || userClosed.current || !customTools.length) return;
        const again = openName.current ? customTools.find((t: any) => t.name === openName.current) : null;
        openTool(again ?? customTools[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [draftTool, showImport, customTools]);

    const actions = (
        <>
            <Button variant="secondary" onClick={() => guard(() => { closeEditor(); userClosed.current = true; setShowImport(true); })}>
                <Import className="size-4" aria-hidden /> Import OpenAPI
            </Button>
            <Button variant="secondary" onClick={() => guard(startNew)}>
                <Plus className="size-4" aria-hidden /> New Tool
            </Button>
        </>
    );

    const list = (
        <nav aria-label="Tools" className="min-w-0">
            {customTools.length === 0 ? (
                <div className="rounded-xl border border-dashed border-border px-5 py-10 text-center text-sm text-text-muted">
                    No custom tools yet. Create an HTTP, n8n webhook, or Python tool.
                </div>
            ) : (
                <ul className="grid gap-0.5" data-tool-list="">
                    {customTools.map((t: any) => {
                        const active = !!draftTool && draftTool.name === t.name && openName.current === t.name;
                        const group = t.group ? groups.find(g => g.id === t.group) : null;
                        return (
                            <li key={t.name} data-tool-row={t.name} className="group relative">
                                <button
                                    type="button"
                                    aria-current={active ? 'true' : undefined}
                                    onClick={() => guard(() => openTool(t))}
                                    className={`grid w-full gap-1 rounded-lg px-3 py-2.5 pr-10 text-left transition-colors ${active ? 'bg-surface-2 shadow-[inset_0_0_0_1px_var(--border)]' : 'hover:bg-row-hover'}`}
                                >
                                    <span className="flex min-w-0 items-center gap-2">
                                        <span className="truncate text-sm font-semibold text-text">{t.generalName || t.name}</span>
                                        {getToolBadge(t)}
                                    </span>
                                    {t.generalName && <span className="truncate font-mono text-xs text-text-faint">{t.name}</span>}
                                    {t.description && <span className="line-clamp-2 text-xs text-text-muted">{t.description}</span>}
                                    {group && <span className="truncate text-xs text-text-faint">{group.name}</span>}
                                </button>
                                <button
                                    onClick={() => onDeleteTool(t.name)}
                                    aria-label={`Delete ${t.generalName || t.name}`}
                                    title={`Delete ${t.generalName || t.name}`}
                                    className="absolute right-1.5 top-2 inline-flex size-7 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-danger md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100"
                                >
                                    <Trash className="size-4" aria-hidden />
                                </button>
                            </li>
                        );
                    })}
                </ul>
            )}
        </nav>
    );

    return (
        <AppScreen id="custom_tools" width="wide" actions={actions}>
        {/* With no tool yet there is nothing to list: one empty state, full width. */}
        <div className={customTools.length || draftTool || showImport ? 'grid gap-6 md:grid-cols-[18.75rem_minmax(0,1fr)] md:gap-8' : ''}>
            {(customTools.length > 0 || !!draftTool || showImport) && list}
            <div className="min-w-0">
            {showImport && !draftTool ? (
                /* ── OpenAPI Import ───────────────────────────────────────── */
                <OpenApiImport
                    onClose={() => setShowImport(false)}
                    onImported={(tools) => { onImported(tools); setShowImport(false); userClosed.current = false; }}
                />
            ) : !draftTool ? (
                <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
                    <Wrench className="mx-auto mb-2 size-8 text-text-faint" aria-hidden />
                    <p className="text-sm font-medium text-text">{customTools.length ? 'Pick a tool to edit it.' : 'Make your first tool.'}</p>
                    <p className="mx-auto mt-1 max-w-md text-sm text-text-muted">
                        Extend your agent with n8n webhooks, HTTP endpoints, or Python functions.
                    </p>
                    <div className="mt-4 flex justify-center gap-2">{actions}</div>
                </div>
            ) : (
                /* ── Builder View ────────────────────────────────────────── */
                <div className="flex flex-col">
                    {/* Header */}
                    <div className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
                        <div className="flex min-w-0 items-center gap-3">
                            <button
                                onClick={() => guard(closeEditor)}
                                aria-label="Close the tool editor"
                                className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
                            >
                                <X className="size-4" aria-hidden />
                            </button>
                            <h2 className="truncate text-section text-text">
                                {draftTool.name ? <>Editing: <span className="font-mono">{draftTool.name}</span></> : 'New tool'}
                            </h2>
                            {dirty && <span className="text-xs text-text-faint">Unsaved</span>}
                        </div>
                        <div className="flex items-center gap-2">
                            {/* Mode tabs */}
                            <div role="group" aria-label="Tool type" className="inline-flex gap-0.5 rounded-lg bg-surface-2 p-0.5">
                                <button
                                    type="button"
                                    aria-pressed={toolBuilderMode === 'config'}
                                    onClick={() => {
                                        if (draftTool.tool_type === 'python') {
                                            const initialInput = { type: 'object', properties: { input: { type: 'string' } } };
                                            setDraftTool({ ...draftTool, tool_type: 'http', url: draftTool.url || '', method: draftTool.method || 'POST', inputSchema: initialInput, inputSchemaStr: JSON.stringify(initialInput, null, 2), outputSchemaStr: '' });
                                            setHeaderRows([{ id: 'h1', key: '', value: '' }]);
                                        }
                                        setToolBuilderMode('config');
                                    }}
                                    className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${toolBuilderMode === 'config' ? 'bg-bg text-text shadow-[0_0_0_1px_var(--border)]' : 'text-text-muted hover:text-text'}`}
                                >
                                    HTTP
                                </button>
                                <button
                                    type="button"
                                    aria-pressed={toolBuilderMode === 'python'}
                                    onClick={() => {
                                        if (draftTool.tool_type !== 'python') {
                                            const defaultCode = `# _args contains all tool arguments as a Python dict\n# print() stdout becomes the tool result\nimport json\n\nresult = {"output": _args.get("input", "")}\nprint(json.dumps(result))\n`;
                                            setDraftTool({ ...draftTool, tool_type: 'python', code: draftTool.code || defaultCode, inputSchema: { type: 'object', properties: { input: { type: 'string' } } }, schemaParams: draftTool.schemaParams || [{ id: 'p1', name: 'input', type: 'string', description: 'The input value', required: false }] });
                                        }
                                        setToolBuilderMode('python');
                                    }}
                                    className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${toolBuilderMode === 'python' ? 'bg-bg text-text shadow-[0_0_0_1px_var(--border)]' : 'text-text-muted hover:text-text'}`}
                                >
                                    Python
                                </button>
                            </div>
                            <Button size="sm" onClick={onSaveTool}>Save</Button>
                        </div>
                    </div>

                    {/* Shared name / description fields */}
                    <div className="grid grid-cols-2 gap-4 mb-4">
                        <div className="space-y-1">
                            <Label htmlFor="cus-drafttool-generalname" className="block">General Name</Label>
                            <input id="cus-drafttool-generalname"
                                type="text"
                                value={draftTool.generalName || ''}
                                onChange={e => {
                                    const val = e.target.value;
                                    const update: any = { ...draftTool, generalName: val };
                                    if (!draftTool.name) {
                                        update.name = val.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
                                    }
                                    setDraftTool(update);
                                }}
                                className="w-full bg-surface border border-border-strong px-3 py-2 text-sm text-text focus:border-accent focus:outline-none placeholder:text-text-faint rounded-md"
                                placeholder="e.g. Process Orders"
                            />
                        </div>
                        <div className="space-y-1">
                            <Label htmlFor="cus-drafttool-name" className="block">System Name (Snake Case)</Label>
                            <input id="cus-drafttool-name"
                                type="text"
                                value={draftTool.name}
                                onChange={e => setDraftTool({ ...draftTool, name: e.target.value })}
                                className="w-full bg-surface border border-border-strong px-3 py-2 text-sm text-text focus:border-accent focus:outline-none font-mono placeholder:text-text-faint rounded-md"
                                placeholder="e.g. process_orders"
                            />
                        </div>
                    </div>
                    <div className="space-y-1 mb-5">
                        <Label htmlFor="cus-drafttool-description" className="block">Description (For AI)</Label>
                        <textarea id="cus-drafttool-description"
                            value={draftTool.description}
                            onChange={e => setDraftTool({ ...draftTool, description: e.target.value })}
                            className="w-full bg-surface border border-border-strong px-3 py-2 text-sm text-text focus:border-accent focus:outline-none resize-vertical min-h-[80px] rounded-md"
                            placeholder="What does this tool do? Describe its purpose and critical rules for the AI..."
                        />
                    </div>

                    {/* ── CONFIG mode (HTTP/n8n tool) ────────────────────── */}
                    {toolBuilderMode === 'config' && (
                        <TemplateScope args={schemaArgNames}>
                        <div className="space-y-5 pr-2">
                            {/* Method */}
                            <div className="space-y-1">
                                <Label className="block">Method</Label>
                                <Select
                                    value={draftTool.method || 'POST'}
                                    onChange={v => setDraftTool({
                                        ...draftTool,
                                        method: v,
                                        // What it does follows the method until the person says otherwise (F488).
                                        annotations: draftTool.annotations
                                            && effectOf(draftTool.annotations, draftTool.method) !== effectOfMethod(draftTool.method || 'POST')
                                            ? draftTool.annotations : undefined,
                                    })}
                                    aria-label="HTTP method"
                                    options={['POST', 'GET', 'PUT', 'DELETE'].map(m => ({ value: m, label: m }))}
                                />
                            </div>

                            {/* What it does (F488): the tool policy decides from this whether it asks first. */}
                            <div className="space-y-1">
                                <Label className="block">What it does</Label>
                                <Select
                                    value={effectOf(draftTool.annotations, draftTool.method || 'POST')}
                                    onChange={v => setDraftTool({ ...draftTool, annotations: annotationsFor(v as ToolEffect) })}
                                    aria-label="What the tool does"
                                    options={TOOL_EFFECTS}
                                />
                                <p className="text-xs text-text-muted">
                                    Your tool policy reads this: a tool that changes or deletes something can wait for a person before it runs.
                                </p>
                            </div>

                            {/* Group */}
                            <div className="space-y-1">
                                <Label className="block">Group <span className="text-text-faint font-normal normal-case">(optional — one row in the agent picker)</span></Label>
                                <div className="flex gap-2">
                                    <Select
                                        value={draftTool.group || ''}
                                        onChange={v => setDraftTool({ ...draftTool, group: v || null })}
                                        aria-label="Tool group"
                                        options={[{ value: '', label: 'No group' }, ...groups.map(g => ({ value: g.id, label: g.name }))]}
                                    />
                                    <input
                                        type="text"
                                        value={newGroupName}
                                        onChange={e => setNewGroupName(e.target.value)}
                                        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); createGroup(); } }}
                                        aria-label="New group name"
                                        placeholder="New group name…"
                                        className="w-44 bg-surface border border-border-strong px-3 py-2 text-xs text-text focus:border-accent focus:outline-none rounded-md"
                                    />
                                    <Button type="button" variant="secondary" size="sm" onClick={createGroup} disabled={!newGroupName.trim()}>Add</Button>
                                </div>
                            </div>

                            {/* n8n Workflow — only shown when n8n is integrated */}
                            {n8nIntegrated && (
                                <div className="space-y-1">
                                    <Label className="block">n8n Workflow</Label>
                                    <Select
                                        value={draftTool.workflowId || undefined}
                                        onChange={async (workflowId) => {
                                            setDraftTool({ ...draftTool, workflowId });
                                            setN8nWorkflowId(workflowId || null);
                                            if (!workflowId) return;
                                            try {
                                                const res = await fetch(`/api/n8n/workflows/${workflowId}/webhook`);
                                                if (!res.ok) return;
                                                const data = await res.json();
                                                if (data?.productionUrl) {
                                                    setDraftTool({ ...draftTool, workflowId, url: data.productionUrl });
                                                }
                                            } catch { /* ignore */ }
                                        }}
                                        placeholder={n8nWorkflowsLoading ? 'Loading workflows…' : 'Select a workflow (optional)'}
                                        aria-label="n8n workflow"
                                        options={(Array.isArray(n8nWorkflows) ? n8nWorkflows : []).map((w: any) => ({
                                            value: String(w.id),
                                            label: w.name || String(w.id),
                                        }))}
                                    />
                                    {draftTool.workflowId ? (
                                        <a
                                            href={`${getN8nBaseUrl()}/workflow/${draftTool.workflowId}`}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-2xs text-accent-ink hover:text-accent-hover hover:underline mt-1"
                                        >
                                            Open workflow in n8n <ExternalLink className="h-3 w-3" />
                                        </a>
                                    ) : (
                                        <a
                                            href={`${getN8nBaseUrl()}/workflow/new`}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-2xs text-text-faint hover:text-text hover:underline mt-1"
                                        >
                                            Create new workflow in n8n <ExternalLink className="h-3 w-3" />
                                        </a>
                                    )}
                                </div>
                            )}

                            {/* URL */}
                            <div className="space-y-1">
                                <div className="flex items-center gap-2">
                                    <Label className="block">URL</Label>
                                    {!n8nIntegrated && (
                                        <span className="text-2xs text-text-faint">
                                            (you can also{' '}
                                            <Link href="/settings/workspace" className="text-accent-ink hover:underline">
                                                integrate n8n workflows
                                            </Link>
                                            )
                                        </span>
                                    )}
                                </div>
                                <TemplateInput
                                    profile="tool-url"
                                    value={draftTool.url || ''}
                                    onChange={e => setDraftTool({ ...draftTool, url: e.target.value })}
                                    placeholder="https://api.example.com/users/{{ id }}"
                                    aria-label="URL"
                                />
                                {/* URL templating hint — dynamic based on input schema */}
                                {(() => {
                                    let keys: string[] = [];
                                    try {
                                        const schema = JSON.parse(draftTool.inputSchemaStr || '{}');
                                        keys = Object.keys(schema?.properties || {});
                                    } catch { /* ignore parse errors */ }
                                    // Double braces, the one syntax used in the body
                                    // template, headers and prompts too. `{field}`
                                    // is still read for URLs written before this
                                    // and for anything pasted from an OpenAPI spec,
                                    // whose own path syntax is single-brace — but it
                                    // is not what anything teaches any more.
                                    const examples = keys.length
                                        ? keys.slice(0, 3).map(k => `{{ ${k} }}`).join(', ')
                                        : '{{ field }}';
                                    return (
                                        <p className="mt-1 text-2xs text-text-faint leading-relaxed">
                                            Use <code className="font-code text-text-muted bg-surface px-1 rounded-md">{examples}</code> in the URL to inject input values — type <code className="font-code text-text-muted bg-surface px-1 rounded-md">{'{{'}</code> to pick one.
                                            {' '}<code className="font-code text-text-muted bg-surface px-1 rounded-md">{'{{ global.key }}'}</code> works here too.
                                            {' '}GET/DELETE args become query params; POST/PUT go in the body.
                                        </p>
                                    );
                                })()}
                            </div>

                            {/* Body template — POST/PUT/DELETE */}
                            {(draftTool.method || 'POST') !== 'GET' && (
                                <div className="space-y-1">
                                    <Label className="block">Request body <span className="text-text-faint font-normal normal-case">(optional)</span></Label>
                                    <CodeEditor
                                        language="json"
                                        profile="tool"
                                        lint="json-template"
                                        height={120}
                                        value={draftTool.body || ''}
                                        onChange={body => setDraftTool({ ...draftTool, body })}
                                        placeholder={'{{ body }}   — the "body" argument is the request body\n{{ body.data }}   — a key inside it\n{"customer": {{ body }}, "ref": {{ id }}, "via": {{ global.source }}}   — a JSON template'}
                                        aria-label="Request body"
                                    />
                                    <p className="text-2xs text-text-faint">Empty sends every argument the URL did not use. <code className="font-mono">{'{{ global.key }}'}</code> works here, in the URL and in headers. Put a reference where a value goes, not inside a string, or the body is sent as text.</p>
                                </div>
                            )}

                            {/* Headers */}
                            <div className="space-y-2 pt-2">
                                <div className="flex justify-between items-end mb-1">
                                    <Label className="block">Headers</Label>
                                    <Button variant="ghost" size="sm" onClick={() => setHeaderRows([...headerRows, { id: `h-${Date.now()}`, key: '', value: '' }])}>
                                        <Plus className="size-3.5" aria-hidden /> Add header
                                    </Button>
                                </div>
                                {headerRows.map((row, idx) => (
                                    <div key={row.id} className="flex gap-2 items-center">
                                        <input
                                            type="text"
                                            aria-label={`Header ${idx + 1} name`}
                                            placeholder="Key (e.g. Authorization)"
                                            value={row.key}
                                            onChange={e => {
                                                const newRows = [...headerRows];
                                                newRows[idx].key = e.target.value;
                                                setHeaderRows(newRows);
                                            }}
                                            className="flex-1 bg-surface border border-border-strong px-3 py-2 text-sm text-text focus:border-accent focus:outline-none font-mono rounded-md"
                                        />
                                        <TemplateInput
                                            profile="tool"
                                            placeholder="Value — type {{ for a variable"
                                            value={row.value}
                                            onChange={e => {
                                                const newRows = [...headerRows];
                                                newRows[idx] = { ...row, value: e.target.value };
                                                setHeaderRows(newRows);
                                            }}
                                            className="flex-1"
                                            aria-label="Header value"
                                        />
                                        <button
                                            onClick={() => setHeaderRows(headerRows.filter(r => r.id !== row.id))}
                                            aria-label={`Remove header ${idx + 1}`}
                                            className="inline-flex size-8 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-danger"
                                        >
                                            <Trash className="h-4 w-4" />
                                        </button>
                                    </div>
                                ))}
                            </div>

                            {/* Schemas */}
                            <div className="space-y-1 flex flex-col min-h-[280px]">
                                <Label className="block">Input Schema (JSON)</Label>
                                <CodeEditor
                                    language="json"
                                    lint="json"
                                    format
                                    height={280}
                                    value={draftTool.inputSchemaStr || ''}
                                    onChange={inputSchemaStr => setDraftTool({ ...draftTool, inputSchemaStr })}
                                    placeholder='{"type": "object", "properties": {"msg": {"type": "string"}}}'
                                    aria-label="Input Schema (JSON)"
                                />
                            </div>
                        </div>
                        </TemplateScope>
                    )}

                    {/* ── PYTHON mode ────────────────────────────────────── */}
                    {toolBuilderMode === 'python' && (
                        <div className="flex flex-col gap-4">
                            {/* Docker Status Banner */}
                            {dockerChecking && !dockerStatus ? (
                                <div className="flex items-center gap-2 px-3 py-2 bg-surface border border-border rounded-md text-xs text-text-faint">
                                    <RefreshCw className="h-3.5 w-3.5 animate-spin shrink-0" />
                                    Checking Docker status...
                                </div>
                            ) : dockerStatus && (() => {
                                const allGood = dockerStatus.installed && dockerStatus.running && dockerStatus.image_exists;
                                if (allGood && !dockerBuildError) {
                                    return (
                                        <div className="flex items-center gap-2 px-3 py-2 bg-success-subtle border border-success/25 rounded-lg text-xs text-success">
                                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                                            Docker sandbox is ready
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} aria-label="Check Docker again" className="ml-auto text-success hover:opacity-80 transition-opacity disabled:opacity-50">
                                                <RefreshCw className={`h-3 w-3 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                    );
                                }
                                return (
                                    <div className="p-3 bg-surface-2 border border-border rounded-lg space-y-2">
                                        <div className="flex items-start gap-2">
                                            <AlertTriangle className="h-4 w-4 text-text shrink-0 mt-0.5" />
                                            <div className="flex-1 space-y-1">
                                                {!dockerStatus.installed && (
                                                    <>
                                                        <p className="text-xs font-semibold text-text">Docker is not installed</p>
                                                        <p className="text-xs text-text-muted">Python tools run in a Docker sandbox. Install Docker Desktop to use them.</p>
                                                        <a href="https://docs.docker.com/get-docker/" target="_blank" rel="noreferrer"
                                                            className="inline-flex items-center gap-1 text-xs text-accent-ink hover:text-accent-hover underline">
                                                            Install Docker Desktop <ExternalLink className="h-3 w-3" />
                                                        </a>
                                                    </>
                                                )}
                                                {dockerStatus.installed && !dockerStatus.running && (
                                                    <>
                                                        <p className="text-xs font-semibold text-text">Docker is not running</p>
                                                        <p className="text-xs text-text-muted">Start Docker Desktop, then refresh the status.</p>
                                                    </>
                                                )}
                                                {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                                    <>
                                                        <p className="text-xs font-semibold text-text">Python sandbox image not built</p>
                                                        <p className="text-xs text-text-muted">The sandbox image needs to be built once. This downloads Python and installs packages (~2–3 min).</p>
                                                    </>
                                                )}
                                                {dockerBuildError && (
                                                    <p className="text-xs text-danger font-mono whitespace-pre-wrap mt-1">{dockerBuildError}</p>
                                                )}
                                            </div>
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} aria-label="Check Docker again" className="text-text-faint hover:text-text transition-colors shrink-0 disabled:opacity-50">
                                                <RefreshCw className={`h-3.5 w-3.5 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                        {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                            <button
                                                onClick={buildSandboxImage}
                                                disabled={dockerBuilding}
                                                className={buttonStyles({ variant: "secondary", size: "sm" })}
                                            >
                                                <Container className="h-3.5 w-3.5" />
                                                {dockerBuilding ? 'Building… (this may take a few minutes)' : 'Build Sandbox Image'}
                                            </button>
                                        )}
                                    </div>
                                );
                            })()}
                            <PythonToolEditor
                                draft={draftTool as PythonDraftTool}
                                onChange={(updated) => setDraftTool(updated)}
                            />
                        </div>
                    )}
                </div>
            )}
            </div>
        </div>

        <ConfirmDialog
            open={!!pending}
            onClose={() => setPending(null)}
            onConfirm={() => { const next = pending; setPending(null); next?.(); }}
            title="Discard unsaved changes?"
            description="This tool has edits that are not saved."
            confirmLabel="Discard"
        />
        </AppScreen>
    );
};

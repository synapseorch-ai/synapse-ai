/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react';
import { Wrench, Plus, Trash, X, ExternalLink, AlertTriangle, CheckCircle2, RefreshCw, Container, Import } from 'lucide-react';
import { PythonToolEditor, type PythonDraftTool } from './PythonToolEditor';
import { OpenApiImport } from './OpenApiImport';
import { Button, Label, Select } from '@/components/ui';

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

    // ── Get tool type badge ────────────────────────────────────────────────
    const getToolBadge = (t: any) => {
        if (t.tool_type === 'python') {
            return <span className="text-2xs font-bold bg-accent-subtle border border-accent/40 text-accent px-1.5 py-0.5 rounded-md">🐍 PYTHON</span>;
        }
        if (t.workflowId || t.url?.includes('webhook')) {
            return <span className="text-2xs font-bold bg-warning/30 border border-warning/50 text-warning px-1.5 py-0.5 rounded-md">n8n</span>;
        }
        return <span className="text-2xs font-bold bg-surface-2 border border-border-strong text-text-muted px-1.5 py-0.5 rounded-md">HTTP</span>;
    };

    return (
        <div className="flex flex-col min-h-[600px]">
            {showImport && !draftTool ? (
                /* ── OpenAPI Import View ──────────────────────────────────── */
                <OpenApiImport
                    onClose={() => setShowImport(false)}
                    onImported={(tools) => { onImported(tools); setShowImport(false); }}
                />
            ) : !draftTool ? (
                /* ── List View ───────────────────────────────────────────── */
                <div className="space-y-4">
                    <div className="flex justify-between items-center">
                        <div>
                            <h3 className="text-lg font-bold text-text flex items-center gap-2">
                                <Wrench className="h-5 w-5" /> Custom Tools
                            </h3>
                            <p className="text-text-faint text-sm">
                                Extend your agent with n8n webhooks, HTTP endpoints, or Python functions.
                            </p>
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={() => setShowImport(true)}
                                className="px-3 py-2 bg-surface border border-border-strong text-text-muted font-bold text-xs uppercase flex items-center gap-2 hover:bg-surface-2 hover:text-text transition-colors rounded-md"
                            >
                                <Import className="h-4 w-4" /> Import OpenAPI
                            </button>
                            <button
                                onClick={() => {
                                    const initialInput = { type: 'object', properties: { input: { type: 'string' } } };
                                    setDraftTool({
                                        name: '',
                                        generalName: '',
                                        description: '',
                                        url: '',
                                        method: 'POST',
                                        inputSchema: initialInput,
                                        inputSchemaStr: JSON.stringify(initialInput, null, 2),
                                        outputSchemaStr: '',
                                        tool_type: 'http',
                                    });
                                    setHeaderRows([{ id: 'h1', key: '', value: '' }]);
                                    setToolBuilderMode('config');
                                }}
                                className="px-3 py-2 bg-surface border border-border-strong text-text-muted font-bold text-xs uppercase flex items-center gap-2 hover:bg-surface-2 hover:text-text transition-colors rounded-md"
                            >
                                <Plus className="h-4 w-4" /> New Tool
                            </button>
                        </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {customTools.map((t: any) => (
                            <div key={t.name} className={`p-4 border hover:border-text-faint transition-all group relative ${t.tool_type === 'python' ? 'bg-accent-subtle border-accent/40 hover:border-accent/40' : 'bg-surface border-border'} rounded-md`}>
                                <div className="font-bold text-text mb-1 flex items-center gap-2 pr-10">
                                    <span className="truncate">{t.generalName || t.name}</span>
                                    {getToolBadge(t)}
                                </div>
                                {t.generalName && <div className="text-2xs text-text-faint font-mono mb-1">({t.name})</div>}
                                <div className="text-xs text-text-faint mb-2 h-8 overflow-hidden">{t.description}</div>
                                {t.group && groups.find(g => g.id === t.group) && (
                                    <div className="text-2xs text-accent mb-0.5">{groups.find(g => g.id === t.group)!.name}</div>
                                )}
                                {t.tool_type === 'python'
                                    ? <div className="text-2xs font-mono text-accent">🐍 Python sandboxed function</div>
                                    : <div className="text-2xs font-mono text-text-faint truncate">{t.url}</div>
                                }
                                <button
                                    onClick={() => onDeleteTool(t.name)}
                                    aria-label={`Delete ${t.generalName || t.name}`}
                                    title={`Delete ${t.generalName || t.name}`}
                                    className="absolute top-2 right-2 p-1 text-text-faint hover:text-danger opacity-0 group-hover:opacity-100"
                                >
                                    <Trash className="h-4 w-4" />
                                </button>
                                <button
                                    onClick={() => {
                                        if (t.tool_type === 'python') {
                                            setDraftTool({
                                                ...t,
                                                schemaParams: t.schemaParams || [],
                                            });
                                            setToolBuilderMode('python');
                                        } else {
                                            setDraftTool({
                                                ...t,
                                                inputSchemaStr: JSON.stringify(t.inputSchema || {}, null, 2),
                                                outputSchemaStr: t.outputSchema ? JSON.stringify(t.outputSchema, null, 2) : '',
                                            });
                                            const rows = Object.entries(t.headers || {}).map(([k, v], i) => ({
                                                id: `h-${i}`, key: k, value: v as string
                                            }));
                                            setHeaderRows(rows.length ? rows : [{ id: 'h1', key: '', value: '' }]);
                                            setToolBuilderMode('config');
                                        }
                                    }}
                                    className="absolute bottom-2 right-2 text-2xs text-text-muted hover:text-text font-bold uppercase"
                                >
                                    Edit
                                </button>
                            </div>
                        ))}
                        {customTools.length === 0 && (
                            <div className="col-span-full py-12 text-center text-text-faint italic text-sm border border-dashed border-border">
                                No custom tools yet. Create an HTTP, n8n webhook, or Python tool.
                            </div>
                        )}
                    </div>
                </div>
            ) : (
                /* ── Builder View ────────────────────────────────────────── */
                <div className="flex flex-col h-full">
                    {/* Header */}
                    <div className="flex items-center justify-between mb-4 pb-4 border-b border-border">
                        <div className="flex items-center gap-4">
                            <button
                                onClick={() => { setDraftTool(null); setToolBuilderMode('config'); }}
                                className="text-text-faint hover:text-text"
                            >
                                <X className="h-5 w-5" />
                            </button>
                            <h3 className="font-bold text-text uppercase tracking-wider">
                                {draftTool.name ? `Editing: ${draftTool.name}` : 'New Tool Builder'}
                            </h3>
                        </div>
                        <div className="flex gap-2">
                            {/* Mode tabs */}
                            <div className="flex bg-surface border border-border p-1 rounded-md gap-0.5">
                                <button
                                    onClick={() => {
                                        if (draftTool.tool_type === 'python') {
                                            const initialInput = { type: 'object', properties: { input: { type: 'string' } } };
                                            setDraftTool({ ...draftTool, tool_type: 'http', url: draftTool.url || '', method: draftTool.method || 'POST', inputSchema: initialInput, inputSchemaStr: JSON.stringify(initialInput, null, 2), outputSchemaStr: '' });
                                            setHeaderRows([{ id: 'h1', key: '', value: '' }]);
                                        }
                                        setToolBuilderMode('config');
                                    }}
                                    className={`px-3 py-1 text-xs font-bold rounded-md ${toolBuilderMode === 'config' ? 'bg-surface-2 text-text' : 'text-text-faint hover:text-text'}`}
                                >
                                    CONFIG
                                </button>
                                <button
                                    onClick={() => {
                                        if (draftTool.tool_type !== 'python') {
                                            const defaultCode = `# _args contains all tool arguments as a Python dict\n# print() stdout becomes the tool result\nimport json\n\nresult = {"output": _args.get("input", "")}\nprint(json.dumps(result))\n`;
                                            setDraftTool({ ...draftTool, tool_type: 'python', code: draftTool.code || defaultCode, inputSchema: { type: 'object', properties: { input: { type: 'string' } } }, schemaParams: draftTool.schemaParams || [{ id: 'p1', name: 'input', type: 'string', description: 'The input value', required: false }] });
                                        }
                                        setToolBuilderMode('python');
                                    }}
                                    className={`px-3 py-1 text-xs font-bold rounded-md ${toolBuilderMode === 'python' ? 'bg-accent text-accent-fg' : 'text-text-faint hover:text-text'}`}
                                >
                                    🐍 PYTHON
                                </button>
                            </div>
                            <button
                                onClick={onSaveTool}
                                className="px-4 py-1.5 bg-accent text-accent-fg text-xs font-bold hover:bg-accent-hover"
                            >
                                SAVE
                            </button>
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
                                className="w-full bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none placeholder:text-text-faint rounded-md"
                                placeholder="e.g. Process Orders"
                            />
                        </div>
                        <div className="space-y-1">
                            <Label htmlFor="cus-drafttool-name" className="block">System Name (Snake Case)</Label>
                            <input id="cus-drafttool-name"
                                type="text"
                                value={draftTool.name}
                                onChange={e => setDraftTool({ ...draftTool, name: e.target.value })}
                                className="w-full bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none font-mono placeholder:text-text-faint rounded-md"
                                placeholder="e.g. process_orders"
                            />
                        </div>
                    </div>
                    <div className="space-y-1 mb-5">
                        <Label htmlFor="cus-drafttool-description" className="block">Description (For AI)</Label>
                        <textarea id="cus-drafttool-description"
                            value={draftTool.description}
                            onChange={e => setDraftTool({ ...draftTool, description: e.target.value })}
                            className="w-full bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none resize-vertical min-h-[80px] rounded-md"
                            placeholder="What does this tool do? Describe its purpose and critical rules for the AI..."
                        />
                    </div>

                    {/* ── CONFIG mode (HTTP/n8n tool) ────────────────────── */}
                    {toolBuilderMode === 'config' && (
                        <div className="space-y-5 pr-2">
                            {/* Method */}
                            <div className="space-y-1">
                                <Label className="block">Method</Label>
                                <Select
                                    value={draftTool.method || 'POST'}
                                    onChange={v => setDraftTool({ ...draftTool, method: v })}
                                    aria-label="HTTP method"
                                    options={['POST', 'GET', 'PUT', 'DELETE'].map(m => ({ value: m, label: m }))}
                                />
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
                                        placeholder="New group name…"
                                        className="w-44 bg-surface border border-border p-2 text-xs text-text focus:border-border-strong focus:outline-none rounded-md"
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
                                            className="inline-flex items-center gap-1 text-2xs text-[#ff6d5a] hover:text-[#ff8a78] hover:underline mt-1"
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
                                            <a href="/settings/workspace" className="text-[#ff6d5a] hover:underline">
                                                integrate n8n workflows
                                            </a>
                                            )
                                        </span>
                                    )}
                                </div>
                                <input
                                    type="text"
                                    value={draftTool.url}
                                    onChange={e => setDraftTool({ ...draftTool, url: e.target.value })}
                                    className="w-full bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none font-mono rounded-md"
                                    placeholder="https://api.example.com/users/{id}"
                                />
                                {/* URL templating hint — dynamic based on input schema */}
                                {(() => {
                                    let keys: string[] = [];
                                    try {
                                        const schema = JSON.parse(draftTool.inputSchemaStr || '{}');
                                        keys = Object.keys(schema?.properties || {});
                                    } catch { /* ignore parse errors */ }
                                    const examples = keys.length
                                        ? keys.slice(0, 3).map(k => `{${k}}`).join(', ')
                                        : '{field}';
                                    return (
                                        <p className="mt-1 text-2xs text-text-faint leading-relaxed">
                                            💡 Use <code className="font-code text-text-muted bg-surface px-1 rounded-md">{examples}</code> in the URL to inject input values.
                                            {' '}GET/DELETE args become query params; POST/PUT go in the body.
                                        </p>
                                    );
                                })()}
                            </div>

                            {/* Body template — POST/PUT/DELETE */}
                            {(draftTool.method || 'POST') !== 'GET' && (
                                <div className="space-y-1">
                                    <Label htmlFor="cus-drafttool-body" className="block">Request body <span className="text-text-faint font-normal normal-case">(optional)</span></Label>
                                    <textarea id="cus-drafttool-body"
                                        value={draftTool.body || ''}
                                        onChange={e => setDraftTool({ ...draftTool, body: e.target.value })}
                                        className="w-full h-20 bg-bg border border-border p-2 text-2xs font-mono text-text focus:border-border-strong focus:outline-none resize-y rounded-md"
                                        placeholder={'{{body}}   — the "body" argument is the request body\n{{body.data}}   — a key inside it\n{"customer": {{body}}, "ref": "{{id}}", "via": "{{global.source}}"}   — a JSON template'}
                                    />
                                    <p className="text-2xs text-text-faint">Empty sends every argument the URL did not use. <code className="font-mono">{'{{global.key}}'}</code> works here, in the URL and in headers.</p>
                                </div>
                            )}

                            {/* Headers */}
                            <div className="space-y-2 pt-2">
                                <div className="flex justify-between items-end mb-1">
                                    <Label className="block">Headers</Label>
                                    <button
                                        onClick={() => setHeaderRows([...headerRows, { id: `h-${Date.now()}`, key: '', value: '' }])}
                                        className="text-2xs text-text-muted hover:text-text font-bold bg-surface-2 px-2 py-1 rounded-md transition-colors"
                                    >
                                        + ADD HEADER
                                    </button>
                                </div>
                                {headerRows.map((row, idx) => (
                                    <div key={row.id} className="flex gap-2 items-center">
                                        <input
                                            type="text"
                                            placeholder="Key (e.g. Authorization)"
                                            value={row.key}
                                            onChange={e => {
                                                const newRows = [...headerRows];
                                                newRows[idx].key = e.target.value;
                                                setHeaderRows(newRows);
                                            }}
                                            className="flex-1 bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none font-mono rounded-md"
                                        />
                                        <input
                                            type="text"
                                            placeholder="Value"
                                            value={row.value}
                                            onChange={e => {
                                                const newRows = [...headerRows];
                                                newRows[idx].value = e.target.value;
                                                setHeaderRows(newRows);
                                            }}
                                            className="flex-1 bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none font-mono rounded-md"
                                        />
                                        <button
                                            onClick={() => setHeaderRows(headerRows.filter(r => r.id !== row.id))}
                                            className="p-2 text-text-faint hover:text-danger transition-colors"
                                        >
                                            <Trash className="h-4 w-4" />
                                        </button>
                                    </div>
                                ))}
                            </div>

                            {/* Schemas */}
                            <div className="space-y-1 flex flex-col min-h-[280px]">
                                <Label htmlFor="cus-drafttool-inputschemastr" className="block">Input Schema (JSON)</Label>
                                <textarea id="cus-drafttool-inputschemastr"
                                    value={draftTool.inputSchemaStr}
                                    onChange={e => setDraftTool({ ...draftTool, inputSchemaStr: e.target.value })}
                                    className="w-full flex-1 bg-bg border border-border p-3 text-2xs font-mono text-text focus:border-border-strong focus:outline-none resize-none rounded-md"
                                    placeholder='{"type": "object", "properties": {"msg": {"type": "string"}}}'
                                />
                            </div>
                        </div>
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
                                        <div className="flex items-center gap-2 px-3 py-2 bg-success/30 border border-success/40 rounded-md text-xs text-success">
                                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                                            Docker sandbox is ready
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} className="ml-auto text-success hover:opacity-80 transition-opacity disabled:opacity-50">
                                                <RefreshCw className={`h-3 w-3 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                    );
                                }
                                return (
                                    <div className="p-3 bg-warning/30 border border-warning/50 rounded-md space-y-2">
                                        <div className="flex items-start gap-2">
                                            <AlertTriangle className="h-4 w-4 text-warning shrink-0 mt-0.5" />
                                            <div className="flex-1 space-y-1">
                                                {!dockerStatus.installed && (
                                                    <>
                                                        <p className="text-xs font-semibold text-warning">Docker is not installed</p>
                                                        <p className="text-xs text-warning/80">Python tools run in a Docker sandbox. Install Docker Desktop to use them.</p>
                                                        <a href="https://docs.docker.com/get-docker/" target="_blank" rel="noreferrer"
                                                            className="inline-flex items-center gap-1 text-xs text-warning hover:opacity-80 transition-opacity underline">
                                                            Install Docker Desktop <ExternalLink className="h-3 w-3" />
                                                        </a>
                                                    </>
                                                )}
                                                {dockerStatus.installed && !dockerStatus.running && (
                                                    <>
                                                        <p className="text-xs font-semibold text-warning">Docker is not running</p>
                                                        <p className="text-xs text-warning/80">Start Docker Desktop, then refresh the status.</p>
                                                    </>
                                                )}
                                                {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                                    <>
                                                        <p className="text-xs font-semibold text-warning">Python sandbox image not built</p>
                                                        <p className="text-xs text-warning/80">The sandbox image needs to be built once. This downloads Python and installs packages (~2–3 min).</p>
                                                    </>
                                                )}
                                                {dockerBuildError && (
                                                    <p className="text-xs text-danger font-mono whitespace-pre-wrap mt-1">{dockerBuildError}</p>
                                                )}
                                            </div>
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} className="text-warning hover:opacity-80 transition-opacity shrink-0 disabled:opacity-50">
                                                <RefreshCw className={`h-3.5 w-3.5 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                        {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                            <button
                                                onClick={buildSandboxImage}
                                                disabled={dockerBuilding}
                                                className="flex items-center gap-2 px-3 py-1.5 bg-warning hover:opacity-90 transition-opacity disabled:opacity-50 text-warning-subtle text-xs font-bold transition-colors rounded-md"
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
    );
};

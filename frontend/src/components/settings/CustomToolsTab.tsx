/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react';
import { Wrench, Plus, Trash, X, ExternalLink, AlertTriangle, CheckCircle2, RefreshCw, Container, Import } from 'lucide-react';
import { PythonToolEditor, type PythonDraftTool } from './PythonToolEditor';
import { OpenApiImport } from './OpenApiImport';

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
            return <span className="text-[9px] font-bold bg-violet-900/40 border border-violet-700 text-violet-400 px-1.5 py-0.5 rounded">🐍 PYTHON</span>;
        }
        if (t.workflowId || t.url?.includes('webhook')) {
            return <span className="text-[9px] font-bold bg-orange-900/30 border border-orange-700/50 text-orange-400 px-1.5 py-0.5 rounded">n8n</span>;
        }
        return <span className="text-[9px] font-bold bg-zinc-800 border border-zinc-700 text-zinc-400 px-1.5 py-0.5 rounded">HTTP</span>;
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
                            <h3 className="text-lg font-bold text-white flex items-center gap-2">
                                <Wrench className="h-5 w-5" /> Custom Tools
                            </h3>
                            <p className="text-zinc-500 text-sm">
                                Extend your agent with n8n webhooks, HTTP endpoints, or Python functions.
                            </p>
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={() => setShowImport(true)}
                                className="px-3 py-2 bg-zinc-800 border border-zinc-700 text-zinc-300 font-bold text-xs uppercase flex items-center gap-2 hover:bg-zinc-700 hover:text-white transition-colors"
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
                                className="px-3 py-2 bg-zinc-800 border border-zinc-700 text-zinc-300 font-bold text-xs uppercase flex items-center gap-2 hover:bg-zinc-700 hover:text-white transition-colors"
                            >
                                <Plus className="h-4 w-4" /> New Tool
                            </button>
                        </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {customTools.map((t: any) => (
                            <div key={t.name} className={`p-4 border hover:border-zinc-600 transition-all group relative ${t.tool_type === 'python' ? 'bg-violet-950/10 border-violet-900/30 hover:border-violet-700/50' : 'bg-zinc-900 border-zinc-800'}`}>
                                <div className="font-bold text-white mb-1 flex items-center gap-2 pr-10">
                                    <span className="truncate">{t.generalName || t.name}</span>
                                    {getToolBadge(t)}
                                </div>
                                {t.generalName && <div className="text-[9px] text-zinc-500 font-mono mb-1">({t.name})</div>}
                                <div className="text-xs text-zinc-500 mb-2 h-8 overflow-hidden">{t.description}</div>
                                {t.tool_type === 'python'
                                    ? <div className="text-[10px] font-mono text-violet-600">🐍 Python sandboxed function</div>
                                    : <div className="text-[10px] font-mono text-zinc-600 truncate">{t.url}</div>
                                }
                                <button
                                    onClick={() => onDeleteTool(t.name)}
                                    className="absolute top-2 right-2 p-1 text-zinc-600 hover:text-red-500 opacity-0 group-hover:opacity-100"
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
                                    className="absolute bottom-2 right-2 text-[10px] text-zinc-400 hover:text-white font-bold uppercase"
                                >
                                    Edit
                                </button>
                            </div>
                        ))}
                        {customTools.length === 0 && (
                            <div className="col-span-full py-12 text-center text-zinc-600 italic text-sm border border-dashed border-zinc-800">
                                No custom tools yet. Create an HTTP, n8n webhook, or Python tool.
                            </div>
                        )}
                    </div>
                </div>
            ) : (
                /* ── Builder View ────────────────────────────────────────── */
                <div className="flex flex-col h-full">
                    {/* Header */}
                    <div className="flex items-center justify-between mb-4 pb-4 border-b border-zinc-800">
                        <div className="flex items-center gap-4">
                            <button
                                onClick={() => { setDraftTool(null); setToolBuilderMode('config'); }}
                                className="text-zinc-500 hover:text-white"
                            >
                                <X className="h-5 w-5" />
                            </button>
                            <h3 className="font-bold text-white uppercase tracking-wider">
                                {draftTool.name ? `Editing: ${draftTool.name}` : 'New Tool Builder'}
                            </h3>
                        </div>
                        <div className="flex gap-2">
                            {/* Mode tabs */}
                            <div className="flex bg-zinc-900 border border-zinc-800 p-1 rounded gap-0.5">
                                <button
                                    onClick={() => {
                                        if (draftTool.tool_type === 'python') {
                                            const initialInput = { type: 'object', properties: { input: { type: 'string' } } };
                                            setDraftTool({ ...draftTool, tool_type: 'http', url: draftTool.url || '', method: draftTool.method || 'POST', inputSchema: initialInput, inputSchemaStr: JSON.stringify(initialInput, null, 2), outputSchemaStr: '' });
                                            setHeaderRows([{ id: 'h1', key: '', value: '' }]);
                                        }
                                        setToolBuilderMode('config');
                                    }}
                                    className={`px-3 py-1 text-xs font-bold rounded ${toolBuilderMode === 'config' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
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
                                    className={`px-3 py-1 text-xs font-bold rounded ${toolBuilderMode === 'python' ? 'bg-violet-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                                >
                                    🐍 PYTHON
                                </button>
                            </div>
                            <button
                                onClick={onSaveTool}
                                className="px-4 py-1.5 bg-white text-black text-xs font-bold hover:bg-zinc-200"
                            >
                                SAVE
                            </button>
                        </div>
                    </div>

                    {/* Shared name / description fields */}
                    <div className="grid grid-cols-2 gap-4 mb-4">
                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-zinc-500">General Name</label>
                            <input
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
                                className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none placeholder:text-zinc-700"
                                placeholder="e.g. Process Orders"
                            />
                        </div>
                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-zinc-500">System Name (Snake Case)</label>
                            <input
                                type="text"
                                value={draftTool.name}
                                onChange={e => setDraftTool({ ...draftTool, name: e.target.value })}
                                className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none font-mono placeholder:text-zinc-700"
                                placeholder="e.g. process_orders"
                            />
                        </div>
                    </div>
                    <div className="space-y-1 mb-5">
                        <label className="text-[10px] uppercase font-bold text-zinc-500">Description (For AI)</label>
                        <textarea
                            value={draftTool.description}
                            onChange={e => setDraftTool({ ...draftTool, description: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none resize-vertical min-h-[80px]"
                            placeholder="What does this tool do? Describe its purpose and critical rules for the AI..."
                        />
                    </div>

                    {/* ── CONFIG mode (HTTP/n8n tool) ────────────────────── */}
                    {toolBuilderMode === 'config' && (
                        <div className="space-y-5 pr-2">
                            {/* Method */}
                            <div className="space-y-1">
                                <label className="text-[10px] uppercase font-bold text-zinc-500">Method</label>
                                <select
                                    value={draftTool.method}
                                    onChange={e => setDraftTool({ ...draftTool, method: e.target.value })}
                                    className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none"
                                >
                                    <option>POST</option>
                                    <option>GET</option>
                                    <option>PUT</option>
                                    <option>DELETE</option>
                                </select>
                            </div>

                            {/* n8n Workflow — only shown when n8n is integrated */}
                            {n8nIntegrated && (
                                <div className="space-y-1">
                                    <label className="text-[10px] uppercase font-bold text-zinc-500">n8n Workflow</label>
                                    <select
                                        value={draftTool.workflowId || ''}
                                        onChange={async (e) => {
                                            const workflowId = e.target.value;
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
                                        className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none"
                                    >
                                        <option value="">{n8nWorkflowsLoading ? 'Loading workflows...' : 'Select a workflow (optional)'}</option>
                                        {Array.isArray(n8nWorkflows) && n8nWorkflows.map((w: any) => (
                                            <option key={String(w.id)} value={String(w.id)}>
                                                {w.name || w.id}
                                            </option>
                                        ))}
                                    </select>
                                    {draftTool.workflowId ? (
                                        <a
                                            href={`${getN8nBaseUrl()}/workflow/${draftTool.workflowId}`}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-[10px] text-[#ff6d5a] hover:text-[#ff8a78] hover:underline mt-1"
                                        >
                                            Open workflow in n8n <ExternalLink className="h-3 w-3" />
                                        </a>
                                    ) : (
                                        <a
                                            href={`${getN8nBaseUrl()}/workflow/new`}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-zinc-300 hover:underline mt-1"
                                        >
                                            Create new workflow in n8n <ExternalLink className="h-3 w-3" />
                                        </a>
                                    )}
                                </div>
                            )}

                            {/* URL */}
                            <div className="space-y-1">
                                <div className="flex items-center gap-2">
                                    <label className="text-[10px] uppercase font-bold text-zinc-500">URL</label>
                                    {!n8nIntegrated && (
                                        <span className="text-[9px] text-zinc-600">
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
                                    className="w-full bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none font-mono"
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
                                        <p className="mt-1 text-[10px] text-zinc-600 leading-relaxed">
                                            💡 Use <code className="text-zinc-400 bg-zinc-900 px-1 rounded">{examples}</code> in the URL to inject input values.
                                            {' '}GET/DELETE args become query params; POST/PUT go in the body.
                                        </p>
                                    );
                                })()}
                            </div>

                            {/* Headers */}
                            <div className="space-y-2 pt-2">
                                <div className="flex justify-between items-end mb-1">
                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Headers</label>
                                    <button
                                        onClick={() => setHeaderRows([...headerRows, { id: `h-${Date.now()}`, key: '', value: '' }])}
                                        className="text-[10px] text-zinc-400 hover:text-white font-bold bg-zinc-800 px-2 py-1 rounded transition-colors"
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
                                            className="flex-1 bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none font-mono"
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
                                            className="flex-1 bg-zinc-900 border border-zinc-800 p-2 text-sm text-white focus:border-white focus:outline-none font-mono"
                                        />
                                        <button
                                            onClick={() => setHeaderRows(headerRows.filter(r => r.id !== row.id))}
                                            className="p-2 text-zinc-600 hover:text-red-500 transition-colors"
                                        >
                                            <Trash className="h-4 w-4" />
                                        </button>
                                    </div>
                                ))}
                            </div>

                            {/* Schemas */}
                            <div className="space-y-1 flex flex-col min-h-[280px]">
                                <label className="text-[10px] uppercase font-bold text-zinc-500">Input Schema (JSON)</label>
                                <textarea
                                    value={draftTool.inputSchemaStr}
                                    onChange={e => setDraftTool({ ...draftTool, inputSchemaStr: e.target.value })}
                                    className="w-full flex-1 bg-zinc-950 border border-zinc-800 p-3 text-[10px] font-mono text-zinc-300 focus:border-white focus:outline-none resize-none"
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
                                <div className="flex items-center gap-2 px-3 py-2 bg-zinc-900 border border-zinc-800 rounded text-xs text-zinc-500">
                                    <RefreshCw className="h-3.5 w-3.5 animate-spin shrink-0" />
                                    Checking Docker status...
                                </div>
                            ) : dockerStatus && (() => {
                                const allGood = dockerStatus.installed && dockerStatus.running && dockerStatus.image_exists;
                                if (allGood && !dockerBuildError) {
                                    return (
                                        <div className="flex items-center gap-2 px-3 py-2 bg-green-950/30 border border-green-800/40 rounded text-xs text-green-400">
                                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                                            Docker sandbox is ready
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} className="ml-auto text-green-700 hover:text-green-400 disabled:opacity-50">
                                                <RefreshCw className={`h-3 w-3 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                    );
                                }
                                return (
                                    <div className="p-3 bg-amber-950/30 border border-amber-700/50 rounded space-y-2">
                                        <div className="flex items-start gap-2">
                                            <AlertTriangle className="h-4 w-4 text-amber-400 shrink-0 mt-0.5" />
                                            <div className="flex-1 space-y-1">
                                                {!dockerStatus.installed && (
                                                    <>
                                                        <p className="text-xs font-semibold text-amber-300">Docker is not installed</p>
                                                        <p className="text-xs text-amber-500/80">Python tools run in a Docker sandbox. Install Docker Desktop to use them.</p>
                                                        <a href="https://docs.docker.com/get-docker/" target="_blank" rel="noreferrer"
                                                            className="inline-flex items-center gap-1 text-xs text-amber-400 hover:text-amber-200 underline">
                                                            Install Docker Desktop <ExternalLink className="h-3 w-3" />
                                                        </a>
                                                    </>
                                                )}
                                                {dockerStatus.installed && !dockerStatus.running && (
                                                    <>
                                                        <p className="text-xs font-semibold text-amber-300">Docker is not running</p>
                                                        <p className="text-xs text-amber-500/80">Start Docker Desktop, then refresh the status.</p>
                                                    </>
                                                )}
                                                {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                                    <>
                                                        <p className="text-xs font-semibold text-amber-300">Python sandbox image not built</p>
                                                        <p className="text-xs text-amber-500/80">The sandbox image needs to be built once. This downloads Python and installs packages (~2–3 min).</p>
                                                    </>
                                                )}
                                                {dockerBuildError && (
                                                    <p className="text-xs text-red-400 font-mono whitespace-pre-wrap mt-1">{dockerBuildError}</p>
                                                )}
                                            </div>
                                            <button onClick={checkDockerStatus} disabled={dockerChecking} className="text-amber-700 hover:text-amber-400 shrink-0 disabled:opacity-50">
                                                <RefreshCw className={`h-3.5 w-3.5 ${dockerChecking ? 'animate-spin' : ''}`} />
                                            </button>
                                        </div>
                                        {dockerStatus.installed && dockerStatus.running && !dockerStatus.image_exists && (
                                            <button
                                                onClick={buildSandboxImage}
                                                disabled={dockerBuilding}
                                                className="flex items-center gap-2 px-3 py-1.5 bg-amber-700 hover:bg-amber-600 disabled:opacity-50 text-white text-xs font-bold transition-colors rounded"
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

/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState } from 'react';
import { X, FileUp, Loader2, CheckSquare, Square, Import } from 'lucide-react';

interface OpenApiImportProps {
    /** Return to the tool list without importing. */
    onClose: () => void;
    /** Called with the tools that were successfully imported (already saved). */
    onImported: (tools: any[]) => void;
}

/**
 * Import many HTTP tools at once from an OpenAPI 3.x / Swagger 2.0 spec.
 * Previews the generated tools (via /api/tools/import/openapi) so the user can
 * pick a subset, then saves the selection (via /api/tools/custom/bulk).
 */
export const OpenApiImport = ({ onClose, onImported }: OpenApiImportProps) => {
    const [specText, setSpecText] = useState('');
    const [baseUrl, setBaseUrl] = useState('');
    const [authKey, setAuthKey] = useState('Authorization');
    const [authValue, setAuthValue] = useState('');
    const [namePrefix, setNamePrefix] = useState('');
    const [preview, setPreview] = useState<any[] | null>(null);
    const [selected, setSelected] = useState<Record<string, boolean>>({});
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const buildHeaders = () => {
        const h: Record<string, string> = {};
        if (authKey.trim() && authValue.trim()) h[authKey.trim()] = authValue.trim();
        return h;
    };

    const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        setSpecText(await file.text());
        setPreview(null);
        e.target.value = '';
    };

    const doPreview = async () => {
        if (!specText.trim()) { setError('Paste or upload a spec first.'); return; }
        setError(null); setLoading(true); setPreview(null);
        try {
            const res = await fetch('/api/tools/import/openapi', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    spec: specText,
                    base_url: baseUrl.trim() || null,
                    headers: buildHeaders(),
                    name_prefix: namePrefix.trim(),
                }),
            });
            const data = await res.json();
            if (!res.ok) { setError(data.detail || 'Failed to parse spec'); return; }
            setPreview(data.tools);
            const sel: Record<string, boolean> = {};
            (data.tools || []).forEach((t: any) => { sel[t.name] = true; });
            setSelected(sel);
        } catch (e) {
            setError(String(e));
        } finally {
            setLoading(false);
        }
    };

    const doImport = async () => {
        if (!preview) return;
        const chosen = preview.filter(t => selected[t.name]);
        if (!chosen.length) { setError('Select at least one tool to import.'); return; }
        setError(null); setLoading(true);
        try {
            const res = await fetch('/api/tools/custom/bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tools: chosen }),
            });
            if (!res.ok) { const d = await res.json(); setError(d.detail || 'Import failed'); return; }
            onImported(chosen);
        } catch (e) {
            setError(String(e));
        } finally {
            setLoading(false);
        }
    };

    const toggle = (name: string) => setSelected(s => ({ ...s, [name]: !s[name] }));
    const allSelected = !!preview && preview.length > 0 && preview.every(t => selected[t.name]);
    const toggleAll = () => {
        if (!preview) return;
        const val = !allSelected;
        const sel: Record<string, boolean> = {};
        preview.forEach(t => { sel[t.name] = val; });
        setSelected(sel);
    };
    const selectedCount = preview ? preview.filter(t => selected[t.name]).length : 0;

    return (
        <div className="space-y-4">
            {/* Header */}
            <div className="flex items-center justify-between pb-4 border-b border-zinc-800">
                <div className="flex items-center gap-3">
                    <button onClick={onClose} className="text-zinc-500 hover:text-white">
                        <X className="h-5 w-5" />
                    </button>
                    <div>
                        <h3 className="text-lg font-bold text-white flex items-center gap-2">
                            <Import className="h-5 w-5" /> Import from OpenAPI / Swagger
                        </h3>
                        <p className="text-zinc-500 text-sm">
                            Paste or upload an OpenAPI 3.x / Swagger 2.0 spec (JSON or YAML) to generate HTTP tools.
                        </p>
                    </div>
                </div>
            </div>

            {/* Spec input */}
            <div className="space-y-2">
                <div className="flex items-center justify-between">
                    <label className="text-xs font-bold uppercase tracking-wider text-zinc-400">Spec (JSON or YAML)</label>
                    <label className="px-2 py-1 bg-zinc-800 border border-zinc-700 text-zinc-300 text-[10px] font-bold uppercase flex items-center gap-1.5 cursor-pointer hover:bg-zinc-700 hover:text-white transition-colors">
                        <FileUp className="h-3.5 w-3.5" /> Upload file
                        <input type="file" accept=".json,.yaml,.yml,application/json,text/yaml" onChange={handleFile} className="hidden" />
                    </label>
                </div>
                <textarea
                    value={specText}
                    onChange={e => { setSpecText(e.target.value); setPreview(null); }}
                    placeholder='{"openapi": "3.0.0", "servers": [...], "paths": {...}}'
                    className="w-full h-40 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs font-mono p-3 focus:border-zinc-600 focus:outline-none resize-y"
                />
            </div>

            {/* Options */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                    <label className="text-xs font-bold uppercase tracking-wider text-zinc-400">Base URL override <span className="text-zinc-600 normal-case font-normal">(optional)</span></label>
                    <input
                        value={baseUrl}
                        onChange={e => setBaseUrl(e.target.value)}
                        placeholder="https://api.example.com/v1"
                        className="w-full mt-1 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs p-2 focus:border-zinc-600 focus:outline-none"
                    />
                </div>
                <div>
                    <label className="text-xs font-bold uppercase tracking-wider text-zinc-400">Name prefix <span className="text-zinc-600 normal-case font-normal">(optional)</span></label>
                    <input
                        value={namePrefix}
                        onChange={e => setNamePrefix(e.target.value)}
                        placeholder="petstore"
                        className="w-full mt-1 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs p-2 focus:border-zinc-600 focus:outline-none"
                    />
                </div>
                <div className="md:col-span-2">
                    <label className="text-xs font-bold uppercase tracking-wider text-zinc-400">Auth header <span className="text-zinc-600 normal-case font-normal">(optional — applied to every tool)</span></label>
                    <div className="flex gap-2 mt-1">
                        <input
                            value={authKey}
                            onChange={e => setAuthKey(e.target.value)}
                            placeholder="Authorization"
                            className="w-1/3 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs p-2 focus:border-zinc-600 focus:outline-none"
                        />
                        <input
                            value={authValue}
                            onChange={e => setAuthValue(e.target.value)}
                            placeholder="Bearer <token>"
                            className="flex-1 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs p-2 focus:border-zinc-600 focus:outline-none"
                        />
                    </div>
                </div>
            </div>

            {error && (
                <div className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 px-3 py-2">{error}</div>
            )}

            {/* Preview action */}
            <div className="flex items-center gap-2">
                <button
                    onClick={doPreview}
                    disabled={loading}
                    className="px-3 py-2 bg-zinc-800 border border-zinc-700 text-zinc-200 font-bold text-xs uppercase flex items-center gap-2 hover:bg-zinc-700 hover:text-white transition-colors disabled:opacity-50"
                >
                    {loading && !preview ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                    Preview tools
                </button>
            </div>

            {/* Preview list */}
            {preview && (
                <div className="space-y-2">
                    <div className="flex items-center justify-between border-t border-zinc-800 pt-3">
                        <button onClick={toggleAll} className="flex items-center gap-2 text-xs text-zinc-400 hover:text-white">
                            {allSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                            {preview.length} operation{preview.length === 1 ? '' : 's'} found
                        </button>
                        <span className="text-[10px] text-zinc-500 uppercase font-bold">{selectedCount} selected</span>
                    </div>
                    {preview.length === 0 ? (
                        <div className="py-6 text-center text-zinc-600 italic text-sm border border-dashed border-zinc-800">
                            No operations found in this spec.
                        </div>
                    ) : (
                        <div className="max-h-64 overflow-y-auto border border-zinc-800 divide-y divide-zinc-800">
                            {preview.map(t => (
                                <button
                                    key={t.name}
                                    onClick={() => toggle(t.name)}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-zinc-900/60"
                                >
                                    {selected[t.name] ? <CheckSquare className="h-4 w-4 text-emerald-400 shrink-0" /> : <Square className="h-4 w-4 text-zinc-600 shrink-0" />}
                                    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded shrink-0 ${
                                        t.method === 'GET' ? 'bg-sky-900/40 text-sky-300 border border-sky-800' :
                                        t.method === 'DELETE' ? 'bg-red-900/40 text-red-300 border border-red-800' :
                                        'bg-amber-900/30 text-amber-300 border border-amber-800/60'
                                    }`}>{t.method}</span>
                                    <span className="font-mono text-xs text-zinc-300 truncate">{t.name}</span>
                                    <span className="font-mono text-[10px] text-zinc-600 truncate ml-auto">{t.url}</span>
                                </button>
                            ))}
                        </div>
                    )}
                    <div className="flex justify-end gap-2 pt-1">
                        <button onClick={onClose} className="px-3 py-2 text-zinc-400 hover:text-white font-bold text-xs uppercase">Cancel</button>
                        <button
                            onClick={doImport}
                            disabled={loading || selectedCount === 0}
                            className="px-4 py-2 bg-emerald-800/80 border border-emerald-700 text-white font-bold text-xs uppercase flex items-center gap-2 hover:bg-emerald-700 transition-colors disabled:opacity-50"
                        >
                            {loading && preview ? <Loader2 className="h-4 w-4 animate-spin" /> : <Import className="h-4 w-4" />}
                            Import {selectedCount || ''} tool{selectedCount === 1 ? '' : 's'}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState } from 'react';
import { X, FileUp, CheckSquare, Square, Import } from 'lucide-react';

import { Spinner, buttonStyles } from '@/components/ui';
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
    // One group per import (the picker shows it as one row). Filled from the
    // spec's title once previewed; editable before saving.
    const [groupName, setGroupName] = useState('');
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
                    group: groupName.trim() || null,
                }),
            });
            const data = await res.json();
            if (!res.ok) { setError(data.detail || 'Failed to parse spec'); return; }
            setPreview(data.tools);
            if (!groupName.trim() && data.group?.name) setGroupName(data.group.name);
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
            <div className="flex items-center justify-between pb-4 border-b border-border">
                <div className="flex items-center gap-3">
                    <button onClick={onClose} aria-label="Close the OpenAPI import" className="inline-flex size-8 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-text">
                        <X className="h-5 w-5" />
                    </button>
                    <div>
                        <h3 className="text-section text-text flex items-center gap-2">
                            <Import className="h-5 w-5" /> Import from OpenAPI / Swagger
                        </h3>
                        <p className="text-text-faint text-sm">
                            Paste or upload an OpenAPI 3.x / Swagger 2.0 spec (JSON or YAML) to generate HTTP tools.
                        </p>
                    </div>
                </div>
            </div>

            {/* Spec input */}
            <div className="space-y-2">
                <div className="flex items-center justify-between">
                    <label className="text-xs font-medium text-text-muted">Spec (JSON or YAML)</label>
                    <label className="relative px-2.5 py-1 bg-surface border border-border-strong text-text-muted text-xs font-medium flex items-center gap-1.5 cursor-pointer hover:bg-surface-2 hover:text-text transition-colors rounded-md focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent">
                        <FileUp className="h-3.5 w-3.5" /> Upload file
                        <input type="file" accept=".json,.yaml,.yml,application/json,text/yaml" onChange={handleFile} className="sr-only" />
                    </label>
                </div>
                <textarea
                    aria-label="Spec (JSON or YAML)"
                    value={specText}
                    onChange={e => { setSpecText(e.target.value); setPreview(null); }}
                    placeholder='{"openapi": "3.0.0", "servers": [...], "paths": {...}}'
                    className="w-full h-40 bg-surface border border-border-strong text-text text-xs font-mono p-3 focus:border-accent focus:outline-none resize-y rounded-md"
                />
            </div>

            {/* Options */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                    <label htmlFor="openapiimport-base-url-override-optional" className="text-xs font-medium text-text-muted">Base URL override <span className="text-text-faint normal-case font-normal">(optional)</span></label>
                    <input
                        id="openapiimport-base-url-override-optional"
                        value={baseUrl}
                        onChange={e => setBaseUrl(e.target.value)}
                        placeholder="https://api.example.com/v1"
                        className="w-full mt-1 bg-surface border border-border-strong text-text text-sm px-3 py-2 focus:border-accent focus:outline-none rounded-md"
                    />
                </div>
                <div>
                    <label htmlFor="openapiimport-name-prefix-optional" className="text-xs font-medium text-text-muted">Name prefix <span className="text-text-faint normal-case font-normal">(optional)</span></label>
                    <input
                        id="openapiimport-name-prefix-optional"
                        value={namePrefix}
                        onChange={e => setNamePrefix(e.target.value)}
                        placeholder="petstore"
                        className="w-full mt-1 bg-surface border border-border-strong text-text text-sm px-3 py-2 focus:border-accent focus:outline-none rounded-md"
                    />
                </div>
                <div className="md:col-span-2">
                    <label htmlFor="openapiimport-group-one-per-import-the-age" className="text-xs font-medium text-text-muted">Group <span className="text-text-faint normal-case font-normal">(one per import — the agent picker shows it as one row; defaults to the spec&apos;s title)</span></label>
                    <input
                        id="openapiimport-group-one-per-import-the-age"
                        value={groupName}
                        onChange={e => setGroupName(e.target.value)}
                        placeholder="Pet Store API"
                        className="w-full mt-1 bg-surface border border-border-strong text-text text-sm px-3 py-2 focus:border-accent focus:outline-none rounded-md"
                    />
                </div>
                <div className="md:col-span-2">
                    <label className="text-xs font-medium text-text-muted">Auth header <span className="text-text-faint normal-case font-normal">(optional — applied to every tool)</span></label>
                    <div className="flex gap-2 mt-1">
                        <input
                            value={authKey}
                            onChange={e => setAuthKey(e.target.value)}
                            placeholder="Authorization"
                            className="w-1/3 bg-surface border border-border-strong text-text text-sm px-3 py-2 focus:border-accent focus:outline-none rounded-md"
                        />
                        <input
                            value={authValue}
                            onChange={e => setAuthValue(e.target.value)}
                            placeholder="Bearer <token>"
                            className="flex-1 bg-surface border border-border-strong text-text text-sm px-3 py-2 focus:border-accent focus:outline-none rounded-md"
                        />
                    </div>
                </div>
            </div>

            {error && (
                <div className="text-sm text-danger bg-danger-subtle border border-danger/25 px-3 py-2 rounded-md">{error}</div>
            )}

            {/* Preview action */}
            <div className="flex items-center gap-2">
                <button
                    onClick={doPreview}
                    disabled={loading}
                    className={buttonStyles({ variant: "secondary" })}
                >
                    {loading && !preview ? <Spinner size={16} /> : null}
                    Preview tools
                </button>
            </div>

            {/* Preview list */}
            {preview && (
                <div className="space-y-2">
                    <div className="flex items-center justify-between border-t border-border pt-3">
                        <button onClick={toggleAll} className="flex items-center gap-2 text-xs text-text-muted hover:text-text">
                            {allSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                            {preview.length} operation{preview.length === 1 ? '' : 's'} found
                        </button>
                        <span className="text-xs text-text-faint">{selectedCount} selected</span>
                    </div>
                    {preview.length === 0 ? (
                        <div className="py-6 text-center text-text-faint italic text-sm border border-dashed border-border rounded-md">
                            No operations found in this spec.
                        </div>
                    ) : (
                        <div className="max-h-64 overflow-y-auto rounded-md border border-border divide-y divide-border">
                            {preview.map(t => (
                                <button
                                    key={t.name}
                                    onClick={() => toggle(t.name)}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-row-hover"
                                >
                                    {selected[t.name] ? <CheckSquare className="h-4 w-4 text-accent-ink shrink-0" /> : <Square className="h-4 w-4 text-text-faint shrink-0" />}
                                    <span className={`text-[11px] font-semibold px-1.5 py-0.5 rounded-md shrink-0 ${
                                        t.method === 'GET' ? 'bg-surface-2 text-text-muted' :
                                        t.method === 'DELETE' ? 'bg-danger-subtle text-danger' :
                                        'bg-machine-subtle text-accent-ink'
                                    }`}>{t.method}</span>
                                    <span className="font-mono text-xs text-text truncate">{t.name}</span>
                                    <span className="font-mono text-[10px] text-text-faint truncate ml-auto">{t.url}</span>
                                </button>
                            ))}
                        </div>
                    )}
                    <div className="flex justify-end gap-2 pt-1">
                        <button onClick={onClose} className={buttonStyles({ variant: "ghost" })}>Cancel</button>
                        <button
                            onClick={doImport}
                            disabled={loading || selectedCount === 0}
                            className={buttonStyles({ variant: "primary" })}
                        >
                            {loading && preview ? <Spinner size={16} /> : <Import className="h-4 w-4" />}
                            Import {selectedCount || ''} tool{selectedCount === 1 ? '' : 's'}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

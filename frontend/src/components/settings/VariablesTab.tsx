'use client';

import { useEffect, useState } from 'react';
import { Plus, Save, Trash, Variable } from 'lucide-react';
import { Button, Input, Label } from '@/components/ui';

/**
 * Global variables — one document, used everywhere a value is typed before it
 * is known: `{{ global.key }}` in a tool's URL, headers and body, in an agent's
 * system prompt, in an orchestration step's prompt template.
 */
type Row = { id: string; key: string; value: string };

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/;

export const VariablesTab = () => {
    const [rows, setRows] = useState<Row[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [saved, setSaved] = useState(false);

    useEffect(() => {
        fetch('/api/settings/variables')
            .then((r) => r.json())
            .then((data) => {
                const values = (data?.values ?? {}) as Record<string, unknown>;
                setRows(Object.entries(values).map(([key, value], i) => ({
                    id: `v-${i}`, key, value: typeof value === 'string' ? value : JSON.stringify(value),
                })));
            })
            .catch(() => setError('Could not load the variables.'))
            .finally(() => setLoading(false));
    }, []);

    const badKeys = rows.filter((r) => r.key.trim() && !KEY_RE.test(r.key.trim())).map((r) => r.key);
    const duplicate = rows.map((r) => r.key.trim()).filter((k, i, all) => k && all.indexOf(k) !== i);

    const save = async () => {
        setError(null);
        setSaved(false);
        if (badKeys.length) { setError(`Names must be letters, numbers, hyphens or underscores, and not start with a digit: ${badKeys.join(', ')}`); return; }
        if (duplicate.length) { setError(`Duplicate name: ${duplicate[0]}`); return; }
        setSaving(true);
        try {
            const values: Record<string, string> = {};
            for (const r of rows) if (r.key.trim()) values[r.key.trim()] = r.value;
            const res = await fetch('/api/settings/variables', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values }),
            });
            if (!res.ok) throw new Error((await res.json())?.detail || 'Save failed.');
            setSaved(true);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Save failed.');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="space-y-4">
            <div>
                <h3 className="text-sm font-semibold text-text flex items-center gap-2"><Variable className="h-4 w-4 text-accent" /> Global variables</h3>
                <p className="text-xs text-text-muted mt-1">
                    Use them anywhere a value is not yet known: <code className="font-mono text-2xs">{'{{ global.api_base }}'}</code> in a tool&apos;s URL, headers or body, in an agent&apos;s system prompt, or in an orchestration step&apos;s prompt.
                </p>
            </div>

            {loading ? (
                <p className="text-xs text-text-faint">Loading…</p>
            ) : (
                <div className="space-y-2">
                    <div className="grid grid-cols-[1fr_2fr_auto] gap-2 text-2xs uppercase tracking-wider text-text-faint px-1">
                        <Label>Name</Label><Label>Value</Label><span />
                    </div>
                    {rows.map((row, idx) => (
                        <div key={row.id} className="grid grid-cols-[1fr_2fr_auto] gap-2 items-center">
                            <Input value={row.key} placeholder="api_base" spellCheck={false} autoComplete="off"
                                onChange={(e) => { const next = [...rows]; next[idx] = { ...row, key: e.target.value }; setRows(next); }} />
                            <Input value={row.value} placeholder="https://api.acme.com" autoComplete="off"
                                onChange={(e) => { const next = [...rows]; next[idx] = { ...row, value: e.target.value }; setRows(next); }} />
                            <button type="button" aria-label={`Remove ${row.key || 'variable'}`} onClick={() => setRows(rows.filter((r) => r.id !== row.id))}
                                className="p-1.5 text-text-faint hover:text-danger rounded-md">
                                <Trash className="h-4 w-4" />
                            </button>
                        </div>
                    ))}
                    {rows.length === 0 && <p className="text-xs text-text-faint px-1">No variables yet.</p>}
                    <div className="flex items-center gap-2 pt-1">
                        <Button variant="secondary" size="sm" onClick={() => setRows([...rows, { id: `v-${Date.now()}`, key: '', value: '' }])}>
                            <Plus className="h-4 w-4" /> Add variable
                        </Button>
                        <Button size="sm" onClick={save} loading={saving}>
                            <Save className="h-4 w-4" /> Save
                        </Button>
                        {saved && <span className="text-xs text-success">Saved.</span>}
                    </div>
                    {error && <p className="text-xs text-danger">{error}</p>}
                </div>
            )}
        </div>
    );
};

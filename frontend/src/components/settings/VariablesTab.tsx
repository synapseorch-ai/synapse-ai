'use client';

import { useEffect, useState } from 'react';
import { Plus, Save, Trash } from 'lucide-react';
import { Button, IconButton, Input, Label, Section } from '@/components/ui';
import { FIELD_LABEL, FORM } from './form';

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
        <Section variant="plain" collapsible={false} title="Global variables">
            <div className={`${FORM} space-y-4`}>
                <p className="text-sm text-text-muted">
                    Use them anywhere a value is not yet known: <code className="rounded bg-surface-2 px-1 font-code text-xs text-text">{'{{ global.api_base }}'}</code> in a tool&apos;s URL, headers or body, in an agent&apos;s system prompt, or in a workflow step&apos;s prompt.
                </p>

            {loading ? (
                <p className="text-xs text-text-faint">Loading…</p>
            ) : (
                <div className="space-y-2">
                    <div className="grid grid-cols-[1fr_2fr_2rem] gap-2 px-1 text-xs text-text-faint">
                        <Label className={FIELD_LABEL}>Name</Label><Label className={FIELD_LABEL}>Value</Label><span />
                    </div>
                    {rows.map((row, idx) => (
                        <div key={row.id} className="grid grid-cols-[1fr_2fr_2rem] items-center gap-2">
                            <Input value={row.key} aria-label={`Variable ${idx + 1} name`} placeholder="api_base" spellCheck={false} autoComplete="off"
                                onChange={(e) => { const next = [...rows]; next[idx] = { ...row, key: e.target.value }; setRows(next); }} />
                            <Input value={row.value} aria-label={`Variable ${idx + 1} value`} placeholder="https://api.acme.com" autoComplete="off"
                                onChange={(e) => { const next = [...rows]; next[idx] = { ...row, value: e.target.value }; setRows(next); }} />
                            <IconButton size="sm" icon={Trash} label={`Remove ${row.key || 'variable'}`} className="hover:text-danger"
                                onClick={() => setRows(rows.filter((r) => r.id !== row.id))} />
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
                        {saved && <span role="status" className="text-xs text-text-muted">Saved.</span>}
                    </div>
                    {error && <p className="text-xs text-danger">{error}</p>}
                </div>
            )}
            </div>
        </Section>
    );
};

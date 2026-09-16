'use client';
import { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Button, Input, Select, Textarea } from '@/components/ui';
import type { StateSchemaEntry } from '@/types/orchestration';

import {
    STATE_TYPES,
    STATE_TYPE_LABELS,
    nextDefaultForType,
    normalizeStateType,
} from './stateTypes';

interface StateSchemaEditorProps {
    schema: Record<string, StateSchemaEntry>;
    onChange: (schema: Record<string, StateSchemaEntry>) => void;
}

const TYPE_OPTIONS = STATE_TYPES.map((t) => ({ value: t, label: STATE_TYPE_LABELS[t] }));

export function StateSchemaEditor({ schema, onChange }: StateSchemaEditorProps) {
    const entries = Object.entries(schema);
    // Raw JSON text buffers for list/dict editors, keyed by entry key.
    // Lets users type freely while we only commit valid JSON to the schema.
    const [jsonBuffers, setJsonBuffers] = useState<Record<string, string>>({});
    const [jsonErrors, setJsonErrors] = useState<Record<string, boolean>>({});

    const addEntry = () => {
        const key = `key_${Date.now()}`;
        onChange({ ...schema, [key]: { type: 'str', default: '', description: '' } });
    };

    const removeEntry = (key: string) => {
        const next = { ...schema };
        delete next[key];
        onChange(next);
        const nextBufs = { ...jsonBuffers }; delete nextBufs[key]; setJsonBuffers(nextBufs);
        const nextErrs = { ...jsonErrors }; delete nextErrs[key]; setJsonErrors(nextErrs);
    };

    const updateKey = (oldKey: string, newKey: string) => {
        if (newKey === oldKey) return;
        const next: Record<string, StateSchemaEntry> = {};
        for (const [k, v] of Object.entries(schema)) {
            next[k === oldKey ? newKey : k] = v;
        }
        onChange(next);
        if (jsonBuffers[oldKey] !== undefined) {
            const nextBufs = { ...jsonBuffers };
            nextBufs[newKey] = nextBufs[oldKey];
            delete nextBufs[oldKey];
            setJsonBuffers(nextBufs);
        }
        if (jsonErrors[oldKey] !== undefined) {
            const nextErrs = { ...jsonErrors };
            nextErrs[newKey] = nextErrs[oldKey];
            delete nextErrs[oldKey];
            setJsonErrors(nextErrs);
        }
    };

    const updateEntry = (key: string, patch: Partial<StateSchemaEntry>) => {
        onChange({ ...schema, [key]: { ...schema[key], ...patch } });
    };

    const changeType = (key: string, newType: string) => {
        // Keep a default the new type can still hold; reset only a mismatched one.
        // This control is also what a person reaches for to fix a key whose type
        // was unreadable — wiping on every change made that fix destroy the very
        // value it was meant to save.
        const next = nextDefaultForType(schema[key]?.default, newType);
        onChange({ ...schema, [key]: { ...schema[key], type: newType, default: next } });
        const nextBufs = { ...jsonBuffers }; delete nextBufs[key]; setJsonBuffers(nextBufs);
        const nextErrs = { ...jsonErrors }; delete nextErrs[key]; setJsonErrors(nextErrs);
    };

    const handleJsonChange = (key: string, raw: string) => {
        setJsonBuffers({ ...jsonBuffers, [key]: raw });
        try {
            const parsed = raw.trim() === ''
                ? (normalizeStateType(schema[key].type) === 'list' ? [] : {})
                : JSON.parse(raw);
            updateEntry(key, { default: parsed });
            setJsonErrors({ ...jsonErrors, [key]: false });
        } catch {
            setJsonErrors({ ...jsonErrors, [key]: true });
        }
    };

    const renderDefaultInput = (key: string, entry: StateSchemaEntry) => {
        switch (normalizeStateType(entry.type)) {
            case 'int':
            case 'float':
                return (
                    <Input
                        size="sm"
                        type="number"
                        // A whole-number key should not offer a spinner that
                        // steps into decimals its declared type cannot hold.
                        step={normalizeStateType(entry.type) === 'int' ? 1 : 'any'}
                        className="font-code"
                        aria-label={`Default for ${key}`}
                        value={entry.default === '' || entry.default == null ? '' : Number(entry.default as number)}
                        onChange={(e) => {
                            const v = e.target.value;
                            updateEntry(key, { default: v === '' ? 0 : Number(v) });
                        }}
                        placeholder="0"
                    />
                );
            case 'bool':
                return (
                    <Select
                        size="sm"
                        aria-label={`Default for ${key}`}
                        value={entry.default === true ? 'true' : 'false'}
                        onChange={(v) => updateEntry(key, { default: v === 'true' })}
                        options={[
                            { value: 'false', label: 'false' },
                            { value: 'true', label: 'true' },
                        ]}
                    />
                );
            case 'list':
            case 'dict': {
                const isList = normalizeStateType(entry.type) === 'list';
                const buffered = jsonBuffers[key];
                const display = buffered !== undefined
                    ? buffered
                    : (() => {
                        try { return JSON.stringify(entry.default ?? (isList ? [] : {}), null, 2); }
                        catch { return isList ? '[]' : '{}'; }
                    })();
                const hasError = !!jsonErrors[key];
                return (
                    <div>
                        <Textarea
                            size="sm"
                            className="min-h-[48px] font-code"
                            aria-label={`Default for ${key}`}
                            invalid={hasError}
                            value={display}
                            onChange={(e) => handleJsonChange(key, e.target.value)}
                            placeholder={isList ? '[]' : '{}'}
                            rows={3}
                        />
                        {hasError && (
                            <p role="alert" className="mt-1 text-[10px] text-danger">
                                Invalid JSON — the value is not saved until it parses.
                            </p>
                        )}
                    </div>
                );
            }
            default:
                return (
                    <Input
                        size="sm"
                        className="font-code"
                        aria-label={`Default for ${key}`}
                        value={(entry.default as string) ?? ''}
                        onChange={(e) => updateEntry(key, { default: e.target.value })}
                        placeholder="Default value"
                    />
                );
        }
    };

    return (
        <div className="space-y-2">
            <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-text-muted">State Schema</span>
                <Button size="sm" variant="ghost" onClick={addEntry}>
                    <Plus size={12} aria-hidden /> Add key
                </Button>
            </div>
            {entries.length === 0 && (
                <div className="text-xs italic text-text-faint">No state keys defined. Steps will still work with implicit state.</div>
            )}
            {entries.map(([key, entry]) => {
                const declared = normalizeStateType(entry.type);
                // A stored type we cannot read is shown back rather than replaced by a
                // guess — rewriting it on render would either dirty the draft merely by
                // opening it, or save over an AI-builder mistake only a person can judge.
                const unknownType = declared === null && !!entry.type;
                return (
                    <div key={key} className="flex items-start gap-2 rounded-md border border-border bg-surface-2/40 p-2">
                        <div className="flex-1 space-y-1.5">
                            <div className="flex gap-2">
                                <Input
                                    size="sm"
                                    className="flex-1 font-code"
                                    aria-label="Key name"
                                    value={key}
                                    onChange={(e) => updateKey(key, e.target.value)}
                                    placeholder="key_name"
                                />
                                <Select
                                    size="sm"
                                    className="w-36 shrink-0"
                                    aria-label={`Type for ${key}`}
                                    // Radix renders a blank trigger — not the placeholder — for a
                                    // value matching no option, which is how a stored `str` came to
                                    // look like an empty box. Carrying the unknown value as its own
                                    // option makes that impossible; a genuinely missing type falls
                                    // through to `undefined` and gets the placeholder, as it should.
                                    value={declared ?? (entry.type || undefined)}
                                    onChange={(t) => changeType(key, t)}
                                    options={unknownType
                                        ? [...TYPE_OPTIONS, { value: entry.type, label: `unknown type \u201C${entry.type}\u201D`, disabled: true }]
                                        : TYPE_OPTIONS}
                                />
                            </div>
                            {unknownType && (
                                <p role="alert" className="text-[10px] text-danger">
                                    “{entry.type}” isn’t a type we know — pick one.
                                </p>
                            )}
                            <Input
                                size="sm"
                                aria-label={`Description for ${key}`}
                                value={entry.description}
                                onChange={(e) => updateEntry(key, { description: e.target.value })}
                                placeholder="Description…"
                            />
                            <div className="flex items-center gap-2">
                                <span className="w-12 shrink-0 text-[10px] uppercase tracking-wider text-text-faint">Default</span>
                                <div className="min-w-0 flex-1">{renderDefaultInput(key, entry)}</div>
                            </div>
                        </div>
                        <button
                            type="button"
                            aria-label={`Remove ${key}`}
                            onClick={() => removeEntry(key)}
                            className="mt-1 text-text-faint transition-colors hover:text-danger"
                        >
                            <Trash2 size={12} aria-hidden />
                        </button>
                    </div>
                );
            })}
        </div>
    );
}

'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * Shared building blocks for the step config panel.
 *
 * Every control here is the design kit; the four ad-hoc class-string constants
 * (`inputCls`, `selectCls`, …) that used to be prop-drilled through every
 * sub-component are gone — `controlStyles` is the one source of control shape.
 */

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { Combobox, Input } from '@/components/ui';
import type { Orchestration, StepConfig } from '@/types/orchestration';

/** Props every per-type section receives. */
export interface StepSectionProps {
    step: StepConfig;
    update: (patch: Partial<StepConfig>) => void;
    otherSteps: { id: string; name: string }[];
    agents: any[];
    availableModels: string[];
    availableTools: { name: string; description: string }[];
    stateKeys: string[];
    showVaultHints: boolean;
    /** Where a transform's code runs, in this product's words. See `StepConfigPanel`. */
    codeRunnerNote: string;
    orchestration?: Orchestration;
}

/** Neutral explainer block — replaces the per-type rainbow of tinted boxes. */
export function Note({ children }: { children: React.ReactNode }) {
    return (
        <div className="rounded-md border border-border bg-surface-2/50 px-3 py-2 text-[11px] leading-relaxed text-text-muted">
            {children}
        </div>
    );
}

const END_VALUE = '__end__';

/**
 * A step-target picker. Searchable because a real orchestration has dozens of
 * steps and the native select this replaces could only be scrolled.
 */
export function StepTargetSelect({
    value,
    onChange,
    otherSteps,
    noneLabel = 'None (end)',
    'aria-label': ariaLabel,
}: {
    value: string | null | undefined;
    onChange: (id: string | undefined) => void;
    otherSteps: { id: string; name: string }[];
    noneLabel?: string;
    'aria-label'?: string;
}) {
    return (
        <Combobox
            size="sm"
            aria-label={ariaLabel}
            value={value ?? END_VALUE}
            onChange={(v) => onChange(v === END_VALUE ? undefined : v)}
            options={[
                { value: END_VALUE, label: noneLabel },
                ...otherSteps.map((s) => ({ value: s.id, label: s.name })),
            ]}
            searchPlaceholder="Search steps…"
        />
    );
}

/** Per-step model override — "(Default)" means the agent/engine default. */
export function ModelSelect({
    value,
    onChange,
    availableModels,
    'aria-label': ariaLabel = 'Model override',
}: {
    value: string | undefined;
    onChange: (model: string | undefined) => void;
    availableModels: string[];
    'aria-label'?: string;
}) {
    const DEFAULT = '__default__';
    return (
        <Combobox
            size="sm"
            aria-label={ariaLabel}
            value={value || DEFAULT}
            onChange={(v) => onChange(v === DEFAULT ? undefined : v)}
            options={[
                { value: DEFAULT, label: '(Default)' },
                ...availableModels.map((m) => ({ value: m, label: m })),
            ]}
            searchPlaceholder="Search models…"
        />
    );
}

/**
 * Multi-select over state keys as toggleable chips, with a free-text escape
 * hatch — replaces the comma-separated text field nobody could get right on
 * the first try. Suggestions come from the state schema plus every step's
 * output key.
 */
export function KeyChips({
    value,
    onChange,
    suggestions,
}: {
    value: string[];
    onChange: (keys: string[]) => void;
    suggestions: string[];
}) {
    const [custom, setCustom] = useState('');
    const remaining = suggestions.filter((k) => !value.includes(k));

    const addCustom = () => {
        const key = custom.trim();
        if (key && !value.includes(key)) onChange([...value, key]);
        setCustom('');
    };

    return (
        <div className="space-y-1.5">
            {value.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {value.map((key) => (
                        <span
                            key={key}
                            className="inline-flex items-center gap-1 rounded-md bg-accent-subtle px-1.5 py-0.5 font-code text-[11px] text-accent"
                            translate="no"
                        >
                            {key}
                            <button
                                type="button"
                                aria-label={`Remove ${key}`}
                                onClick={() => onChange(value.filter((k) => k !== key))}
                                className="rounded-sm opacity-70 transition-opacity hover:opacity-100"
                            >
                                <X size={10} aria-hidden />
                            </button>
                        </span>
                    ))}
                </div>
            )}
            {remaining.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {remaining.map((key) => (
                        <button
                            key={key}
                            type="button"
                            onClick={() => onChange([...value, key])}
                            title="Add as input key"
                            className="rounded-md border border-border px-1.5 py-0.5 font-code text-[11px] text-text-faint transition-colors hover:border-accent hover:text-accent"
                            translate="no"
                        >
                            + {key}
                        </button>
                    ))}
                </div>
            )}
            <Input
                size="sm"
                className="font-code"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                onBlur={addCustom}
                onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        addCustom();
                    }
                }}
                placeholder="Add a custom key…"
                aria-label="Add a custom input key"
            />
        </div>
    );
}

/**
 * Clickable `state.<key>` chips under an expression input — the schema is
 * right there in the draft, so the author should never have to remember a key.
 */
export function StateKeyHelper({
    stateKeys,
    onInsert,
}: {
    stateKeys: string[];
    onInsert: (token: string) => void;
}) {
    if (stateKeys.length === 0) return null;
    return (
        <div className="flex flex-wrap gap-1 pt-1">
            {stateKeys.slice(0, 8).map((key) => (
                <button
                    key={key}
                    type="button"
                    onClick={() => onInsert(`state.${key}`)}
                    title={`Insert state.${key}`}
                    className="rounded-md border border-border px-1.5 py-0.5 font-code text-[10px] text-text-faint transition-colors hover:border-accent hover:text-accent"
                    translate="no"
                >
                    state.{key}
                </button>
            ))}
        </div>
    );
}

/** Text input that buffers locally and commits on blur/Enter — prevents focus loss on keyed renames. */
export function LocalInput({
    value,
    onCommit,
    size = 'sm',
    ...props
}: { value: string; onCommit: (val: string) => void; size?: 'sm' | 'md' } & Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    'value' | 'size'
>) {
    const [local, setLocal] = useState(value);
    useEffect(() => {
        setLocal(value);
    }, [value]);
    return (
        <Input
            {...props}
            size={size}
            value={local}
            onChange={(e) => setLocal(e.target.value)}
            onBlur={() => onCommit(local)}
            onKeyDown={(e) => {
                if (e.key === 'Enter') onCommit(local);
            }}
        />
    );
}

/** Parse a number input that may be cleared: '' → undefined. */
export function intOrUndefined(raw: string): number | undefined {
    return raw === '' ? undefined : parseInt(raw);
}

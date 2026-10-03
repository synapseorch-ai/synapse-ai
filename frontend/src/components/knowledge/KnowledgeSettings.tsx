'use client';

/*
 * A knowledge base's settings (B2b, D167): a preset, how it is searched, how its
 * index is built, its budget and who may search it.
 *
 * Search settings apply to the next search. Index settings (chunking, the
 * embedding model, enrichment, the language) rebuild the index beside the one
 * serving, which keeps answering until the new one holds every document — so a
 * draft that changes them is priced first (the cost preview), and saving says
 * what it will cost. Saving sends only what changed, from the revision the edit
 * started at; someone else's save in between is a 409, and the drawer offers to
 * reload.
 *
 * Mounted only while open, so each opening starts from the base as it is.
 */
import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';

import {
    Button, Combobox, Drawer, ErrorNote, Field, Input, Label, Muted, Section, Select, Spinner,
    type ComboboxOption, type SelectOption,
} from '@/components/ui';
import { cn } from '@/lib/cn';
import {
    INDEX_AFFECTING, KnowledgeApiError, LANGUAGES, PRESETS, applyPreset, formatUsd as usd,
    knowledgeApi, matchingPreset,
    type BaseSettings, type CostPreview, type KnowledgeBase,
} from '@/lib/knowledge';

import { useKnowledgeHost, type KnowledgeWorkspace } from './host';

const EDITED = ['language', 'visitor_access', 'budget_monthly_usd', 'chunking', 'embedding', 'rerank', 'fusion',
    'retrieval', 'enrichment'] as const satisfies readonly (keyof BaseSettings)[];

const DEFAULT_MODEL = '__default__';

function pick(kb: KnowledgeBase): BaseSettings {
    return Object.fromEntries(EDITED.map(k => [k, structuredClone(kb[k])])) as BaseSettings;
}

function same(a: unknown, b: unknown): boolean {
    return JSON.stringify(a) === JSON.stringify(b);
}


/** A number field that keeps what is typed and reports a number, or null when empty (if `optional`). */
function NumberInput({
    value, onChange, optional, step, min, max, ...rest
}: {
    value: number | null;
    onChange: (value: number | null) => void;
    optional?: boolean;
    step?: number;
    min?: number;
    max?: number;
    'aria-label'?: string;
    id?: string;
    placeholder?: string;
}) {
    const [text, setText] = useState(value == null ? '' : String(value));
    const [seen, setSeen] = useState(value);
    if (value !== seen) {  // set from outside (a preset, a reload): show it, unless it is what was typed
        setSeen(value);
        const typed = text.trim() === '' ? null : Number(text);
        if (typed !== value) setText(value == null ? '' : String(value));
    }
    return (
        <Input
            {...rest}
            type="number"
            inputMode="decimal"
            step={step}
            min={min}
            max={max}
            value={text}
            onChange={e => {
                setText(e.target.value);
                if (e.target.value.trim() === '') {
                    if (optional) {
                        setSeen(null);
                        onChange(null);
                    }
                    return;
                }
                const n = Number(e.target.value);
                if (Number.isFinite(n)) {
                    setSeen(n);
                    onChange(n);
                }
            }}
        />
    );
}

function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            aria-label={label}
            onClick={() => onChange(!checked)}
            className={cn('relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
                checked ? 'bg-accent' : 'bg-surface-2')}
        >
            <span className={cn('inline-block size-4 rounded-full transition-transform',
                checked ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted')} />
        </button>
    );
}

export function KnowledgeSettings({
    kb, documents, onClose, onSaved,
}: {
    kb: KnowledgeBase;
    /** Documents a rebuild would carry over (indexed and partly indexed). */
    documents: number;
    onClose: () => void;
    onSaved: (kb: KnowledgeBase) => void;
}) {
    const [base, setBase] = useState(kb);
    const [draft, setDraft] = useState<BaseSettings>(() => pick(kb));
    const host = useKnowledgeHost();
    const [workspace, setWorkspace] = useState<KnowledgeWorkspace | null>(null);
    const [preview, setPreview] = useState<CostPreview | null>(null);
    const [previewError, setPreviewError] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [stale, setStale] = useState(false);

    useEffect(() => {
        let live = true;
        host.loadWorkspace().then(w => { if (live) setWorkspace(w); });
        return () => { live = false; };
    }, [host]);

    const saved = useMemo(() => pick(base), [base]);
    const changed = EDITED.filter(k => !same(draft[k], saved[k]));
    const rebuilds = changed.some(k => (INDEX_AFFECTING as readonly string[]).includes(k)) && !!base.state.active_snapshot;
    const preset = matchingPreset(draft as unknown as Record<string, unknown>);

    // Price a draft that rebuilds the index; a newer draft aborts the older request.
    const priced = rebuilds ? JSON.stringify(Object.fromEntries(INDEX_AFFECTING.map(k => [k, draft[k]]))) : '';
    useEffect(() => {
        if (!priced) return;
        const control = new AbortController();
        const timer = setTimeout(() => {
            const overrides = { ...JSON.parse(priced), budget_monthly_usd: draft.budget_monthly_usd };
            knowledgeApi.costPreview(base.id, overrides, { signal: control.signal })
                .then(p => { setPreview(p); setPreviewError(''); })
                .catch(e => {
                    if (control.signal.aborted) return;
                    setPreview(null);
                    setPreviewError(e instanceof Error ? e.message : String(e));
                });
        }, 400);
        return () => { clearTimeout(timer); control.abort(); };
        // The budget only changes what is left, not the price: it is read when the index draft changes.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [priced, base.id]);

    const set = <K extends keyof BaseSettings>(key: K, value: BaseSettings[K]) => setDraft(d => ({ ...d, [key]: value }));
    const setIn = <K extends 'chunking' | 'embedding' | 'rerank' | 'fusion' | 'retrieval' | 'enrichment'>(
        key: K, field: keyof BaseSettings[K], value: unknown,
    ) => setDraft(d => ({ ...d, [key]: { ...d[key], [field]: value } }));

    const save = async () => {
        setSaving(true);
        setError('');
        try {
            const patch: Record<string, unknown> = { revision: base.revision };
            for (const k of changed) {
                if (k === 'budget_monthly_usd' && draft.budget_monthly_usd == null) patch.clear_budget = true;
                else patch[k] = draft[k];
            }
            onSaved(await knowledgeApi.updateBase(base.id, patch as Parameters<typeof knowledgeApi.updateBase>[1]));
        } catch (e) {
            if (e instanceof KnowledgeApiError && e.status === 409) setStale(true);
            else setError(e instanceof Error ? e.message : String(e));
        } finally {
            setSaving(false);
        }
    };

    const reload = async () => {
        try {
            const fresh = await knowledgeApi.base(base.id);
            setBase(fresh);
            setDraft(pick(fresh));
            setStale(false);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    const embeddingOptions: ComboboxOption[] = useMemo(() => {
        const defaultLabel = workspace?.embedding ? `Workspace default (${workspace.embedding})` : 'Workspace default (none set)';
        const listed = host.withUnreachable(workspace?.embeddingModels ?? [], draft.embedding.model || undefined);
        return [{ value: DEFAULT_MODEL, label: defaultLabel }, ...listed];
    }, [host, workspace, draft.embedding.model]);

    const contextualOptions: ComboboxOption[] = useMemo(() => {
        const fallback = workspace?.contextual || workspace?.model || '';
        const defaultLabel = fallback ? `Workspace default (${fallback})` : 'Workspace default (none set)';
        const listed = host.withUnreachable(workspace?.chatModels ?? [], draft.enrichment.contextual_model || undefined);
        return [{ value: DEFAULT_MODEL, label: defaultLabel }, ...listed];
    }, [host, workspace, draft.enrichment.contextual_model]);

    const rerankRoute = host.rerankChoices.find(r => (draft.rerank.model ?? '').startsWith(r.model.split('.')[0] + '.'));
    const rerankKeyMissing = !!rerankRoute?.key && workspace !== null && !workspace.keys[rerankRoute.key];

    const presetOptions: SelectOption<string>[] = [
        ...PRESETS.map(p => ({ value: p.id, label: p.name, hint: p.description })),
        { value: 'custom', label: 'Custom', disabled: true },
    ];
    const spent = base.state.month_spend_usd;
    const cap = draft.budget_monthly_usd;

    return (
        <Drawer
            open
            onClose={onClose}
            width="lg"
            title={`Settings for ${base.name}`}
            description="Search settings apply to the next search. Index settings rebuild the index; the current one answers until the new one is ready."
            footer={(
                <>
                    <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
                    <Button onClick={save} loading={saving} disabled={!changed.length || stale || (rebuilds && !!previewError)}>
                        {rebuilds ? 'Save and rebuild' : 'Save'}
                    </Button>
                </>
            )}
        >
            <div className="space-y-6">
                {stale && (
                    <ErrorNote>
                        Someone changed this knowledge base since you opened it.{' '}
                        <button type="button" className="font-medium underline" onClick={reload}>Reload it</button> and make your change again.
                    </ErrorNote>
                )}
                {error && <ErrorNote>{error}</ErrorNote>}

                <Field label="Preset" hint={preset ? preset.description : 'Your own settings. Pick a preset to start from one.'}>
                    <Select
                        value={preset?.id ?? 'custom'}
                        onChange={id => {
                            const p = PRESETS.find(x => x.id === id);
                            if (p) setDraft(d => applyPreset(d as unknown as Record<string, unknown>, p) as unknown as BaseSettings);
                        }}
                        options={presetOptions}
                        aria-label="Preset"
                    />
                </Field>

                <fieldset className="space-y-4">
                    <legend className="mb-1 text-sm font-semibold text-text">Search</legend>
                    <Muted className="block">Applies to the next search.</Muted>
                    <div className="grid gap-4 sm:grid-cols-2">
                        <Field label="Passages per search" hint="1–50.">
                            <NumberInput value={draft.retrieval.top_k} min={1} max={50} step={1}
                                onChange={v => setIn('retrieval', 'top_k', v ?? 8)} aria-label="Passages per search" />
                        </Field>
                        <Field label="Minimum score" hint="Optional. Reads the reranker's score, or one leg's.">
                            <NumberInput optional value={draft.retrieval.score_threshold} step={0.01}
                                onChange={v => setIn('retrieval', 'score_threshold', v)} aria-label="Minimum score" />
                        </Field>
                        <Field label="Rerank" hint="Default: on where a reranker is configured.">
                            <Select
                                value={draft.rerank.enabled === null ? 'default' : draft.rerank.enabled ? 'on' : 'off'}
                                onChange={v => setIn('rerank', 'enabled', v === 'default' ? null : v === 'on')}
                                options={[{ value: 'default', label: 'Default' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]}
                                aria-label="Rerank"
                            />
                        </Field>
                        <Field
                            label="Rerank model"
                            hint={rerankKeyMissing
                                ? <>Needs {rerankRoute?.provider}: add it on <Link href="/settings/models" className="underline">Settings → Models</Link>.</>
                                : `Optional. Named with its provider, e.g. ${host.rerankChoices[0]?.model ?? 'cohere.rerank-v3.5'}.`}
                        >
                            <Input
                                value={draft.rerank.model ?? ''}
                                onChange={e => setIn('rerank', 'model', e.target.value.trim() || null)}
                                placeholder="Workspace default"
                                list="knowledge-rerank-models"
                                autoComplete="off"
                                spellCheck={false}
                            />
                        </Field>
                        <datalist id="knowledge-rerank-models">
                            {host.rerankChoices.map(r => <option key={r.model} value={r.model}>{r.provider}</option>)}
                        </datalist>
                        <Field label="With neighbouring passages">
                            <Select
                                value={draft.retrieval.parent_expansion}
                                onChange={v => setIn('retrieval', 'parent_expansion', v)}
                                options={[
                                    { value: 'off', label: 'No' },
                                    { value: 'neighbors', label: 'Neighbours' },
                                    { value: 'section', label: 'The whole section' },
                                ]}
                                aria-label="With neighbouring passages"
                            />
                        </Field>
                        <Field label="Context limit (tokens)" hint="500–100,000.">
                            <NumberInput value={draft.retrieval.max_context_tokens} min={500} max={100000} step={500}
                                onChange={v => setIn('retrieval', 'max_context_tokens', v ?? 6000)} aria-label="Context limit (tokens)" />
                        </Field>
                    </div>
                    <Section title="Fusion and candidates" description="How the keyword and meaning searches are combined.">
                        <div className="grid gap-4 sm:grid-cols-2">
                            <Field label="Keyword weight">
                                <NumberInput value={draft.fusion.weights.lexical ?? 1} min={0} step={0.1}
                                    onChange={v => setIn('fusion', 'weights', { ...draft.fusion.weights, lexical: v ?? 0 })} aria-label="Keyword weight" />
                            </Field>
                            <Field label="Meaning weight">
                                <NumberInput value={draft.fusion.weights.vector ?? 1} min={0} step={0.1}
                                    onChange={v => setIn('fusion', 'weights', { ...draft.fusion.weights, vector: v ?? 0 })} aria-label="Meaning weight" />
                            </Field>
                            <Field label="Candidates per search" hint="10–200, before fusion and rerank.">
                                <NumberInput value={draft.fusion.overfetch} min={10} max={200} step={10}
                                    onChange={v => setIn('fusion', 'overfetch', v ?? 50)} aria-label="Candidates per search" />
                            </Field>
                            <Field label="Rerank candidates" hint="1–100.">
                                <NumberInput value={draft.rerank.candidates} min={1} max={100} step={10}
                                    onChange={v => setIn('rerank', 'candidates', v ?? 50)} aria-label="Rerank candidates" />
                            </Field>
                        </div>
                    </Section>
                </fieldset>

                <fieldset className="space-y-4">
                    <legend className="mb-1 text-sm font-semibold text-text">Index</legend>
                    <Muted className="block">Changing these rebuilds the index.</Muted>
                    <div className="grid gap-4 sm:grid-cols-2">
                        <Field label="Embedding model" hint="Search by meaning. Without one, words only.">
                            <Combobox
                                value={draft.embedding.model || DEFAULT_MODEL}
                                onChange={v => setDraft(d => ({ ...d, embedding: { ...d.embedding, model: v === DEFAULT_MODEL ? '' : v } }))}
                                options={embeddingOptions}
                                aria-label="Embedding model"
                            />
                        </Field>
                        <Field label="Width" hint="Optional. The model's own when empty.">
                            <NumberInput optional value={draft.embedding.dims} min={1} max={8192} step={1}
                                onChange={v => setIn('embedding', 'dims', v)} aria-label="Width" placeholder="Model's own" />
                        </Field>
                        <Field label="Passages">
                            <Select
                                value={draft.chunking.mode}
                                onChange={v => setIn('chunking', 'mode', v)}
                                options={[
                                    { value: 'structure', label: 'By section' },
                                    { value: 'fixed', label: 'Fixed size' },
                                    { value: 'page', label: 'One per page' },
                                    { value: 'none', label: 'Whole document' },
                                ]}
                                aria-label="Passages"
                            />
                        </Field>
                        <Field label="Language" hint="For word matching. Simple stems nothing.">
                            <Select
                                value={draft.language}
                                onChange={v => set('language', v)}
                                options={LANGUAGES.map(l => ({ value: l, label: l[0].toUpperCase() + l.slice(1) }))}
                                aria-label="Language"
                            />
                        </Field>
                        {(draft.chunking.mode === 'structure' || draft.chunking.mode === 'fixed') && (
                            <>
                                <Field label="Passage size (tokens)" hint="64–2,000.">
                                    <NumberInput value={draft.chunking.target_tokens} min={64} max={2000} step={50}
                                        onChange={v => setDraft(d => ({ ...d, chunking: { ...d.chunking, target_tokens: v ?? 450, max_tokens: Math.max(d.chunking.max_tokens, v ?? 450) } }))}
                                        aria-label="Passage size (tokens)" />
                                </Field>
                                <Field label="Overlap" hint="0–25% of a passage.">
                                    <NumberInput value={Math.round(draft.chunking.overlap_pct * 100)} min={0} max={25} step={5}
                                        onChange={v => setIn('chunking', 'overlap_pct', (v ?? 0) / 100)} aria-label="Overlap (%)" />
                                </Field>
                            </>
                        )}
                        <Field
                            label="Context for each passage"
                            hint={draft.enrichment.mode === 'contextual'
                                ? 'A model writes a short context for every passage while it is indexed, so passages that lean on their document are found. Paid per passage: the price is below.'
                                : undefined}
                        >
                            <Select
                                value={draft.enrichment.mode}
                                // Back to the saved mode restores the saved block, so a model picked and dropped rebuilds nothing.
                                onChange={v => setDraft(d => ({
                                    ...d,
                                    enrichment: v === saved.enrichment.mode ? structuredClone(saved.enrichment) : { ...d.enrichment, mode: v },
                                }))}
                                options={[
                                    { value: 'prefix', label: 'Title, date and section (free)' },
                                    { value: 'contextual', label: 'Contextual Retrieval: a model writes it' },
                                ]}
                                aria-label="Context for each passage"
                            />
                        </Field>
                        {draft.enrichment.mode === 'contextual' && (
                            <Field label="Context model" hint="Writes the contexts. A small, inexpensive model is enough.">
                                <Combobox
                                    value={draft.enrichment.contextual_model || DEFAULT_MODEL}
                                    onChange={v => setIn('enrichment', 'contextual_model', v === DEFAULT_MODEL ? null : v)}
                                    options={contextualOptions}
                                    searchPlaceholder="Search chat models…"
                                    aria-label="Context model"
                                />
                            </Field>
                        )}
                    </div>
                </fieldset>

                <fieldset className="space-y-4">
                    <legend className="mb-1 text-sm font-semibold text-text">Budget and access</legend>
                    <Field
                        label="Monthly budget (USD)"
                        hint={`${usd(spent)} spent this month${cap != null ? `; ${usd(Math.max(0, cap - spent))} left` : ''}. At the budget, indexing pauses and searches use words only. Empty: no budget.`}
                    >
                        <NumberInput optional value={cap} min={0} step={1}
                            onChange={v => set('budget_monthly_usd', v)} aria-label="Monthly budget (USD)" placeholder="No budget" />
                    </Field>
                    <div className="flex items-start justify-between gap-4">
                        <div>
                            <Label size="sm" className="block">Visitors may search it</Label>
                            <Muted className="block text-xs">Widget and channel visitors, when its tools are also ticked on the widget or channel.</Muted>
                        </div>
                        <Switch checked={draft.visitor_access} onChange={v => set('visitor_access', v)} label="Visitors may search it" />
                    </div>
                </fieldset>

                {rebuilds && (
                    <div role="status" className="rounded-md border border-border bg-surface-2/50 px-3 py-2.5 text-sm">
                        {previewError ? (
                            <p className="text-danger">{previewError}</p>
                        ) : !preview ? (
                            <p className="flex items-center gap-2 text-text-muted"><Spinner />Pricing the rebuild…</p>
                        ) : (
                            <>
                                <p className="text-text">
                                    Saving rebuilds the index: {preview.docs || documents} {preview.docs === 1 ? 'document' : 'documents'},
                                    about <strong>{usd(preview.total_est_usd)}</strong>. The current index keeps answering until the new one is ready.
                                </p>
                                {preview.stages.length > 0 && (
                                    <ul className="mt-1.5 space-y-0.5 text-xs text-text-muted">
                                        {preview.stages.map(s => (
                                            <li key={s.stage}>
                                                {s.stage === 'embed' ? 'Embedding' : s.stage === 'ocr' ? 'Reading scanned pages' : 'Writing contexts'}:{' '}
                                                {Math.round(s.units).toLocaleString()} {s.unit} with <span className="font-mono">{s.model}</span> — {usd(s.est_usd)}
                                                {s.cost_source === 'local' ? ' (on this machine)' : s.cost_source === 'byok' ? ' (your key)' : ''}
                                            </li>
                                        ))}
                                    </ul>
                                )}
                                {preview.budget_remaining_usd != null && preview.total_est_usd > preview.budget_remaining_usd && (
                                    <p className="mt-1.5 text-xs text-warning">
                                        That is more than the {usd(preview.budget_remaining_usd)} left this month: the rebuild will pause at the budget.
                                    </p>
                                )}
                            </>
                        )}
                    </div>
                )}
            </div>
        </Drawer>
    );
}
